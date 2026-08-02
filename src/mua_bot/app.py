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
from .config import Settings, resolve_secret
from .container import Container, build_container
from .gui import register_gui


def _verify_onebot_signature(body: bytes, signature: str | None, secret: str) -> bool:
    if not secret:
        return True
    if not signature:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(signature, f"sha1={digest}")


def _verify_onebot_auth(
    body: bytes,
    signature: str | None,
    authorization: str | None,
    webhook_secret: str,
    access_token: str,
) -> bool:
    if webhook_secret:
        return _verify_onebot_signature(body, signature, webhook_secret)
    if access_token:
        supplied = authorization.removeprefix("Bearer ").strip() if authorization else ""
        return hmac.compare_digest(supplied, access_token)
    return True


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

    async def accept_onebot_webhook(
        bot_id: str,
        request: Request,
        x_signature: str | None,
        authorization: str | None,
    ) -> dict[str, object]:
        bot = resolved_settings.qq_bot(bot_id)
        if bot is None:
            raise HTTPException(status_code=404, detail="Unknown QQ Bot")
        if not bot.enabled:
            raise HTTPException(status_code=503, detail="QQ Bot is disabled")
        body = await request.body()
        if len(body) > 2 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Event body too large")
        access_token = resolve_secret(bot.access_token, bot.access_token_file)
        if not _verify_onebot_auth(
            body, x_signature, authorization, bot.webhook_secret, access_token
        ):
            raise HTTPException(status_code=401, detail="Invalid OneBot webhook authentication")
        try:
            event = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        if not isinstance(event, dict):
            raise HTTPException(status_code=400, detail="Event must be a JSON object")
        try:
            await container.runtime.submit(event, bot.id)
        except asyncio.QueueFull as exc:
            raise HTTPException(status_code=503, detail="Event queue is full") from exc
        return {"accepted": True, "bot_id": bot.id}

    @app.post("/webhooks/onebot", status_code=status.HTTP_202_ACCEPTED)
    async def onebot_webhook(
        request: Request,
        x_signature: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        bot = resolved_settings.qq_bot()
        if bot is None:
            raise HTTPException(status_code=503, detail="No QQ Bot is configured")
        return await accept_onebot_webhook(bot.id, request, x_signature, authorization)

    @app.post("/webhooks/onebot/{bot_id}", status_code=status.HTTP_202_ACCEPTED)
    async def onebot_bot_webhook(
        bot_id: str,
        request: Request,
        x_signature: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        return await accept_onebot_webhook(bot_id, request, x_signature, authorization)

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
