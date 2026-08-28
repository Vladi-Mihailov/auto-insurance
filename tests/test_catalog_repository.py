import pytest

from app.catalog import repository as catalog_repo
from app.db import get_connection, init_db


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    connection = get_connection(db_path)
    yield connection
    connection.close()


def test_upsert_category_is_idempotent(conn):
    catalog_repo.upsert_category(conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
    catalog_repo.upsert_category(conn, external_id=7, code="passenger_car", name="Легковой (обновлено)", icon="vehicle")
    conn.commit()

    categories = catalog_repo.list_categories(conn)
    assert len(categories) == 1
    assert categories[0].name == "Легковой (обновлено)"


def test_deactivate_categories_not_in(conn):
    catalog_repo.upsert_category(conn, external_id=7, code="passenger_car", name="Легковой", icon=None)
    catalog_repo.upsert_category(conn, external_id=10, code="motorcycle", name="Мотоцикл", icon=None)
    conn.commit()

    catalog_repo.deactivate_categories_not_in(conn, [7])
    conn.commit()

    active_codes = {c.code for c in catalog_repo.list_categories(conn)}
    assert active_codes == {"passenger_car"}
    all_codes = {c.code for c in catalog_repo.list_categories(conn, active_only=False)}
    assert all_codes == {"passenger_car", "motorcycle"}


def test_get_category_by_code_missing_returns_none(conn):
    assert catalog_repo.get_category_by_code(conn, "does-not-exist") is None


def test_deactivate_categories_not_in_with_empty_list_deactivates_all(conn):
    """Regression test: `x NOT IN ()` has no valid SQL spelling, and `NOT IN
    (NULL)` is always unknown/false — passing an empty list used to
    silently deactivate nothing instead of everything."""
    catalog_repo.upsert_category(conn, external_id=7, code="passenger_car", name="Легковой", icon=None)
    conn.commit()

    catalog_repo.deactivate_categories_not_in(conn, [])
    conn.commit()

    assert catalog_repo.list_categories(conn) == []
    assert len(catalog_repo.list_categories(conn, active_only=False)) == 1


def test_list_categories_allowed_codes_none_is_unrestricted(conn):
    """None (the default) must behave exactly as before this param existed
    -- this is what keeps Georgia's category list untouched."""
    catalog_repo.upsert_category(conn, external_id=7, code="passenger_car", name="Легковой", icon=None)
    catalog_repo.upsert_category(conn, external_id=10, code="motorcycle", name="Мотоцикл", icon=None)
    conn.commit()

    codes = {c.code for c in catalog_repo.list_categories(conn, allowed_codes=None)}
    assert codes == {"passenger_car", "motorcycle"}


def test_list_categories_allowed_codes_filters_to_the_given_set(conn):
    catalog_repo.upsert_category(conn, external_id=7, code="passenger_car", name="Легковой", icon=None)
    catalog_repo.upsert_category(conn, external_id=10, code="motorcycle", name="Мотоцикл", icon=None)
    conn.commit()

    categories = catalog_repo.list_categories(conn, allowed_codes=["passenger_car"])
    assert [c.code for c in categories] == ["passenger_car"]


def test_list_categories_allowed_codes_empty_list_returns_nothing(conn):
    """An explicit empty list is a real "nothing enabled" state, distinct
    from None (unrestricted) -- and must not hit the invalid `code IN ()`
    SQL some other list-filtering helpers in this module have to guard
    against (see deactivate_categories_not_in)."""
    catalog_repo.upsert_category(conn, external_id=7, code="passenger_car", name="Легковой", icon=None)
    conn.commit()
    assert catalog_repo.list_categories(conn, allowed_codes=[]) == []


def test_list_categories_allowed_codes_still_respects_active_only(conn):
    catalog_repo.upsert_category(conn, external_id=7, code="passenger_car", name="Легковой", icon=None)
    conn.commit()
    catalog_repo.deactivate_categories_not_in(conn, [])
    conn.commit()

    assert catalog_repo.list_categories(conn, allowed_codes=["passenger_car"]) == []
    assert len(catalog_repo.list_categories(conn, allowed_codes=["passenger_car"], active_only=False)) == 1


def test_deactivate_manufacturers_not_in_with_empty_list_deactivates_all(conn):
    catalog_repo.upsert_manufacturer(conn, external_id=12, name="BMW", is_popular=True)
    conn.commit()

    catalog_repo.deactivate_manufacturers_not_in(conn, [])
    conn.commit()

    assert catalog_repo.list_manufacturers(conn) == []


def test_deactivate_models_not_in_with_empty_list_deactivates_all_for_that_manufacturer(conn):
    manufacturer_id = catalog_repo.upsert_manufacturer(conn, external_id=12, name="BMW", is_popular=True)
    catalog_repo.upsert_model(conn, external_id=1, manufacturer_id=manufacturer_id, name="X5")
    conn.commit()

    catalog_repo.deactivate_models_not_in(conn, manufacturer_id, [])
    conn.commit()

    assert catalog_repo.list_models(conn, manufacturer_id) == []


def test_upsert_manufacturer_idempotent_and_returns_stable_internal_id(conn):
    first_id = catalog_repo.upsert_manufacturer(conn, external_id=12, name="BMW", is_popular=True)
    second_id = catalog_repo.upsert_manufacturer(conn, external_id=12, name="BMW", is_popular=True)
    conn.commit()
    assert first_id == second_id


def test_list_manufacturers_orders_popular_first(conn):
    catalog_repo.upsert_manufacturer(conn, external_id=2, name="AC", is_popular=False)
    catalog_repo.upsert_manufacturer(conn, external_id=12, name="BMW", is_popular=True)
    conn.commit()

    names = [m.name for m in catalog_repo.list_manufacturers(conn)]
    assert names[0] == "BMW"


def test_search_manufacturers_matches_substring(conn):
    catalog_repo.upsert_manufacturer(conn, external_id=7, name="AUDI", is_popular=False)
    catalog_repo.upsert_manufacturer(conn, external_id=12, name="BMW", is_popular=True)
    conn.commit()

    results = catalog_repo.search_manufacturers(conn, "bm")
    assert [m.name for m in results] == ["BMW"]


def test_deactivate_manufacturers_not_in(conn):
    catalog_repo.upsert_manufacturer(conn, external_id=12, name="BMW", is_popular=True)
    catalog_repo.upsert_manufacturer(conn, external_id=2, name="AC", is_popular=False)
    conn.commit()

    catalog_repo.deactivate_manufacturers_not_in(conn, [12])
    conn.commit()

    active_names = {m.name for m in catalog_repo.list_manufacturers(conn)}
    assert active_names == {"BMW"}


def test_upsert_model_scoped_to_manufacturer(conn):
    manufacturer_id = catalog_repo.upsert_manufacturer(conn, external_id=12, name="BMW", is_popular=True)
    conn.commit()

    model_id = catalog_repo.upsert_model(conn, external_id=361, manufacturer_id=manufacturer_id, name="730 LD")
    conn.commit()

    model = catalog_repo.get_model(conn, model_id)
    assert model is not None
    assert model.manufacturer_id == manufacturer_id
    assert model.name == "730 LD"


def test_list_models_only_returns_models_for_that_manufacturer(conn):
    bmw_id = catalog_repo.upsert_manufacturer(conn, external_id=12, name="BMW", is_popular=True)
    audi_id = catalog_repo.upsert_manufacturer(conn, external_id=7, name="AUDI", is_popular=False)
    catalog_repo.upsert_model(conn, external_id=1, manufacturer_id=bmw_id, name="X5")
    catalog_repo.upsert_model(conn, external_id=2, manufacturer_id=audi_id, name="A4")
    conn.commit()

    bmw_models = catalog_repo.list_models(conn, bmw_id)
    assert [m.name for m in bmw_models] == ["X5"]


def test_deactivate_models_not_in_only_touches_that_manufacturer(conn):
    bmw_id = catalog_repo.upsert_manufacturer(conn, external_id=12, name="BMW", is_popular=True)
    audi_id = catalog_repo.upsert_manufacturer(conn, external_id=7, name="AUDI", is_popular=False)
    catalog_repo.upsert_model(conn, external_id=1, manufacturer_id=bmw_id, name="X5")
    catalog_repo.upsert_model(conn, external_id=2, manufacturer_id=bmw_id, name="X6")
    catalog_repo.upsert_model(conn, external_id=3, manufacturer_id=audi_id, name="A4")
    conn.commit()

    catalog_repo.deactivate_models_not_in(conn, bmw_id, [1])
    conn.commit()

    bmw_active = {m.name for m in catalog_repo.list_models(conn, bmw_id)}
    assert bmw_active == {"X5"}
    audi_active = {m.name for m in catalog_repo.list_models(conn, audi_id)}
    assert audi_active == {"A4"}  # untouched by BMW's deactivation
