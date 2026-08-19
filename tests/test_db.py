import asyncio

import httpx

from app.db import get_connection, init_db


def test_concurrent_requests_do_not_hit_sqlite_cross_thread_error():
    """Regression test for sqlite3.ProgrammingError: "SQLite objects created
    in a thread can only be used in that same thread."

    Root cause: every sync dependency in a request's chain (get_db,
    get_session_id, ...) and the sync endpoint function itself are each
    individually dispatched via FastAPI's `run_in_threadpool` ->
    `anyio.to_thread.run_sync`, which hands the work to WHICHEVER worker
    thread is currently idle in a shared pool -- not necessarily the same
    thread that ran the previous dependency in the SAME request. Under real
    concurrent load (several requests in flight at once, competing for the
    same idle-worker pool), the connection get_db() creates in one thread
    routinely gets touched (e.g. by log_event) from a different thread a
    moment later. This only needs enough simultaneous in-flight requests to
    force more than one worker thread into play; a single sequential
    request never triggers it, which is why casual manual testing missed it.

    Reproduced here with many genuinely concurrent requests sharing ONE
    asyncio event loop (matching real uvicorn: one loop, one shared anyio
    worker pool) via httpx's ASGI transport + asyncio.gather -- a separate
    TestClient per thread does NOT reproduce this, since each gets its own
    isolated event loop/worker pool.
    """
    from app.main import app

    async def _hammer():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async def hit():
                try:
                    response = await client.get("/")
                    return response.status_code
                except Exception as exc:  # noqa: BLE001 -- capturing for the assertion below
                    return repr(exc)

            return await asyncio.gather(*[hit() for _ in range(40)])

    results = asyncio.run(_hammer())
    failures = [r for r in results if r != 200]
    assert not failures, f"concurrent requests failed: {failures[:5]}"


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
