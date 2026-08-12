from app.db import get_connection, init_db


def test_init_db_creates_expected_tables(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)

    tables = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {
        "insurance_sessions",
        "insurance_orders",
        "insurance_order_status_history",
        "insurance_analytics_events",
    } <= tables
    conn.close()


def test_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    init_db(db_path)  # must not raise
