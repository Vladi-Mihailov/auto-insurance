from app.analytics.repository import log_event
from app.db import get_connection, init_db


def test_log_event_persists_row(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)

    log_event(conn, session_id="sess-1", order_id=None, event_name="landing_view")
    log_event(
        conn,
        session_id="sess-1",
        order_id=5,
        event_name="period_selected",
        properties={"period_code": "14d"},
    )

    rows = conn.execute("SELECT * FROM insurance_analytics_events ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0]["event_name"] == "landing_view"
    assert rows[0]["order_id"] is None
    assert rows[1]["order_id"] == 5
    assert "14d" in rows[1]["properties"]
    conn.close()
