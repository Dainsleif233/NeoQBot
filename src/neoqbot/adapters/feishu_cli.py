from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from typing import Any

from ..config import FeishuBotConfig, FeishuConfig
from ..models import Announcement, SearchHit


class FeishuCliError(RuntimeError):
    pass


class _FormatValues(defaultdict[str, str]):
    def __missing__(self, key: str) -> str:
        raise FeishuCliError(f"Missing template value: {key}")


class DisabledFeishuGateway:
    async def archive_announcement(self, announcement: Announcement) -> None:
        return None

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        return []

    async def doctor(self) -> dict[str, object]:
        return {"ok": True, "enabled": False}

    async def login(self) -> object:
        raise FeishuCliError("飞书 CLI 尚未启用")

    async def logout(self) -> object:
        raise FeishuCliError("飞书 CLI 尚未启用")


class FeishuCliGateway:
    """Safe argv-template wrapper around the official Feishu CLI.

    CLI versions change more frequently than this service. Templates keep those changes out of
    business logic. Commands are executed without a shell, so document content cannot inject a
    second command.
    """

    def __init__(self, config: FeishuConfig | FeishuBotConfig):
        self.config = config

    async def _run(self, action: str, values: dict[str, Any], stdin_text: str | None = None) -> Any:
        template = self.config.command_templates.get(action)
        if not template:
            raise FeishuCliError(f"Feishu CLI command template is not configured: {action}")
        serialized = _FormatValues(str, {key: str(value) for key, value in values.items()})
        argv = [token.format_map(serialized) for token in template]
        environment = os.environ.copy()
        environment.update(self.config.extra_environment)
        process = await asyncio.create_subprocess_exec(
            self.config.executable,
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin_text.encode() if stdin_text is not None else None),
                timeout=self.config.timeout_seconds,
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise FeishuCliError(f"Feishu CLI action timed out: {action}") from None
        if process.returncode != 0:
            error = stderr.decode("utf-8", errors="replace")[-2000:]
            raise FeishuCliError(f"Feishu CLI action {action} failed: {error}")
        output = stdout.decode("utf-8", errors="replace").strip()
        if not output:
            return None
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return output

    async def archive_announcement(self, announcement: Announcement) -> None:
        payload = json.dumps(announcement.model_dump(mode="json"), ensure_ascii=False)
        await self._run(
            "archive_announcement",
            {
                "payload_json": payload,
                "announcement_id": announcement.announcement_id,
                "group_id": announcement.group_id,
                "title": announcement.title,
                "content": announcement.content,
                "author_id": announcement.author_id,
                "published_at": announcement.published_at.isoformat()
                if announcement.published_at
                else "",
            },
            stdin_text=payload if self.config.archive_payload_stdin else None,
        )

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        result = await self._run("search", {"query": query, "limit": limit})
        if isinstance(result, dict):
            raw_items = result.get("items") or result.get("data") or result.get("results") or []
        elif isinstance(result, list):
            raw_items = result
        elif isinstance(result, str):
            return [SearchHit(title="飞书搜索结果", snippet=result)]
        else:
            raw_items = []
        hits: list[SearchHit] = []
        for item in raw_items[:limit]:
            if isinstance(item, str):
                hits.append(SearchHit(title="搜索结果", snippet=item))
            elif isinstance(item, dict):
                hits.append(
                    SearchHit(
                        title=str(item.get("title") or item.get("name") or "搜索结果"),
                        snippet=str(
                            item.get("snippet") or item.get("content") or item.get("text") or ""
                        ),
                        url=str(item.get("url") or item.get("link") or ""),
                    )
                )
        return hits

    async def doctor(self) -> dict[str, object]:
        if "doctor" in self.config.command_templates:
            result = await self._run("doctor", {})
            return {"ok": True, "result": result}
        environment = os.environ.copy()
        environment.update(self.config.extra_environment)
        process = await asyncio.create_subprocess_exec(
            self.config.executable,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.config.timeout_seconds
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            return {"ok": False, "error": "Feishu CLI --version timed out"}
        return {
            "ok": process.returncode == 0,
            "version": stdout.decode(errors="replace").strip(),
            "error": stderr.decode(errors="replace").strip(),
        }

    async def login(self) -> object:
        return await self._run("login", {})

    async def logout(self) -> object:
        return await self._run("logout", {})
