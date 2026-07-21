from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Settings
from .database import Database
from .events import EventHandler
from .services import AnnouncementService, ModerationService

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        event_handler: EventHandler,
        moderation: ModerationService,
        announcements: AnnouncementService,
    ) -> None:
        self.settings = settings
        self.database = database
        self.event_handler = event_handler
        self.moderation = moderation
        self.announcements = announcements
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=5000)
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._stopping.clear()
        for index in range(self.settings.runtime.event_workers):
            self._tasks.append(
                asyncio.create_task(self._event_worker(), name=f"event-worker-{index}")
            )
        if self.settings.moderation.enabled:
            self._tasks.append(asyncio.create_task(self._moderation_loop(), name="moderation-loop"))
        if self.settings.announcements.enabled:
            self._tasks.append(
                asyncio.create_task(self._announcement_loop(), name="announcement-loop")
            )
        if self.settings.retention.enabled:
            self._tasks.append(
                asyncio.create_task(self._maintenance_loop(), name="maintenance-loop")
            )

    async def stop(self) -> None:
        self._stopping.set()
        with suppress(TimeoutError):
            await asyncio.wait_for(
                self.queue.join(), timeout=self.settings.runtime.shutdown_grace_seconds
            )
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def submit(self, event: dict[str, Any]) -> None:
        self.queue.put_nowait(event)

    async def _event_worker(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                result = await self.event_handler.handle(event)
                logger.info("OneBot event handled: %s", result)
            except Exception:
                logger.exception("Unhandled OneBot event failure")
            finally:
                self.queue.task_done()

    async def run_all_moderation(self, window_end: datetime | None = None) -> dict[str, str]:
        end = window_end or datetime.now(UTC).replace(second=0, microsecond=0)
        result: dict[str, str] = {}
        for group_id in self.settings.qq.managed_group_ids:
            try:
                result[group_id] = await self.moderation.run_group(group_id, end)
            except Exception as exc:
                logger.exception("Moderation run failed for group %s", group_id)
                result[group_id] = f"failed:{type(exc).__name__}"
        return result

    async def sync_all_announcements(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for group_id in self.settings.qq.managed_group_ids:
            try:
                result[group_id] = await self.announcements.sync_group(group_id)
            except Exception as exc:
                logger.exception("Announcement fetch failed for group %s", group_id)
                result[group_id] = {"error": str(exc)}
        return result

    async def run_maintenance(self) -> dict[str, int]:
        now = datetime.now(UTC)
        result = self.database.prune(
            messages_before=now - timedelta(days=self.settings.retention.message_days),
            joins_before=now - timedelta(days=self.settings.retention.join_request_days),
            moderation_before=now - timedelta(days=self.settings.retention.moderation_run_days),
            audit_before=now - timedelta(days=self.settings.retention.audit_days),
        )
        logger.info("Retention maintenance completed: %s", result)
        return result

    async def _moderation_loop(self) -> None:
        interval = self.settings.moderation.interval_minutes
        while not self._stopping.is_set():
            await asyncio.sleep(self._seconds_until_boundary(interval))
            if not self._stopping.is_set():
                try:
                    await self.run_all_moderation()
                except Exception:
                    logger.exception("Unexpected moderation scheduler failure")

    async def _announcement_loop(self) -> None:
        if self.settings.announcements.sync_on_startup:
            await self.sync_all_announcements()
        interval_seconds = max(1, self.settings.announcements.sync_interval_minutes) * 60
        while not self._stopping.is_set():
            await asyncio.sleep(interval_seconds)
            if not self._stopping.is_set():
                try:
                    await self.sync_all_announcements()
                except Exception:
                    logger.exception("Unexpected announcement scheduler failure")

    async def _maintenance_loop(self) -> None:
        try:
            await self.run_maintenance()
        except Exception:
            logger.exception("Initial retention maintenance failed")
        while not self._stopping.is_set():
            await asyncio.sleep(24 * 60 * 60)
            if not self._stopping.is_set():
                try:
                    await self.run_maintenance()
                except Exception:
                    logger.exception("Retention maintenance failed")

    @staticmethod
    def _seconds_until_boundary(interval_minutes: int) -> float:
        interval = max(1, interval_minutes)
        now = datetime.now(UTC)
        minute_of_day = now.hour * 60 + now.minute
        next_minute = ((minute_of_day // interval) + 1) * interval
        next_day_offset, target_minute = divmod(next_minute, 24 * 60)
        target = (now + timedelta(days=next_day_offset)).replace(
            hour=target_minute // 60,
            minute=target_minute % 60,
            second=0,
            microsecond=0,
        )
        return max(0.1, (target - now).total_seconds())
