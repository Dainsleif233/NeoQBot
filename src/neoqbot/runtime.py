from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Settings
from .database import Database
from .events import EventHandler
from .recording import LocalMessageRecorder
from .services import AnnouncementService, ModerationService

logger = logging.getLogger(__name__)
QUIET_EVENT_RESULTS = frozenset(
    {
        "ignored",
        "unknown_bot",
        "unmanaged_group",
        "disabled",
        "duplicate",
        "unauthorized",
        "not_a_search",
    }
)


class Runtime:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        event_handler: EventHandler,
        moderation: ModerationService | dict[str, ModerationService],
        announcements: AnnouncementService | dict[str, AnnouncementService],
        message_recorder: LocalMessageRecorder | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.event_handler = event_handler
        self.moderation = moderation if isinstance(moderation, dict) else {"default": moderation}
        self.announcements = (
            announcements if isinstance(announcements, dict) else {"default": announcements}
        )
        self.message_recorder = message_recorder
        self.queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=5000)
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._stopping.clear()
        for index in range(self.settings.runtime.event_workers):
            self._tasks.append(
                asyncio.create_task(self._event_worker(), name=f"event-worker-{index}")
            )
        for assignment in self.settings.qq_group_assignments():
            message_task = assignment.tasks.message_detection
            if message_task.enabled and message_task.polling_detection:
                self._tasks.append(
                    asyncio.create_task(
                        self._moderation_loop(
                            assignment.bot_id,
                            assignment.group_id,
                            message_task.interval_minutes,
                        ),
                        name=f"moderation-loop-{assignment.bot_id}-{assignment.group_id}",
                    )
                )
            announcement_task = assignment.tasks.announcement_sync
            if announcement_task.enabled and (
                announcement_task.auto_sync or announcement_task.sync_on_startup
            ):
                self._tasks.append(
                    asyncio.create_task(
                        self._announcement_loop(
                            assignment.bot_id,
                            assignment.group_id,
                            announcement_task.sync_interval_minutes,
                            announcement_task.sync_on_startup,
                            announcement_task.auto_sync,
                        ),
                        name=f"announcement-loop-{assignment.bot_id}-{assignment.group_id}",
                    )
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

    async def submit(self, event: dict[str, Any], bot_id: str = "default") -> None:
        self.queue.put_nowait((bot_id, event))

    async def _event_worker(self) -> None:
        while True:
            bot_id, event = await self.queue.get()
            try:
                result = await self.event_handler.handle(event, bot_id)
                log = logger.debug if result in QUIET_EVENT_RESULTS else logger.info
                log("OneBot event handled by %s: %s", bot_id, result)
            except Exception:
                logger.exception("Unhandled OneBot event failure")
            finally:
                self.queue.task_done()

    async def run_all_moderation(
        self, window_end: datetime | None = None
    ) -> dict[str, dict[str, str]]:
        end = window_end or datetime.now(UTC).replace(second=0, microsecond=0)
        result: dict[str, dict[str, str]] = {}
        for bot in self.settings.effective_qq_bots():
            if any(
                assignment.tasks.message_detection.analyze
                for assignment in self.settings.qq_group_assignments(bot.id)
            ):
                result[bot.id] = await self.run_bot_moderation(bot.id, end)
        return result

    async def run_bot_moderation(
        self, bot_id: str, window_end: datetime | None = None
    ) -> dict[str, str]:
        bot = self.settings.qq_bot(bot_id)
        service = self.moderation.get(bot_id)
        if bot is None or service is None:
            raise ValueError(f"Unknown QQ Bot: {bot_id}")
        end = window_end or datetime.now(UTC).replace(second=0, microsecond=0)
        result: dict[str, str] = {}
        assignments = [
            assignment
            for assignment in self.settings.qq_group_assignments(bot_id)
            if assignment.tasks.message_detection.analyze
        ]
        for assignment in assignments:
            group_id = assignment.group_id
            try:
                result[group_id] = await service.run_group(group_id, end)
            except Exception as exc:
                logger.exception("Moderation run failed for bot %s group %s", bot_id, group_id)
                result[group_id] = f"failed:{type(exc).__name__}"
        return result

    async def sync_all_announcements(self) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for bot in self.settings.effective_qq_bots():
            if any(
                assignment.tasks.announcement_sync.enabled
                for assignment in self.settings.qq_group_assignments(bot.id)
            ):
                result[bot.id] = await self.sync_bot_announcements(bot.id)
        return result

    async def sync_bot_announcements(self, bot_id: str) -> dict[str, object]:
        bot = self.settings.qq_bot(bot_id)
        service = self.announcements.get(bot_id)
        if bot is None or service is None:
            raise ValueError(f"Unknown QQ Bot: {bot_id}")
        result: dict[str, object] = {}
        assignments = [
            assignment
            for assignment in self.settings.qq_group_assignments(bot_id)
            if assignment.tasks.announcement_sync.enabled
        ]
        for assignment in assignments:
            group_id = assignment.group_id
            try:
                result[group_id] = await service.sync_group(group_id)
            except Exception as exc:
                logger.exception("Announcement fetch failed for bot %s group %s", bot_id, group_id)
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
        if self.message_recorder is not None:
            result["message_archive_files"] = self.message_recorder.prune(
                now - timedelta(days=self.settings.retention.message_days)
            )
        logger.info("Retention maintenance completed: %s", result)
        return result

    async def _moderation_loop(self, bot_id: str, group_id: str, interval: int) -> None:
        while not self._stopping.is_set():
            await asyncio.sleep(self._seconds_until_boundary(interval))
            if not self._stopping.is_set():
                try:
                    service = self.moderation.get(bot_id)
                    if service is not None:
                        await service.run_group(group_id)
                except Exception:
                    logger.exception(
                        "Unexpected moderation scheduler failure for %s group %s",
                        bot_id,
                        group_id,
                    )

    async def _announcement_loop(
        self,
        bot_id: str,
        group_id: str,
        interval_minutes: int,
        sync_on_startup: bool,
        auto_sync: bool,
    ) -> None:
        service = self.announcements.get(bot_id)
        if service is None:
            return
        if sync_on_startup:
            try:
                await service.sync_group(group_id)
            except Exception:
                logger.exception(
                    "Initial announcement sync failed for %s group %s", bot_id, group_id
                )
        if not auto_sync:
            return
        interval_seconds = max(1, interval_minutes) * 60
        while not self._stopping.is_set():
            await asyncio.sleep(interval_seconds)
            if not self._stopping.is_set():
                try:
                    await service.sync_group(group_id)
                except Exception:
                    logger.exception(
                        "Unexpected announcement scheduler failure for %s group %s",
                        bot_id,
                        group_id,
                    )

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
