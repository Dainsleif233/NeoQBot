"""Optional AstrBot control-plane plugin for MUA-Bot.

Environment:
  MUA_API_URL=http://mua-bot:8080
  MUA_API_TOKEN=the same value as MUA_APP__ADMIN_API_TOKEN
  MUA_ASTRBOT_ADMIN_IDS=12345,67890
"""

import os

import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


class MuaControlPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.base_url = os.getenv("MUA_API_URL", "http://mua-bot:8080").rstrip("/")
        self.token = os.getenv("MUA_API_TOKEN", "")
        self.admin_ids = {
            item.strip()
            for item in os.getenv("MUA_ASTRBOT_ADMIN_IDS", "").split(",")
            if item.strip()
        }
        self.client = httpx.AsyncClient(timeout=90)

    def _authorized(self, event: AstrMessageEvent) -> bool:
        return bool(self.token and str(event.get_sender_id()) in self.admin_ids)

    async def _call(self, method: str, path: str) -> str:
        response = await self.client.request(
            method,
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        response.raise_for_status()
        return response.text[:3500]

    @filter.command("mua_status")
    async def status(self, event: AstrMessageEvent):
        """查看 MUA-Bot 状态（仅配置的管理员）。"""
        if not self._authorized(event):
            yield event.plain_result("无权执行此命令。")
            return
        try:
            yield event.plain_result(await self._call("GET", "/api/v1/status"))
        except Exception as exc:
            logger.exception("MUA-Bot status request failed")
            yield event.plain_result(f"请求失败：{type(exc).__name__}")

    @filter.command("mua_moderate")
    async def moderate(self, event: AstrMessageEvent):
        """立即运行一次群聊监测。"""
        if not self._authorized(event):
            yield event.plain_result("无权执行此命令。")
            return
        try:
            yield event.plain_result(await self._call("POST", "/api/v1/jobs/moderation"))
        except Exception as exc:
            logger.exception("MUA-Bot moderation request failed")
            yield event.plain_result(f"请求失败：{type(exc).__name__}")

    @filter.command("mua_sync")
    async def sync(self, event: AstrMessageEvent):
        """立即同步一次群公告。"""
        if not self._authorized(event):
            yield event.plain_result("无权执行此命令。")
            return
        try:
            yield event.plain_result(await self._call("POST", "/api/v1/jobs/announcements"))
        except Exception as exc:
            logger.exception("MUA-Bot announcement request failed")
            yield event.plain_result(f"请求失败：{type(exc).__name__}")

    async def terminate(self):
        await self.client.aclose()
