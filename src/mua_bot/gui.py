from __future__ import annotations

# ruff: noqa: B008 - FastAPI dependency injection intentionally uses Depends in defaults.
import asyncio
import hashlib
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

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
        ("qq", "webui_token"): current.qq.webui_token,
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
    qq_section = candidate.setdefault("qq", {})
    if isinstance(qq_section, dict) and isinstance(qq_section.get("bots"), list):
        current_bots = {bot.id: bot for bot in current.effective_qq_bots()}
        for incoming in qq_section["bots"]:
            if not isinstance(incoming, dict):
                continue
            existing = current_bots.get(str(incoming.get("id", "")))
            if existing is None:
                continue
            for key in ("access_token", "webui_token", "webhook_secret"):
                if incoming.get(key) in (None, "", "***"):
                    incoming[key] = getattr(existing, key)
    feishu = candidate.setdefault("feishu", {})
    if isinstance(feishu, dict):
        incoming_environment = feishu.get("extra_environment")
        if incoming_environment in (None, "***"):
            feishu["extra_environment"] = current.feishu.extra_environment
        elif isinstance(incoming_environment, dict):
            for key, value in list(incoming_environment.items()):
                if value in (None, "", "***") and key in current.feishu.extra_environment:
                    incoming_environment[key] = current.feishu.extra_environment[key]
        if isinstance(feishu.get("bots"), list):
            current_bots = {bot.id: bot for bot in current.effective_feishu_bots()}
            for incoming in feishu["bots"]:
                if not isinstance(incoming, dict):
                    continue
                existing = current_bots.get(str(incoming.get("id", "")))
                if existing is None:
                    continue
                environment = incoming.get("extra_environment")
                if environment in (None, "***"):
                    incoming["extra_environment"] = existing.extra_environment
                elif isinstance(environment, dict):
                    for key, value in list(environment.items()):
                        if value in (None, "", "***") and key in existing.extra_environment:
                            environment[key] = existing.extra_environment[key]
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
        mua_session: str | None = Cookie(default=None, alias="mua_session"),
    ) -> GuiSession:
        session = get_container().auth.session(mua_session)
        if session is None:
            raise HTTPException(status_code=401, detail="请先登录")
        return session

    def csrf_session(
        session: GuiSession = Depends(current_session),
        x_csrf_token: str | None = Header(default=None),
    ) -> GuiSession:
        if not x_csrf_token or not secrets.compare_digest(x_csrf_token, session.csrf_token):
            raise HTTPException(status_code=403, detail="CSRF 校验失败")
        return session

    def ready_admin(
        session: GuiSession = Depends(current_session),
    ) -> GuiSession:
        if session.must_change_password:
            raise HTTPException(status_code=403, detail="首次登录必须先修改默认密码")
        return session

    def ready_admin_csrf(
        session: GuiSession = Depends(csrf_session),
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
        session: GuiSession = Depends(current_session),
    ) -> dict[str, Any]:
        return {
            "username": session.username,
            "csrf_token": session.csrf_token,
            "must_change_password": session.must_change_password,
        }

    @app.post("/api/gui/auth/logout")
    async def gui_logout(
        response: Response,
        _: GuiSession = Depends(csrf_session),
        mua_session: str | None = Cookie(default=None, alias="mua_session"),
    ) -> dict[str, bool]:
        get_container().auth.logout(mua_session)
        response.delete_cookie("mua_session", path="/")
        return {"ok": True}

    @app.post("/api/gui/auth/password")
    async def gui_change_password(
        payload: PasswordPayload,
        session: GuiSession = Depends(csrf_session),
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
        _: GuiSession = Depends(ready_admin),
    ) -> dict[str, Any]:
        settings = get_settings()
        return {
            "version": __version__,
            "environment": settings.app.environment,
            "dry_run": settings.app.dry_run,
            "queue_size": get_container().runtime.queue.qsize(),
            "counts": get_container().database.counts(),
            "diagnostics": settings.diagnostics(),
            "managed_groups": settings.managed_group_ids(),
            "bots": [
                {
                    "id": bot.id,
                    "name": bot.name,
                    "enabled": bot.enabled,
                    "groups": bot.managed_group_ids,
                    "tasks": bot.tasks.model_dump(mode="json"),
                }
                for bot in settings.effective_qq_bots()
            ],
        }

    @app.get("/api/gui/settings")
    async def gui_settings(
        _: GuiSession = Depends(ready_admin),
    ) -> dict[str, Any]:
        return {"config": get_settings().redacted_dict()}

    @app.put("/api/gui/settings")
    async def gui_save_settings(
        payload: SettingsPayload,
        _: GuiSession = Depends(ready_admin_csrf),
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
        _: GuiSession = Depends(ready_admin),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        try:
            records = get_container().database.recent_records(kind, limit)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"kind": kind, "records": records}

    @app.post("/api/gui/jobs/{job}")
    async def gui_run_job(
        job: str,
        _: GuiSession = Depends(ready_admin_csrf),
        bot_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        runtime = get_container().runtime
        if job == "moderation":
            result = (
                await runtime.run_bot_moderation(bot_id)
                if bot_id
                else await runtime.run_all_moderation()
            )
        elif job == "announcements":
            result = (
                await runtime.sync_bot_announcements(bot_id)
                if bot_id
                else await runtime.sync_all_announcements()
            )
        elif job == "maintenance":
            result = await runtime.run_maintenance()
        else:
            raise HTTPException(status_code=404, detail="未知任务")
        return {"ok": True, "result": result}

    def qq_qrcode_path(bot_id: str | None) -> Path:
        bot = get_settings().qq_bot(bot_id)
        if bot is None:
            raise HTTPException(status_code=404, detail="未知 QQ Bot")
        path = Path(bot.qrcode_path)
        try:
            if not path.is_file():
                raise HTTPException(status_code=404, detail="NapCat 尚未生成登录二维码")
            if path.stat().st_size > 5 * 1024 * 1024:
                raise HTTPException(status_code=422, detail="NapCat 二维码文件异常")
            with path.open("rb") as handle:
                if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                    raise HTTPException(status_code=422, detail="NapCat 二维码不是有效 PNG")
        except HTTPException:
            raise
        except OSError as exc:
            raise HTTPException(status_code=404, detail=f"无法读取 NapCat 二维码：{exc}") from exc
        return path

    def qrcode_fingerprint(path: Path) -> tuple[int, int, str] | None:
        try:
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            return stat.st_mtime_ns, stat.st_size, digest
        except OSError:
            return None

    @app.get("/api/gui/integrations/qq/qrcode")
    async def gui_qq_qrcode(
        _: GuiSession = Depends(ready_admin),
        bot_id: str | None = Query(default=None),
    ) -> FileResponse:
        path = await asyncio.to_thread(qq_qrcode_path, bot_id)
        return FileResponse(
            path,
            media_type="image/png",
            headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
        )

    @app.post("/api/gui/integrations/qq/qrcode/refresh")
    async def gui_qq_qrcode_refresh(
        _: GuiSession = Depends(ready_admin_csrf),
        bot_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        bot = get_settings().qq_bot(bot_id)
        if bot is None:
            raise HTTPException(status_code=404, detail="未知 QQ Bot")
        path = Path(bot.qrcode_path)
        before = await asyncio.to_thread(qrcode_fingerprint, path)
        try:
            result = await get_container().napcat_clients[bot.id].refresh_qrcode()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"NapCat 刷新二维码失败：{exc}") from exc
        deadline = time.monotonic() + 8
        after = before
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            after = await asyncio.to_thread(qrcode_fingerprint, path)
            if after is not None and after != before:
                break
        if after is None or after == before:
            raise HTTPException(status_code=504, detail="NapCat 已接收刷新请求，但二维码文件未更新")
        return {
            "ok": True,
            "bot_id": bot.id,
            "changed": True,
            "updated_at": after[0] / 1_000_000_000,
            "result": result,
        }

    @app.post("/api/gui/integrations/qq/webui-token")
    async def gui_qq_webui_token(
        _: GuiSession = Depends(ready_admin_csrf),
        bot_id: str | None = Query(default=None),
    ) -> dict[str, str]:
        bot = get_settings().qq_bot(bot_id)
        if bot is None:
            raise HTTPException(status_code=404, detail="未知 QQ Bot")
        try:
            token = get_container().napcat_clients[bot.id].token()
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"bot_id": bot.id, "token": token}

    @app.get("/api/gui/integrations/qq")
    async def gui_qq_status(
        _: GuiSession = Depends(ready_admin),
        bot_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        settings = get_settings()
        bots = settings.effective_qq_bots()
        if bot_id:
            bots = [bot for bot in bots if bot.id == bot_id]
            if not bots:
                raise HTTPException(status_code=404, detail="未知 QQ Bot")
        results = []
        for bot in bots:
            try:
                webui_token_available = bool(get_container().napcat_clients[bot.id].token())
            except Exception:
                webui_token_available = False
            if not bot.enabled:
                onebot_status: dict[str, Any] = {"ok": True, "enabled": False}
                napcat_status: dict[str, Any] = {"ok": True, "enabled": False}
                connection_state = "disabled"
                connection_message = "QQ Bot 已停用"
            else:
                onebot_result, napcat_result = await asyncio.gather(
                    get_container().qq_clients[bot.id].doctor(),
                    get_container().napcat_clients[bot.id].check_login_status(),
                    return_exceptions=True,
                )
                onebot_status = (
                    {"ok": False, "error": str(onebot_result)}
                    if isinstance(onebot_result, BaseException)
                    else onebot_result
                )
                napcat_status = (
                    {"ok": False, "error": str(napcat_result)}
                    if isinstance(napcat_result, BaseException)
                    else {"ok": True, **napcat_result}
                )
                if onebot_status.get("ok"):
                    connection_state = "connected"
                    connection_message = "QQ 已登录，OneBot 连接正常"
                elif napcat_status.get("isOffline"):
                    connection_state = "qq_offline"
                    connection_message = "QQ 登录态存在，但客户端已离线"
                elif napcat_status.get("isLogin"):
                    connection_state = "qq_logged_in_onebot_unavailable"
                    connection_message = "QQ 已登录，但 OneBot HTTP 服务不可用"
                elif napcat_status.get("ok"):
                    connection_state = "waiting_for_scan"
                    connection_message = "NapCat 已连接，等待扫描二维码"
                else:
                    connection_state = "napcat_unreachable"
                    connection_message = "NapCat WebUI 与 OneBot 均不可访问"
            results.append(
                {
                    "id": bot.id,
                    "name": bot.name,
                    "enabled": bot.enabled,
                    "status": onebot_status,
                    "onebot_status": onebot_status,
                    "napcat_status": napcat_status,
                    "connection_state": connection_state,
                    "connection_message": connection_message,
                    "webhook_url": f"/webhooks/onebot/{bot.id}",
                    "webui_public_url": bot.webui_public_url,
                    "webui_public_port": bot.webui_public_port,
                    "qrcode_available": Path(bot.qrcode_path).is_file(),
                    "webui_token_available": webui_token_available,
                    "tasks": bot.tasks.model_dump(mode="json"),
                }
            )
        primary = results[0]
        return {
            "bots": results,
            "status": primary["status"],
            "webui_public_url": primary["webui_public_url"],
            "webui_public_port": primary["webui_public_port"],
        }

    @app.get("/api/gui/integrations/feishu")
    async def gui_feishu_status(
        _: GuiSession = Depends(ready_admin),
        bot_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        bots = get_settings().effective_feishu_bots()
        if bot_id:
            bots = [bot for bot in bots if bot.id == bot_id]
            if not bots:
                raise HTTPException(status_code=404, detail="未知飞书 Bot")
        results = []
        for bot in bots:
            try:
                status = await get_container().feishu_clients[bot.id].doctor()
            except Exception as exc:
                status = {"ok": False, "error": str(exc)}
            results.append(
                {"id": bot.id, "name": bot.name, "enabled": bot.enabled, "status": status}
            )
        return {"bots": results, "status": results[0]["status"]}

    @app.post("/api/gui/integrations/feishu/{action}")
    async def gui_feishu_action(
        action: str,
        _: GuiSession = Depends(ready_admin_csrf),
        bot_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        if action not in {"login", "logout"}:
            raise HTTPException(status_code=404, detail="未知飞书动作")
        bot = get_settings().feishu_bot(bot_id)
        if bot is None:
            raise HTTPException(status_code=404, detail="未知飞书 Bot")
        try:
            result = await getattr(get_container().feishu_clients[bot.id], action)()
            return {"ok": True, "bot_id": bot.id, "result": result}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
