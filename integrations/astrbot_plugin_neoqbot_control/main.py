"""Optional AstrBot control-plane plugin for NeoQBot.

Environment:
  NEOQBOT_API_URL=http://neoqbot:8080
  NEOQBOT_API_TOKEN=the same value as NEOQBOT_APP__ADMIN_API_TOKEN
  NEOQBOT_ASTRBOT_ADMIN_IDS=12345,67890
"""

import os

import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


class NeoQBotControlPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.base_url = os.getenv("NEOQBOT_API_URL", "http://neoqbot:8080").rstrip("/")
        self.token = os.getenv("NEOQBOT_API_TOKEN", "")
        self.admin_ids = {
            item.strip()
            for item in os.getenv("NEOQBOT_ASTRBOT_ADMIN_IDS", "").split(",")
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

    @filter.command("neoqbot_status")
    async def status(self, event: AstrMessageEvent):
        """查看 NeoQBot 状态（仅配置的管理员）。"""
        if not self._authorized(event):
            yield event.plain_result("无权执行此命令。")
            return
        try:
            yield event.plain_result(await self._call("GET", "/api/v1/status"))
        except Exception as exc:
            logger.exception("NeoQBot status request failed")
            yield event.plain_result(f"请求失败：{type(exc).__name__}")

    @filter.command("neoqbot_moderate")
    async def moderate(self, event: AstrMessageEvent):
        """立即运行一次群聊监测。"""
        if not self._authorized(event):
            yield event.plain_result("无权执行此命令。")
            return
        try:
            yield event.plain_result(await self._call("POST", "/api/v1/jobs/moderation"))
        except Exception as exc:
            logger.exception("NeoQBot moderation request failed")
            yield event.plain_result(f"请求失败：{type(exc).__name__}")

    @filter.command("neoqbot_sync")
    async def sync(self, event: AstrMessageEvent):
        """立即同步一次群公告。"""
        if not self._authorized(event):
            yield event.plain_result("无权执行此命令。")
            return
        try:
            yield event.plain_result(await self._call("POST", "/api/v1/jobs/announcements"))
        except Exception as exc:
            logger.exception("NeoQBot announcement request failed")
            yield event.plain_result(f"请求失败：{type(exc).__name__}")

    async def terminate(self):
        await self.client.aclose()
