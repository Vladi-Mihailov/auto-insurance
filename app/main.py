from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.db import init_db
from app.deps import PROJECT_ROOT, SESSION_COOKIE_MAX_AGE, SESSION_COOKIE_NAME, get_settings
from app.web.admin_prices_routes import router as admin_prices_router
from app.web.admin_routes import router as admin_router
from app.web.checkout_routes import router as checkout_router
from app.web.routes import router

app = FastAPI(title="auto-insurance")

_settings = get_settings()
init_db(_settings.app.db_file)


def _cleanup_web_uploads() -> None:
    """Abandoned website uploads / expired documents (best-effort, never raises)."""
    from app.db import get_connection
    from app.orders.web_documents import cleanup_storage

    conn = get_connection(_settings.app.db_file)
    try:
        cleanup_storage(conn, _settings)
    finally:
        conn.close()


_cleanup_web_uploads()


class SessionCookieMiddleware(BaseHTTPMiddleware):
    """Applies the anonymous session cookie to whatever response comes back.

    Route handlers return their own Response objects (TemplateResponse /
    RedirectResponse), which bypasses FastAPI's usual dependency-Response
    cookie merging — see app.deps.get_session_id for why this exists.
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        new_session_id = getattr(request.state, "new_session_id", None)
        if new_session_id:
            response.set_cookie(
                SESSION_COOKIE_NAME,
                new_session_id,
                httponly=True,
                samesite="lax",
                secure=get_settings().app.cookie_secure,
                max_age=SESSION_COOKIE_MAX_AGE,
                path="/",
            )
        return response


app.add_middleware(SessionCookieMiddleware)

app.mount(
    "/static",
    StaticFiles(directory=str(PROJECT_ROOT / "app" / "web" / "static")),
    name="static",
)
app.include_router(router)
app.include_router(checkout_router)
app.include_router(admin_router)
app.include_router(admin_prices_router)
