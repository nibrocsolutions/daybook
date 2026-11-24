"""Daybook application factory."""

from __future__ import annotations

import html
import logging
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from app.config import Settings
from app.db import Base, make_engine, make_session_factory
from app.errors import AuthRedirect, FormExpired, PermissionDenied
from urllib.parse import quote

from app.security import safe_next
from app.seed import seed
from app.web.deps import go
from app.web.rendering import APP_DIR, render
from app.web.routes.admin import router as admin_router
from app.web.routes.auth import router as auth_router
from app.web.routes.customers import router as customers_router
from app.web.routes.dashboard import router as dashboard_router
from app.web.routes.jobs import router as jobs_router
from app.web.routes.payments import router as payments_router
from app.web.routes.webhooks import router as webhooks_router

log = logging.getLogger("daybook")
CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "script-src 'self'; "
    "form-action 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'"
)


class CommitBeforeResponse:
    """Commit the request session before the status line is sent.

    The browser follows a redirect as soon as it sees the headers. Saving
    after the response has already left races that next page and shows the
    old ledger.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                state = scope.get("state")
                db = getattr(state, "db", None) if state is not None else None
                if db is not None and not getattr(state, "db_done", False):
                    if int(message["status"]) < 400:
                        db.commit()
                    else:
                        db.rollback()
                    state.db_done = True
            await send(message)

        await self.app(scope, receive, send_wrapper)


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                path = scope.get("path", "")
                extra = {
                    b"x-content-type-options": b"nosniff",
                    b"x-frame-options": b"DENY",
                    b"referrer-policy": b"same-origin",
                    b"permissions-policy": b"camera=(), microphone=(), geolocation=()",
                    b"content-security-policy": CSP.encode(),
                }
                if not path.startswith("/static"):
                    extra[b"cache-control"] = b"no-store"
                existing = {name.lower() for name, _value in headers}
                for name, value in extra.items():
                    if name not in existing:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    engine = make_engine(settings.database_url)
    init_database(engine, settings.database_url)
    app = FastAPI(title="Daybook", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.engine = engine
    app.state.SessionLocal = make_session_factory(engine)
    seed(app)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CommitBeforeResponse)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="daybook_session",
        max_age=14 * 24 * 60 * 60,
        same_site="lax",
        https_only=settings.secure_cookies,
    )
    app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
    app.include_router(auth_router)
    app.include_router(dashboard_router)
    app.include_router(jobs_router)
    app.include_router(customers_router)
    app.include_router(payments_router)
    app.include_router(admin_router)
    app.include_router(webhooks_router)

    @app.get("/health")
    def health():
        try:
            with app.state.SessionLocal() as db:
                db.execute(text("SELECT 1"))
        except Exception:
            log.error("health check failed")
            return JSONResponse({"ok": False}, status_code=503)
        return {"ok": True}

    @app.get("/robots.txt")
    def robots():
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    @app.exception_handler(AuthRedirect)
    async def auth_redirect(_request: Request, exc: AuthRedirect):
        target = quote(safe_next(exc.next_url), safe="/")
        return go(f"/login?next={target}")

    @app.exception_handler(PermissionDenied)
    async def denied(request: Request, exc: PermissionDenied):
        return await error_page(request, 403, "You need an admin for that", exc.message)

    @app.exception_handler(FormExpired)
    async def form_expired(request: Request, exc: FormExpired):
        return await error_page(request, 403, "That form expired", exc.message)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/webhooks") or request.url.path == "/health":
            detail = exc.detail if isinstance(exc.detail, str) else "error"
            return JSONResponse({"error": detail}, status_code=exc.status_code)
        if exc.status_code == 404:
            return await error_page(request, 404, "That page is not here", "The link may be old, or the record was removed.")
        if exc.status_code >= 500:
            return await error_page(request, exc.status_code, "Something went wrong", "Try that again in a moment.")
        message = exc.detail if isinstance(exc.detail, str) else "That request could not be completed."
        return await error_page(request, exc.status_code, "That request was refused", message)

    @app.exception_handler(RequestValidationError)
    async def invalid_form(request: Request, _exc: RequestValidationError):
        return await error_page(request, 400, "That form could not be read", "Refresh the page and try again.")

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        if settings.env == "development":
            log.exception("request failed")
        else:
            log.error("request failed: %s", type(exc).__name__)
        if request.url.path.startswith("/webhooks"):
            return JSONResponse({"error": "server error"}, status_code=500)
        return await error_page(request, 500, "Something went wrong", "Try that again in a moment.")

    log.info("Daybook ready")
    return app


async def error_page(request: Request, status: int, title: str, message: str) -> Response:
    try:
        db = request.app.state.SessionLocal()
        try:
            return render(request, db, "error.html", status_code=status, title=title, message=message)
        finally:
            db.close()
    except Exception:
        log.error("error page failed")
        safe_title = html.escape(title)
        safe_message = html.escape(message)
        body = (
            "<!doctype html><meta charset='utf-8'><title>"
            + safe_title
            + "</title><h1>"
            + safe_title
            + "</h1><p>"
            + safe_message
            + "</p>"
        )
        return HTMLResponse(body, status_code=status)


def init_database(engine, url: str) -> None:
    import app.models  # noqa: F401
    from sqlalchemy import inspect, text

    attempts = 1 if url.startswith("sqlite") else 20
    for attempt in range(attempts):
        try:
            Base.metadata.create_all(engine)
            _ensure_job_category(engine, inspect, text)
            return
        except OperationalError:
            if attempt == attempts - 1:
                raise
            time.sleep(1)


def _ensure_job_category(engine, inspect, text) -> None:
    """Add kind-of-work on databases created before that column existed."""
    if not inspect(engine).has_table("jobs"):
        return
    names = {column["name"] for column in inspect(engine).get_columns("jobs")}
    if "category" in names:
        return
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE jobs ADD COLUMN category VARCHAR(20) NOT NULL DEFAULT 'other'"))


app = create_app()
