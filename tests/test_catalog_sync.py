"""Sync logic tested against fake raw data (shaped like tpl.ge's real
responses) — never hits the real network. See app/catalog/client.py for the
one place that actually calls tpl.ge."""

import httpx
import pytest

from app.catalog import repository as catalog_repo
from app.catalog import sync
from app.catalog.client import TplClientError
from app.db import get_connection, init_db


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    connection = get_connection(db_path)
    yield connection
    connection.close()


def test_sync_categories_maps_known_external_ids(conn):
    raw = [
        {"id": 7, "name": "მსუბუქი / Passenger Car", "vehiclecategoryIcon": "vehicle"},
        {"id": 10, "name": "მოტოციკლი / Motorcycle", "vehiclecategoryIcon": "motorcyrcle"},
    ]
    count = sync.sync_categories(conn, raw)
    assert count == 2
    codes = {c.code for c in catalog_repo.list_categories(conn)}
    assert codes == {"passenger_car", "motorcycle"}


def test_sync_categories_skips_unknown_external_id_rather_than_guessing(conn):
    raw = [{"id": 999, "name": "Something new", "vehiclecategoryIcon": None}]
    count = sync.sync_categories(conn, raw)
    assert count == 0
    assert catalog_repo.list_categories(conn) == []


def test_sync_categories_deactivates_ones_missing_from_a_later_sync(conn):
    sync.sync_categories(conn, [{"id": 7, "name": "Passenger Car", "vehiclecategoryIcon": None}, {"id": 10, "name": "Motorcycle", "vehiclecategoryIcon": None}])
    sync.sync_categories(conn, [{"id": 7, "name": "Passenger Car", "vehiclecategoryIcon": None}])

    active = {c.code for c in catalog_repo.list_categories(conn)}
    assert active == {"passenger_car"}
    all_rows = {c.code for c in catalog_repo.list_categories(conn, active_only=False)}
    assert all_rows == {"passenger_car", "motorcycle"}


def test_sync_manufacturers_idempotent(conn):
    raw = [{"id": 12, "name": "BMW", "isPopular": True}, {"id": 2, "name": "AC", "isPopular": False}]
    first = sync.sync_manufacturers(conn, raw)
    second = sync.sync_manufacturers(conn, raw)
    assert first == 2
    assert second == 2
    assert len(catalog_repo.list_manufacturers(conn)) == 2


def test_sync_manufacturers_skips_records_with_no_name_rather_than_guessing(conn):
    # Observed for real on tpl.ge: ~10 of 1856 manufacturer records have no
    # "name" field at all.
    raw = [{"id": 12, "name": "BMW", "isPopular": True}, {"id": 935, "isPopular": False}]
    count = sync.sync_manufacturers(conn, raw)
    assert count == 1
    assert {m.name for m in catalog_repo.list_manufacturers(conn)} == {"BMW"}


def test_sync_manufacturers_updates_name_on_change(conn):
    sync.sync_manufacturers(conn, [{"id": 12, "name": "BMW", "isPopular": True}])
    sync.sync_manufacturers(conn, [{"id": 12, "name": "BMW AG", "isPopular": True}])

    manufacturers = catalog_repo.list_manufacturers(conn)
    assert len(manufacturers) == 1
    assert manufacturers[0].name == "BMW AG"


def test_sync_models_for_manufacturer_deactivates_missing_and_keeps_others(conn):
    sync.sync_manufacturers(conn, [{"id": 12, "name": "BMW", "isPopular": True}])
    manufacturer = catalog_repo.list_manufacturers(conn)[0]

    sync.sync_models_for_manufacturer(
        conn, manufacturer.id, [{"id": 1, "name": "X5"}, {"id": 2, "name": "X6"}]
    )
    sync.sync_models_for_manufacturer(conn, manufacturer.id, [{"id": 1, "name": "X5"}])

    # "Other" is synced in alongside the real models on every run (see
    # test_sync_models_for_manufacturer_adds_other_when_tplge_omits_it) --
    # its fixed external_id means it's never swept up by the deactivation
    # this test is otherwise checking.
    active = {m.name for m in catalog_repo.list_models(conn, manufacturer.id)}
    assert active == {"X5", "Other"}
    all_models = {m.name for m in catalog_repo.list_models(conn, manufacturer.id, active_only=False)}
    assert all_models == {"X5", "X6", "Other"}


def test_sync_models_for_manufacturer_marks_models_synced_at(conn):
    """The never-synced vs. genuinely-empty distinction this whole mechanism
    exists for: models_synced_at must be set even when the manufacturer
    genuinely has zero models, not just when it has some."""
    manufacturer_id = catalog_repo.upsert_manufacturer(conn, external_id=1, name="Other", is_popular=False)
    conn.commit()
    assert catalog_repo.get_manufacturer(conn, manufacturer_id).models_never_synced is True

    sync.sync_models_for_manufacturer(conn, manufacturer_id, [])  # tpl.ge returned genuinely zero real models

    manufacturer = catalog_repo.get_manufacturer(conn, manufacturer_id)
    assert manufacturer.models_never_synced is False
    # even zero real models still gets the synthetic "Other" fallback model
    assert {m.name for m in catalog_repo.list_models(conn, manufacturer_id)} == {"Other"}


def test_sync_models_for_manufacturer_adds_other_when_tplge_omits_it(conn):
    """tpl.ge's own per-manufacturer model lists don't consistently include
    an "Other" row (observed directly, e.g. real VOLKSWAGEN/SCHMITZ data) --
    synced in locally so the "if you can't find your model, pick Other"
    fallback always has something to land on."""
    sync.sync_manufacturers(conn, [{"id": 158, "name": "VOLKSWAGEN", "isPopular": False}])
    manufacturer = catalog_repo.list_manufacturers(conn)[0]

    sync.sync_models_for_manufacturer(
        conn, manufacturer.id, [{"id": 1, "name": "GOLF"}, {"id": 2, "name": "PASSAT"}]
    )

    names = {m.name for m in catalog_repo.list_models(conn, manufacturer.id)}
    assert names == {"GOLF", "PASSAT", "Other"}


def test_sync_models_for_manufacturer_does_not_duplicate_existing_other(conn):
    """If tpl.ge already includes a real "Other" model for a manufacturer,
    a second synthetic one must not be added alongside it."""
    sync.sync_manufacturers(conn, [{"id": 3, "name": "ALFA ROMEO", "isPopular": False}])
    manufacturer = catalog_repo.list_manufacturers(conn)[0]

    sync.sync_models_for_manufacturer(
        conn, manufacturer.id, [{"id": 1, "name": "GIULIA"}, {"id": 2, "name": "Other"}]
    )

    other_rows = [m for m in catalog_repo.list_models(conn, manufacturer.id) if m.name == "Other"]
    assert len(other_rows) == 1


def test_sync_models_on_demand_success(conn, monkeypatch):
    manufacturer_id = catalog_repo.upsert_manufacturer(conn, external_id=3, name="ALFA ROMEO", is_popular=False)
    conn.commit()
    manufacturer = catalog_repo.get_manufacturer(conn, manufacturer_id)
    assert manufacturer.models_never_synced is True

    monkeypatch.setattr(
        sync.tpl_client, "fetch_models", lambda client, ext_id: [{"id": 1, "name": "GIULIA"}, {"id": 2, "name": "Other"}]
    )

    ok = sync.sync_models_on_demand(conn, manufacturer)
    assert ok is True
    assert catalog_repo.get_manufacturer(conn, manufacturer_id).models_never_synced is False
    names = {m.name for m in catalog_repo.list_models(conn, manufacturer_id)}
    assert names == {"GIULIA", "Other"}


def test_sync_models_on_demand_failure_leaves_never_synced(conn, monkeypatch):
    """A transient tpl.ge failure must not get permanently recorded as
    "synced, zero models" — the next attempt (this user retrying, or a
    later batch run) has to try again."""
    manufacturer_id = catalog_repo.upsert_manufacturer(conn, external_id=3, name="ALFA ROMEO", is_popular=False)
    conn.commit()
    manufacturer = catalog_repo.get_manufacturer(conn, manufacturer_id)

    def _boom(client, ext_id):
        raise TplClientError("GET .../models -> 503")

    monkeypatch.setattr(sync.tpl_client, "fetch_models", _boom)

    ok = sync.sync_models_on_demand(conn, manufacturer)
    assert ok is False
    assert catalog_repo.get_manufacturer(conn, manufacturer_id).models_never_synced is True
    assert catalog_repo.list_models(conn, manufacturer_id) == []


def test_run_full_sync_all_models_skips_already_synced_by_default(conn, monkeypatch):
    calls = []

    def fake_categories(client):
        return [{"id": 7, "name": "Passenger Car", "vehiclecategoryIcon": None}]

    def fake_manufacturers(client):
        return [{"id": 12, "name": "BMW", "isPopular": True}, {"id": 2, "name": "AC", "isPopular": False}]

    def fake_models(client, ext_id):
        calls.append(ext_id)
        return [{"id": 1, "name": "Model"}]

    monkeypatch.setattr(sync.tpl_client, "fetch_categories", fake_categories)
    monkeypatch.setattr(sync.tpl_client, "fetch_manufacturers", fake_manufacturers)
    monkeypatch.setattr(sync.tpl_client, "fetch_models", fake_models)
    monkeypatch.setattr(sync.time, "sleep", lambda *_: None)

    fake_client = object()
    sync.run_full_sync(conn, fake_client, all_models=True)
    assert calls == [12, 2]  # first run: nothing synced yet, both are targets

    calls.clear()
    sync.run_full_sync(conn, fake_client, all_models=True)
    assert calls == []  # second run: both already synced, zero requests — the ~1800-request concern


def test_run_full_sync_all_models_force_resyncs_everything(conn, monkeypatch):
    calls = []

    monkeypatch.setattr(sync.tpl_client, "fetch_categories", lambda client: [])
    monkeypatch.setattr(sync.tpl_client, "fetch_manufacturers", lambda client: [{"id": 12, "name": "BMW", "isPopular": True}])
    monkeypatch.setattr(sync.tpl_client, "fetch_models", lambda client, ext_id: (calls.append(ext_id), [])[1])
    monkeypatch.setattr(sync.time, "sleep", lambda *_: None)

    fake_client = object()
    sync.run_full_sync(conn, fake_client, all_models=True)
    sync.run_full_sync(conn, fake_client, all_models=True, force=True)
    assert calls == [12, 12]  # force re-fetched even though already synced


def test_run_full_sync_retries_transient_failures(conn, monkeypatch):
    attempts = {"count": 0}

    def flaky_categories(client):
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise httpx.ConnectTimeout("boom")
        return [{"id": 7, "name": "Passenger Car", "vehiclecategoryIcon": None}]

    monkeypatch.setattr(sync.tpl_client, "fetch_categories", flaky_categories)
    monkeypatch.setattr(sync.tpl_client, "fetch_manufacturers", lambda client: [])
    monkeypatch.setattr(sync.time, "sleep", lambda *_: None)

    report = sync.run_full_sync(conn, object(), all_models=False)
    assert report["categories"] == 1
    assert attempts["count"] == 2  # failed once, succeeded on retry


def test_sync_does_not_create_duplicates_across_repeated_runs(conn):
    raw_categories = [{"id": 7, "name": "Passenger Car", "vehiclecategoryIcon": None}]
    raw_manufacturers = [{"id": 12, "name": "BMW", "isPopular": True}]

    for _ in range(3):
        sync.sync_categories(conn, raw_categories)
        sync.sync_manufacturers(conn, raw_manufacturers)

    assert len(catalog_repo.list_categories(conn, active_only=False)) == 1
    assert len(catalog_repo.list_manufacturers(conn, active_only=False)) == 1
