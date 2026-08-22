"""Idempotent catalog sync: tpl.ge -> our local SQLite catalog.

Upserts by external_id (never duplicates), updates names on change, and
deactivates (never physically deletes) rows that disappeared from the source.
Safe to re-run any time — run it again later to pick up new manufacturers/models.

Models are the expensive part (~1800+ manufacturers, one request each), so
there are two ways they get synced:

- Batch (this module's CLI): syncs popular manufacturers every run (cheap,
  7 requests), and for --all-models only targets manufacturers that have
  NEVER been synced (models_synced_at IS NULL) unless --force is given — a
  second `--all-models` run does not redo ~1800 requests for no reason.
- On-demand (sync_models_on_demand, called from app.web.checkout_routes):
  if a user picks a manufacturer whose models were never synced, the
  backend fetches just that one manufacturer's models inline, on the spot,
  with a short timeout. This is what actually guarantees the invariant the
  models picker depends on: an empty model list only ever means "genuinely
  no models for this manufacturer" (models_synced_at is set), never "we
  never checked". The browser still never talks to tpl.ge directly — this
  is our backend calling out, same as the batch path, just synchronously
  inside a request instead of from the CLI.

Run manually:
    python -m app.catalog.sync                 # categories + manufacturers + popular manufacturers' models
    python -m app.catalog.sync --all-models     # ...plus models for every never-synced manufacturer
    python -m app.catalog.sync --all-models --force   # ...and re-sync even already-synced ones
"""

import argparse
import sqlite3
import time
from pathlib import Path

import httpx

from app.catalog import client as tpl_client
from app.catalog import repository as catalog_repo
from app.catalog.models import Manufacturer
from app.db import get_connection, init_db

# Our own stable category codes + Russian labels, mapped onto tpl.ge's
# external category ids (see app.catalog.client module docstring for how
# these ids were found). The ids and the fact that there are exactly these
# six categories come from tpl.ge; the code/name choice is ours.
CATEGORY_CODE_BY_EXTERNAL_ID: dict[int, tuple[str, str]] = {
    7: ("passenger_car", "Легковой"),
    10: ("motorcycle", "Мотоцикл"),
    9: ("bus", "Автобус"),
    8: ("truck", "Грузовик"),
    11: ("trailer", "Прицеп"),
    12: ("special_vehicle", "Спецтехника"),
}

_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 1.5
_BATCH_RATE_LIMIT_SECONDS = 0.2

# tpl.ge's own manufacturer list includes a real "Other" entry (external_id 1),
# but its per-manufacturer model lists don't consistently include one (observed
# directly, e.g. VOLKSWAGEN/SCHMITZ) -- synced in ourselves with a fixed,
# obviously-not-a-real-tpl.ge-id external_id so the model picker's "if your
# model isn't listed, pick Other" fallback always has something to land on,
# and it survives being re-upserted on every future sync of that manufacturer.
_OTHER_MODEL_EXTERNAL_ID = -1


def _fetch_with_retry(fn, *args, **kwargs):
    delay = _RETRY_BASE_DELAY_SECONDS
    last_exc = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return fn(*args, **kwargs)
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < _RETRY_ATTEMPTS - 1:
                time.sleep(delay)
                delay *= 2
    raise last_exc


def sync_categories(conn: sqlite3.Connection, raw_categories: list[dict]) -> int:
    seen_external_ids = []
    for item in raw_categories:
        external_id = item["id"]
        mapped = CATEGORY_CODE_BY_EXTERNAL_ID.get(external_id)
        if mapped is None:
            continue  # unknown category id — don't guess a code, skip rather than invent one
        code, name = mapped
        catalog_repo.upsert_category(
            conn, external_id=external_id, code=code, name=name, icon=item.get("vehiclecategoryIcon")
        )
        seen_external_ids.append(external_id)
    catalog_repo.deactivate_categories_not_in(conn, seen_external_ids)
    conn.commit()
    return len(seen_external_ids)


def sync_manufacturers(conn: sqlite3.Connection, raw_manufacturers: list[dict]) -> int:
    """A handful of tpl.ge's real manufacturer records (~10 of 1856, observed
    directly) have no "name" at all — a data-quality issue on their end, not
    ours to paper over with a guess. Those are skipped rather than upserted
    with an invented name; deactivate_manufacturers_not_in only affects rows
    that actually exist locally, so skipping is always safe."""
    seen_external_ids = []
    for item in raw_manufacturers:
        name = item.get("name")
        if not name:
            continue
        catalog_repo.upsert_manufacturer(conn, external_id=item["id"], name=name, is_popular=bool(item.get("isPopular")))
        seen_external_ids.append(item["id"])
    catalog_repo.deactivate_manufacturers_not_in(conn, seen_external_ids)
    conn.commit()
    return len(seen_external_ids)


def sync_models_for_manufacturer(conn: sqlite3.Connection, manufacturer_id: int, raw_models: list[dict]) -> int:
    seen_external_ids = []
    has_other = False
    for item in raw_models:
        name = item.get("name")
        if not name:
            continue
        catalog_repo.upsert_model(conn, external_id=item["id"], manufacturer_id=manufacturer_id, name=name)
        seen_external_ids.append(item["id"])
        if name == "Other":
            has_other = True
    if not has_other:
        catalog_repo.upsert_model(
            conn, external_id=_OTHER_MODEL_EXTERNAL_ID, manufacturer_id=manufacturer_id, name="Other"
        )
        seen_external_ids.append(_OTHER_MODEL_EXTERNAL_ID)
    catalog_repo.deactivate_models_not_in(conn, manufacturer_id, seen_external_ids)
    catalog_repo.mark_models_synced(conn, manufacturer_id)
    conn.commit()
    return len(seen_external_ids)


def sync_models_on_demand(conn: sqlite3.Connection, manufacturer: Manufacturer, *, timeout: float = 8.0) -> bool:
    """Called from the request path (see app.web.checkout_routes) the first
    time a user picks a manufacturer whose models were never batch-synced.
    Short timeout + a single attempt (no retries) since this runs inline in
    a user-facing request — on failure, models_synced_at is left unset so
    the next attempt (this user retrying, or a later batch sync) tries
    again rather than the manufacturer looking permanently model-less.
    """
    try:
        with tpl_client.new_client(timeout=timeout) as client:
            raw_models = tpl_client.fetch_models(client, manufacturer.external_id)
    except httpx.HTTPError:
        return False
    sync_models_for_manufacturer(conn, manufacturer.id, raw_models)
    return True


def run_full_sync(conn: sqlite3.Connection, client: httpx.Client, *, all_models: bool = False, force: bool = False) -> dict:
    report = {}

    report["categories"] = sync_categories(conn, _fetch_with_retry(tpl_client.fetch_categories, client))
    report["manufacturers"] = sync_manufacturers(conn, _fetch_with_retry(tpl_client.fetch_manufacturers, client))

    manufacturers = catalog_repo.list_manufacturers(conn)
    if not all_models:
        targets = [m for m in manufacturers if m.is_popular]
    elif force:
        targets = manufacturers
    else:
        targets = [m for m in manufacturers if m.models_never_synced]

    model_counts = {}
    failures = []
    for manufacturer in targets:
        try:
            raw_models = _fetch_with_retry(tpl_client.fetch_models, client, manufacturer.external_id)
        except httpx.HTTPError as exc:
            failures.append(manufacturer.name)
            continue
        model_counts[manufacturer.name] = sync_models_for_manufacturer(conn, manufacturer.id, raw_models)
        time.sleep(_BATCH_RATE_LIMIT_SECONDS)

    report["models_by_manufacturer"] = model_counts
    report["model_sync_failures"] = failures
    report["manufacturers_synced_for_models"] = len(targets)
    report["manufacturers_skipped_for_models"] = len(manufacturers) - len(targets)

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--all-models",
        action="store_true",
        help="Also sync models for manufacturers that have never had their models synced.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="With --all-models, re-sync every manufacturer's models, including already-synced ones.",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent.parent
    from app.deps import get_settings  # local import: avoid pulling in the web layer at module load time

    settings = get_settings()
    init_db(settings.app.db_file)
    conn = get_connection(settings.app.db_file)
    try:
        with tpl_client.new_client() as client:
            report = run_full_sync(conn, client, all_models=args.all_models, force=args.force)
    finally:
        conn.close()

    print(f"Categories synced: {report['categories']}")
    print(f"Manufacturers synced: {report['manufacturers']}")
    print(
        f"Models synced for {report['manufacturers_synced_for_models']} manufacturer(s) "
        f"({report['manufacturers_skipped_for_models']} skipped — pass --all-models to include never-synced ones):"
    )
    for name, count in report["models_by_manufacturer"].items():
        print(f"  {name}: {count} models")
    if report["model_sync_failures"]:
        print(f"Failed after retries (still unsynced, will retry next run): {report['model_sync_failures']}")


if __name__ == "__main__":
    main()
