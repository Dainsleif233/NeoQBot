from __future__ import annotations

import asyncio
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .auth import GuiSession
from .config import Settings
from .container import Container


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)


class PasswordPayload(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=10, max_length=512)


class SettingsPayload(BaseModel):
    config: dict[str, Any]


class LoginThrottle:
    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}

    def allowed(self, address: str) -> bool:
        now = time.monotonic()
        recent = [stamp for stamp in self._failures.get(address, []) if now - stamp < 300]
        self._failures[address] = recent
        return len(recent) < 8

    def fail(self, address: str) -> None:
        self._failures.setdefault(address, []).append(time.monotonic())

    def clear(self, address: str) -> None:
        self._failures.pop(address, None)


def _preserve_masked_secrets(candidate: dict[str, Any], current: Settings) -> dict[str, Any]:
    secret_values = {
        ("app", "admin_api_token"): current.app.admin_api_token,
        ("qq", "access_token"): current.qq.access_token,
        ("qq", "webhook_secret"): current.qq.webhook_secret,
        ("llm", "api_key"): current.llm.api_key,
        ("gui", "bootstrap_password"): current.gui.bootstrap_password,
    }
    for (section, key), original in secret_values.items():
        section_value = candidate.setdefault(section, {})
        if not isinstance(section_value, dict):
            continue
        if section_value.get(key) in (None, "", "***"):
            section_value[key] = original
    feishu = candidate.setdefault("feishu", {})
    if isinstance(feishu, dict):
        incoming_environment = feishu.get("extra_environment")
        if incoming_environment in (None, "***"):
            feishu["extra_environment"] = current.feishu.extra_environment
        elif isinstance(incoming_environment, dict):
            for key, value in list(incoming_environment.items()):
                if value in (None, "", "***") and key in current.feishu.extra_environment:
                    incoming_environment[key] = current.feishu.extra_environment[key]
    app_section = candidate.setdefault("app", {})
    if not isinstance(app_section, dict):
        raise ValueError("app 配置必须是对象")
    app_section["database_path"] = current.app.database_path
    return candidate


def register_gui(
    app: FastAPI,
    get_container: Callable[[], Container],
    get_settings: Callable[[], Settings],
    reload_settings: Callable[[Settings], Awaitable[list[str]]],
) -> None:
    web_root = Path(__file__).with_name("web")
    app.mount("/gui/assets", StaticFiles(directory=web_root), name="gui-assets")
    throttle = LoginThrottle()

    def current_session(
        mua_session: Annotated[str | None, Cookie(alias="mua_session")] = None,
    ) -> GuiSession:
        session = get_container().auth.session(mua_session)
        if session is None:
            raise HTTPException(status_code=401, detail="请先登录")
        return session

    def csrf_session(
        session: Annotated[GuiSession, Depends(current_session)],
        x_csrf_token: Annotated[str | None, Header()] = None,
    ) -> GuiSession:
        if not x_csrf_token or not secrets.compare_digest(x_csrf_token, session.csrf_token):
            raise HTTPException(status_code=403, detail="CSRF 校验失败")
        return session

    def ready_admin(
        session: Annotated[GuiSession, Depends(current_session)],
    ) -> GuiSession:
        if session.must_change_password:
            raise HTTPException(status_code=403, detail="首次登录必须先修改默认密码")
        return session

    def ready_admin_csrf(
        session: Annotated[GuiSession, Depends(csrf_session)],
    ) -> GuiSession:
        if session.must_change_password:
            raise HTTPException(status_code=403, detail="首次登录必须先修改默认密码")
        return session

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/gui/")

    @app.get("/gui", include_in_schema=False)
    async def gui_redirect() -> RedirectResponse:
        return RedirectResponse("/gui/")

    @app.get("/gui/", include_in_schema=False)
    async def gui_index() -> FileResponse:
        return FileResponse(web_root / "index.html")

    @app.post("/api/gui/auth/login")
    async def gui_login(
        payload: LoginPayload, request: Request, response: Response
    ) -> dict[str, Any]:
        address = request.client.host if request.client else "unknown"
        if not throttle.allowed(address):
            raise HTTPException(status_code=429, detail="登录失败次数过多，请五分钟后再试")
        result = await asyncio.to_thread(
            get_container().auth.login, payload.username, payload.password
        )
        if result is None:
            throttle.fail(address)
            get_container().database.audit(
                "gui_login", "failed", "admin_user", payload.username, {"address": address}
            )
            await asyncio.sleep(0.25)
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        throttle.clear(address)
        token, session = result
        settings = get_settings()
        response.set_cookie(
            "mua_session",
            token,
            max_age=settings.gui.session_hours * 3600,
            httponly=True,
            secure=settings.gui.secure_cookie,
            samesite="strict",
            path="/",
        )
        get_container().database.audit(
            "gui_login", "success", "admin_user", session.username, {"address": address}
        )
        return {
            "username": session.username,
            "csrf_token": session.csrf_token,
            "must_change_password": session.must_change_password,
        }

    @app.get("/api/gui/auth/session")
    async def gui_session(
        session: Annotated[GuiSession, Depends(current_session)],
    ) -> dict[str, Any]:
        return {
            "username": session.username,
            "csrf_token": session.csrf_token,
            "must_change_password": session.must_change_password,
        }

    @app.post("/api/gui/auth/logout")
    async def gui_logout(
        response: Response,
        _: Annotated[GuiSession, Depends(csrf_session)],
        mua_session: Annotated[str | None, Cookie(alias="mua_session")] = None,
    ) -> dict[str, bool]:
        get_container().auth.logout(mua_session)
        response.delete_cookie("mua_session", path="/")
        return {"ok": True}

    @app.post("/api/gui/auth/password")
    async def gui_change_password(
        payload: PasswordPayload,
        session: Annotated[GuiSession, Depends(csrf_session)],
    ) -> dict[str, bool]:
        try:
            changed = await asyncio.to_thread(
                get_container().auth.change_password,
                session,
                payload.current_password,
                payload.new_password,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not changed:
            raise HTTPException(status_code=400, detail="当前密码错误")
        get_container().database.audit("gui_password", "changed", "admin_user", session.username)
        return {"ok": True}

    @app.get("/api/gui/dashboard")
    async def gui_dashboard(
        _: Annotated[GuiSession, Depends(ready_admin)],
    ) -> dict[str, Any]:
        settings = get_settings()
        return {
            "version": __version__,
            "environment": settings.app.environment,
            "dry_run": settings.app.dry_run,
            "queue_size": get_container().runtime.queue.qsize(),
            "counts": get_container().database.counts(),
            "diagnostics": settings.diagnostics(),
            "managed_groups": settings.qq.managed_group_ids,
        }

    @app.get("/api/gui/settings")
    async def gui_settings(
        _: Annotated[GuiSession, Depends(ready_admin)],
    ) -> dict[str, Any]:
        return {"config": get_settings().redacted_dict()}

    @app.put("/api/gui/settings")
    async def gui_save_settings(
        payload: SettingsPayload,
        _: Annotated[GuiSession, Depends(ready_admin_csrf)],
    ) -> dict[str, Any]:
        current = get_settings()
        try:
            merged = _preserve_masked_secrets(payload.config, current)
            updated = Settings.model_validate(merged)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        config_path = os.getenv("MUA_CONFIG", "config.yaml")
        try:
            updated.save(config_path)
            restart_required = await reload_settings(updated)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"配置文件写入失败：{exc}") from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"配置已写入，但热加载失败：{exc}") from exc
        get_container().database.audit(
            "gui_settings", "saved", "configuration", config_path, {"restart": restart_required}
        )
        return {
            "ok": True,
            "restart_required": restart_required,
            "diagnostics": updated.diagnostics(),
        }

    @app.get("/api/gui/records/{kind}")
    async def gui_records(
        kind: str,
        _: Annotated[GuiSession, Depends(ready_admin)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        try:
            records = get_container().database.recent_records(kind, limit)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"kind": kind, "records": records}

    @app.post("/api/gui/jobs/{job}")
    async def gui_run_job(
        job: str,
        _: Annotated[GuiSession, Depends(ready_admin_csrf)],
    ) -> dict[str, Any]:
        runtime = get_container().runtime
        actions = {
            "moderation": runtime.run_all_moderation,
            "announcements": runtime.sync_all_announcements,
            "maintenance": runtime.run_maintenance,
        }
        action = actions.get(job)
        if action is None:
            raise HTTPException(status_code=404, detail="未知任务")
        result = await action()
        return {"ok": True, "result": result}

    @app.get("/api/gui/integrations/qq")
    async def gui_qq_status(
        _: Annotated[GuiSession, Depends(ready_admin)],
    ) -> dict[str, Any]:
        try:
            status = await get_container().qq.doctor()
        except Exception as exc:
            status = {"ok": False, "error": str(exc)}
        settings = get_settings()
        return {
            "status": status,
            "webui_public_url": settings.qq.webui_public_url,
            "webui_public_port": settings.qq.webui_public_port,
        }

    @app.get("/api/gui/integrations/feishu")
    async def gui_feishu_status(
        _: Annotated[GuiSession, Depends(ready_admin)],
    ) -> dict[str, Any]:
        try:
            return {"status": await get_container().feishu.doctor()}
        except Exception as exc:
            return {"status": {"ok": False, "error": str(exc)}}

    @app.post("/api/gui/integrations/feishu/{action}")
    async def gui_feishu_action(
        action: str,
        _: Annotated[GuiSession, Depends(ready_admin_csrf)],
    ) -> dict[str, Any]:
        if action not in {"login", "logout"}:
            raise HTTPException(status_code=404, detail="未知飞书动作")
        try:
            result = await getattr(get_container().feishu, action)()
            return {"ok": True, "result": result}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
