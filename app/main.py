"""FastAPI application factory + Socket.IO mount.

Run with ``python run.py`` (HTTP for this device + HTTPS for the LAN), or
``uvicorn app.main:asgi --host 0.0.0.0 --port 8700``.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
from contextlib import asynccontextmanager

import socketio
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.ai.tools import load_all as load_tools
from app.config import ROOT, settings
from app.core import hooks
from app.core.jobs import jobs
from app.core.logs import setup_logging
from app.core.sockets import sio
from app.core.state import close_http, redis
from app.db import SessionLocal, engine, init_db
from app.security.middleware import IdempotencyMiddleware, WebSecurityMiddleware
from app.security.ratelimit import limiter

setup_logging()
log = logging.getLogger("deminic")

# Windows' MIME registry is unreliable (e.g. .webp unknown, .js sometimes text/plain,
# which breaks ES-module loading) - pin the types static files are served with.
import mimetypes  # noqa: E402

for _ext, _type in ((".js", "text/javascript"), (".mjs", "text/javascript"), (".css", "text/css"), (".svg", "image/svg+xml"),
                    (".webp", "image/webp"), (".woff2", "font/woff2"), (".json", "application/json"), (".ico", "image/x-icon"),
                    (".mp4", "video/mp4"), (".webm", "video/webm")):
    mimetypes.add_type(_type, _ext)

FEATURE_ROUTERS = ("pages", "auth", "account", "chats", "jobs", "personal", "business", "coding", "status")


async def _sync_sentinel() -> None:
    from sqlalchemy import select

    from app.models import User
    from app.security import sentinel

    async with SessionLocal() as db:
        rows = (await db.execute(select(User.email, User.public_id))).all()
    for email, pid in rows:
        await sentinel.register_account(email, pid)


async def _maintenance_loop() -> None:
    from app.core import vectors
    from app.services.memory_service import purge_soft_deleted

    while True:
        await asyncio.sleep(60)
        try:
            async with SessionLocal() as db:
                n = await vectors.reembed_hash_rows(db)
                if n:
                    log.info("re-embedded %d fallback vectors with the primary model", n)
            await purge_soft_deleted()
            await hooks.run_maintenance()
        except Exception:  # noqa: BLE001
            log.exception("maintenance cycle failed")


def _quiet_windows_resets() -> None:
    """Windows' Proactor loop logs a traceback whenever a client drops a socket
    abruptly (WinError 10054) - harmless noise; everything else is still logged."""
    loop = asyncio.get_running_loop()

    def handler(lp, context):  # noqa: ANN001
        if isinstance(context.get("exception"), (ConnectionResetError, ConnectionAbortedError)):
            return
        lp.default_exception_handler(context)

    loop.set_exception_handler(handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _quiet_windows_resets()
    log.info("starting Deminic ...")
    await init_db()
    load_tools()
    from app.core import intents

    async with SessionLocal() as db:
        await intents.seed_all(db)
    await _sync_sentinel()
    import os

    recovered = 0 if os.environ.get("DEMINIC_SKIP_RECOVERY") == "1" else await jobs.recover_on_startup()
    if recovered:
        log.warning("recovered %d background task(s) interrupted by the previous shutdown", recovered)
    jobs.start_watchdog()
    await hooks.run_startup()
    maint = asyncio.create_task(_maintenance_loop(), name="maintenance")
    log.info("Deminic ready")
    try:
        yield
    finally:
        log.info("shutting down: cancelling cancellable tasks, reverting protected ones to their restore points")
        maint.cancel()
        await jobs.shutdown()
        await hooks.run_shutdown()
        await close_http()
        await redis.aclose()
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="Deminic Agentic AI", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.limiter = limiter

    # Middleware: last added = outermost.
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(IdempotencyMiddleware)
    app.add_middleware(WebSecurityMiddleware)

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limited(request: Request, exc: RateLimitExceeded):
        return JSONResponse({"detail": f"Rate limit exceeded ({exc.detail}) - slow down a little."}, status_code=429)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        errs = [{"field": ".".join(str(x) for x in e.get("loc", [])[1:]), "message": e.get("msg")} for e in exc.errors()]
        return JSONResponse({"detail": errs[0]["message"] if errs else "Invalid request", "errors": errs}, status_code=422)

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            return JSONResponse({"detail": detail.get("message", "error"), **detail}, status_code=exc.status_code,
                                headers=exc.headers)
        if request.url.path.startswith("/api/") or exc.status_code != 404:
            return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=exc.headers)
        from app.templating import render

        return render(request, "404.html", {"theme": "system", "user": None}, status_code=404)

    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
    (settings.uploads_dir / "avatars").mkdir(parents=True, exist_ok=True)
    app.mount("/media/avatars", StaticFiles(directory=str(settings.uploads_dir / "avatars")), name="avatars")

    for name in FEATURE_ROUTERS:
        mod = importlib.import_module(f"app.routers.{name}")
        app.include_router(mod.router)
    return app


app = create_app()
asgi = socketio.ASGIApp(sio, other_asgi_app=app, socketio_path="socket.io")
