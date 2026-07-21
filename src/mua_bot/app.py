from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from . import __version__
from .config import Settings
from .container import Container, build_container
from .gui import register_gui


def _verify_onebot_signature(body: bytes, signature: str | None, secret: str) -> bool:
    if not secret:
        return True
    if not signature:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(signature, f"sha1={digest}")


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings.load(os.getenv("MUA_CONFIG", "config.yaml"))
    container: Container = build_container(resolved_settings)
    reload_lock = asyncio.Lock()

    async def reload_runtime(updated: Settings) -> list[str]:
        nonlocal container, resolved_settings
        restart_required: list[str] = []
        if updated.app.host != resolved_settings.app.host:
            restart_required.append("app.host")
        if updated.app.port != resolved_settings.app.port:
            restart_required.append("app.port")
        async with reload_lock:
            replacement = build_container(updated)
            await replacement.runtime.start()
            previous = container
            container = replacement
            resolved_settings = updated
            app.state.container = replacement
            await previous.close()
        return restart_required

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await container.runtime.start()
        try:
            yield
        finally:
            await container.close()

    app = FastAPI(
        title="MUA-Bot",
        version=__version__,
        description="QQ group governance and Feishu archive agent",
        lifespan=lifespan,
    )
    app.state.container = container

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-src http: https:; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    async def require_admin(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        token = resolved_settings.app.admin_api_token
        if not token:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Admin API is disabled until app.admin_api_token is configured",
            )
        supplied = authorization.removeprefix("Bearer ") if authorization else ""
        if not hmac.compare_digest(supplied, token):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    @app.get("/healthz")
    async def health() -> dict[str, object]:
        return {"ok": True, "version": __version__, "queue_size": container.runtime.queue.qsize()}

    @app.get("/readyz")
    async def ready() -> dict[str, object]:
        diagnostics = resolved_settings.diagnostics()
        return {
            "ok": not diagnostics["errors"],
            "database": container.database.counts(),
            "diagnostics": diagnostics,
        }

    @app.post("/webhooks/onebot", status_code=status.HTTP_202_ACCEPTED)
    async def onebot_webhook(
        request: Request,
        x_signature: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        body = await request.body()
        if len(body) > 2 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Event body too large")
        if not _verify_onebot_signature(body, x_signature, resolved_settings.qq.webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid OneBot signature")
        try:
            event = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        if not isinstance(event, dict):
            raise HTTPException(status_code=400, detail="Event must be a JSON object")
        try:
            await container.runtime.submit(event)
        except asyncio.QueueFull as exc:
            raise HTTPException(status_code=503, detail="Event queue is full") from exc
        return {"accepted": True}

    @app.get("/api/v1/status", dependencies=[Depends(require_admin)])
    async def api_status() -> dict[str, object]:
        return {
            "version": __version__,
            "database": container.database.counts(),
            "queue_size": container.runtime.queue.qsize(),
            "config": resolved_settings.redacted_dict(),
        }

    @app.post("/api/v1/jobs/moderation", dependencies=[Depends(require_admin)])
    async def run_moderation() -> dict[str, object]:
        return {"result": await container.runtime.run_all_moderation()}

    @app.post("/api/v1/jobs/announcements", dependencies=[Depends(require_admin)])
    async def sync_announcements() -> dict[str, object]:
        return {"result": await container.runtime.sync_all_announcements()}

    @app.post("/api/v1/jobs/maintenance", dependencies=[Depends(require_admin)])
    async def run_maintenance() -> dict[str, object]:
        return {"result": await container.runtime.run_maintenance()}

    if resolved_settings.gui.enabled:
        register_gui(
            app,
            get_container=lambda: container,
            get_settings=lambda: resolved_settings,
            reload_settings=reload_runtime,
        )

    return app
