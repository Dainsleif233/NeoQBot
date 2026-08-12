from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security.utils import get_authorization_scheme_param

from . import __version__
from .config import Settings, resolve_secret
from .container import Container, build_container
from .gui import register_gui
from .security import (
    FailureLimiter,
    HostValidationMiddleware,
    RequestBodyLimitMiddleware,
    client_ip_allowed,
    request_appears_secure,
)

logger = logging.getLogger(__name__)


def _verify_onebot_signature(body: bytes, signature: str | None, secret: str) -> bool:
    if not secret:
        return False
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
    """Validate the credentials used by standard OneBot HTTP clients.

    NapCat uses its HTTP-client ``token`` as an HMAC-SHA1 signing key and
    sends the result in ``X-Signature``.  The same OneBot token is also used
    by its HTTP server as a Bearer token.  The bundled sidecar deliberately
    shares this single secret with NeoQBot, so accept either protocol form.
    ``webhook_secret`` remains an optional independent HMAC key for existing
    external adapters; no unauthenticated request is accepted.
    """
    signature_valid = any(
        _verify_onebot_signature(body, signature, secret)
        for secret in dict.fromkeys((webhook_secret, access_token))
        if secret
    )
    scheme, supplied = get_authorization_scheme_param(authorization or "")
    token_valid = (
        bool(access_token)
        and scheme.lower() == "bearer"
        and hmac.compare_digest(supplied, access_token)
    )
    return signature_valid or token_valid


def _onebot_auth_diagnostics(
    authorization: str | None,
    signature: str | None,
    webhook_secret: str,
    access_token: str,
) -> dict[str, object]:
    """Describe a rejected request without ever retaining or logging its secret."""
    scheme, supplied = get_authorization_scheme_param(authorization or "")
    return {
        "authorization_present": bool(authorization),
        "authorization_scheme": scheme.lower() or "none",
        "authorization_credential_present": bool(supplied),
        "signature_present": bool(signature),
        "webhook_secret_configured": bool(webhook_secret),
        "access_token_configured": bool(access_token),
    }


def create_app(settings: Settings | None = None, config_path: str | Path | None = None) -> FastAPI:
    resolved_config_path = Path(config_path or os.getenv("NEOQBOT_CONFIG", "config.yaml"))
    resolved_settings = settings or Settings.load(resolved_config_path)
    security_errors = resolved_settings.deployment_security_errors()
    if security_errors:
        raise RuntimeError("Unsafe NeoQBot deployment configuration: " + "; ".join(security_errors))
    container: Container = build_container(resolved_settings)
    reload_lock = asyncio.Lock()

    async def reload_runtime(updated: Settings) -> list[str]:
        nonlocal container, resolved_settings
        security_errors = updated.deployment_security_errors()
        if security_errors:
            raise ValueError("; ".join(security_errors))
        restart_required: list[str] = []
        if updated.app.host != resolved_settings.app.host:
            restart_required.append("app.host")
        if updated.app.port != resolved_settings.app.port:
            restart_required.append("app.port")
        restart_fields = (
            "allowed_hosts",
            "allow_ip_hosts",
            "forwarded_allow_ips",
            "max_request_body_bytes",
            "expose_api_docs",
        )
        for field in restart_fields:
            if getattr(updated.app, field) != getattr(resolved_settings.app, field):
                restart_required.append(f"app.{field}")
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

    docs_enabled = resolved_settings.app.expose_api_docs
    app = FastAPI(
        title="NeoQBot",
        version=__version__,
        description="Multi-platform bot, group, and knowledge orchestration control plane",
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=resolved_settings.app.max_request_body_bytes,
    )
    app.add_middleware(
        HostValidationMiddleware,
        allowed_hosts=resolved_settings.app.allowed_hosts,
        allow_ip_hosts=resolved_settings.app.allow_ip_hosts,
    )
    app.state.container = container
    admin_failures = FailureLimiter()
    webhook_failures = FailureLimiter()
    webhook_auth_diagnostics = FailureLimiter()
    legacy_webhook_aliases_logged: set[str] = set()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        path = request.url.path
        management_path = path == "/" or path.startswith(
            ("/gui", "/api/gui", "/api/v1", "/docs", "/redoc", "/openapi.json")
        )
        secure_request = request_appears_secure(
            request.url.scheme,
            request.headers.get("host", ""),
            resolved_settings.app.allowed_hosts,
            resolved_settings.app.allow_ip_hosts,
        )
        if management_path and resolved_settings.app.require_https and not secure_request:
            return JSONResponse(
                status_code=426,
                content={"detail": "HTTPS is required for the management interface"},
                headers={"Cache-Control": "no-store"},
            )
        address = request.client.host if request.client else "unknown"
        if management_path and not client_ip_allowed(
            address, resolved_settings.app.management_allowed_networks
        ):
            return JSONResponse(
                status_code=404,
                content={"detail": "Not Found"},
                headers={"Cache-Control": "no-store"},
            )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-src 'none'; object-src 'none'; form-action 'self'; "
            "base-uri 'self'; frame-ancestors 'none'"
        )
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if path.startswith(("/api/", "/gui")):
            response.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    async def require_admin(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        address = request.client.host if request.client else "unknown"
        key = f"admin-api:{address}"
        if admin_failures.blocked(key, 20, 300):
            raise HTTPException(
                status_code=429,
                detail="Too many authentication failures",
                headers={"Retry-After": "300"},
            )
        token = resolve_secret(
            resolved_settings.app.admin_api_token,
            resolved_settings.app.admin_api_token_file,
        )
        if not token:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Admin API is disabled until app.admin_api_token is configured",
            )
        scheme, supplied = get_authorization_scheme_param(authorization or "")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied, token):
            admin_failures.hit(key, 300)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )
        admin_failures.clear(key)

    @app.get("/healthz")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/readyz")
    async def ready(response: Response) -> dict[str, bool]:
        diagnostics = resolved_settings.diagnostics()
        ready_state = not diagnostics["errors"]
        if not ready_state:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"ok": ready_state}

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
        address = request.client.host if request.client else "unknown"
        failure_key = f"webhook:{bot.id}:{address}"
        if webhook_failures.blocked(failure_key, 60, 300):
            raise HTTPException(
                status_code=429,
                detail="Too many webhook authentication failures",
                headers={"Retry-After": "300"},
            )
        body = await request.body()
        access_token = resolve_secret(bot.access_token, bot.access_token_file)
        if not _verify_onebot_auth(
            body, x_signature, authorization, bot.webhook_secret, access_token
        ):
            webhook_failures.hit(failure_key, 300)
            diagnostic_key = f"webhook-auth-diagnostic:{bot.id}:{address}"
            if not webhook_auth_diagnostics.blocked(diagnostic_key, 1, 300):
                webhook_auth_diagnostics.hit(diagnostic_key, 300)
                diagnostic = {
                    **_onebot_auth_diagnostics(
                        authorization,
                        x_signature,
                        bot.webhook_secret,
                        access_token,
                    ),
                    "connection_mode": bot.connection_mode,
                    "access_token_file": bot.access_token_file,
                }
                logger.warning(
                    "Rejected OneBot webhook authentication for bot %s from %s: %s. "
                    "Verify the signing credential; for bundled NapCat, refresh init-volumes "
                    "and restart qq-bridge.",
                    bot.id,
                    address,
                    diagnostic,
                )
                container.database.audit(
                    "onebot_webhook_auth",
                    "rejected",
                    "qq_bot",
                    bot.id,
                    diagnostic,
                )
            raise HTTPException(
                status_code=401,
                detail="Invalid OneBot webhook authentication",
            )
        webhook_failures.clear(failure_key)
        webhook_auth_diagnostics.clear(f"webhook-auth-diagnostic:{bot.id}:{address}")
        try:
            event = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        if not isinstance(event, dict):
            raise HTTPException(status_code=400, detail="Event must be a JSON object")
        group_id = str(event.get("group_id") or "").strip()
        if group_id and resolved_settings.qq_group_assignment(bot.id, group_id) is None:
            logger.debug(
                "Discarded OneBot event from unassigned group %s for bot %s",
                group_id,
                bot.id,
            )
            return {
                "accepted": False,
                "bot_id": bot.id,
                "group_id": group_id,
                "reason": "unmanaged_group",
            }
        try:
            await container.runtime.submit(event, bot.id)
        except asyncio.QueueFull as exc:
            raise HTTPException(status_code=503, detail="Event queue is full") from exc
        return {"accepted": True, "bot_id": bot.id}

    def resolve_onebot_webhook_bot_id(requested_bot_id: str | None = None) -> str:
        if requested_bot_id:
            exact = resolved_settings.qq_bot(requested_bot_id)
            if exact is not None:
                return exact.id
            bundled = resolved_settings.bundled_qq_bot()
            if bundled is not None and requested_bot_id.lower() in {
                "default",
                "bundled",
                "napcat",
                "qq-bridge",
            }:
                alias = requested_bot_id.lower()
                if alias not in legacy_webhook_aliases_logged:
                    legacy_webhook_aliases_logged.add(alias)
                    logger.warning(
                        "Accepted legacy OneBot webhook alias %s for bundled bot %s; "
                        "refresh the NapCat HTTP client configuration",
                        requested_bot_id,
                        bundled.id,
                    )
                    container.database.audit(
                        "onebot_webhook_alias",
                        "accepted",
                        "qq_bot",
                        bundled.id,
                        {"alias": requested_bot_id},
                    )
                return bundled.id
            raise HTTPException(status_code=404, detail="Unknown QQ Bot")
        bot = resolved_settings.bundled_qq_bot() or resolved_settings.qq_bot()
        if bot is None:
            raise HTTPException(status_code=503, detail="No QQ Bot is configured")
        return bot.id

    @app.post("/webhooks/onebot", status_code=status.HTTP_200_OK)
    @app.post("/webhooks/onebot/", status_code=status.HTTP_200_OK, include_in_schema=False)
    async def onebot_webhook(
        request: Request,
        x_signature: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        bot_id = resolve_onebot_webhook_bot_id()
        return await accept_onebot_webhook(bot_id, request, x_signature, authorization)

    @app.post("/webhooks/onebot/{bot_id}", status_code=status.HTTP_200_OK)
    @app.post(
        "/webhooks/onebot/{bot_id}/",
        status_code=status.HTTP_200_OK,
        include_in_schema=False,
    )
    async def onebot_bot_webhook(
        bot_id: str,
        request: Request,
        x_signature: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        resolved_bot_id = resolve_onebot_webhook_bot_id(bot_id)
        return await accept_onebot_webhook(resolved_bot_id, request, x_signature, authorization)

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
            config_path=resolved_config_path,
        )

    return app
