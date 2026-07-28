from datetime import UTC, datetime, timedelta
from pathlib import Path

from mua_bot.config import Settings
from mua_bot.database import Database
from mua_bot.models import (
    Announcement,
    GroupMessage,
    JoinDecision,
    JoinRequest,
    ModerationFinding,
    ModerationResult,
)
from mua_bot.recording import LocalMessageRecorder
from mua_bot.services import AnnouncementService, JoinApprovalService, ModerationService


class FakeEngine:
    join_decision = JoinDecision(decision="approve", confidence=0.95, reason="ok")
    moderation_result = ModerationResult(safe=True, summary="safe")

    async def review_join(self, request: JoinRequest) -> JoinDecision:
        return self.join_decision

    async def moderate_messages(self, messages: list[GroupMessage]) -> ModerationResult:
        return self.moderation_result


class FakeQQ:
    def __init__(self) -> None:
        self.approvals: list[tuple[str, bool]] = []
        self.notifications: list[str] = []
        self.private_messages: list[tuple[str, str]] = []

    async def approve_join(self, request: JoinRequest, approve: bool, reason: str = "") -> None:
        self.approvals.append((request.flag, approve))

    async def notify_administrators(self, message: str) -> None:
        self.notifications.append(message)

    async def send_private_message(self, user_id: str, message: str) -> None:
        self.private_messages.append((user_id, message))

    async def fetch_announcements(self, group_id: str):
        return []

    async def doctor(self):
        return {"ok": True}

    async def close(self):
        return None


class FailingNotificationQQ(FakeQQ):
    async def notify_administrators(self, message: str) -> None:
        raise RuntimeError("notification unavailable")


class FailingFetchQQ(FakeQQ):
    async def fetch_announcements(self, group_id: str):
        raise RuntimeError("QQ announcement API unavailable")


class FakeFeishu:
    def __init__(self) -> None:
        self.archived: list[Announcement] = []

    async def archive_announcement(self, announcement: Announcement) -> None:
        self.archived.append(announcement)

    async def search(self, query: str, limit: int):
        return []

    async def doctor(self):
        return {"ok": True}


def settings_for(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "app": {"database_path": str(tmp_path / "test.db"), "dry_run": False},
            "qq": {"managed_group_ids": ["g1"], "administrator_qq_ids": ["a1"]},
            "join_approval": {"auto_approve": True, "minimum_confidence": 0.9},
            "moderation": {"risk_threshold": 0.7, "window_minutes": 5},
        }
    )


async def test_join_is_approved_once(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    database = Database(settings.app.database_path)
    database.initialize()
    qq = FakeQQ()
    service = JoinApprovalService(settings, database, FakeEngine(), qq)
    request = JoinRequest(event_id="e1", flag="f1", group_id="g1", user_id="u1")

    assert await service.handle(request) == "approved"
    assert await service.handle(request) == "duplicate"
    assert qq.approvals == [("f1", True)]


async def test_join_detection_only_records_without_processing(tmp_path: Path) -> None:
    values = settings_for(tmp_path).model_dump()
    values["qq"]["bots"] = [
        {
            "id": "observer",
            "name": "Observer",
            "managed_group_ids": ["g1"],
            "administrator_qq_ids": ["a1"],
            "tasks": {
                "join_management": {"enabled": True, "detect_requests": True},
            },
        }
    ]
    settings = Settings.model_validate(values)
    database = Database(settings.app.database_path)
    database.initialize()
    qq = FakeQQ()
    service = JoinApprovalService(
        settings, database, FakeEngine(), qq, settings.qq_bot("observer")
    )

    result = await service.handle(
        JoinRequest(
            bot_id="observer",
            event_id="e-observer",
            flag="f-observer",
            group_id="g1",
            user_id="u1",
        )
    )

    assert result == "detected"
    assert qq.approvals == []
    assert qq.notifications == []


async def test_moderation_alerts_only_above_threshold(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    database = Database(settings.app.database_path)
    database.initialize()
    qq = FakeQQ()
    engine = FakeEngine()
    engine.moderation_result = ModerationResult(
        safe=False,
        summary="risk",
        findings=[
            ModerationFinding(
                category="attack",
                severity="high",
                risk_score=0.9,
                reason="targeted attack",
                message_ids=["m1"],
                excerpts=["excerpt"],
            )
        ],
    )
    service = ModerationService(settings, database, engine, qq)
    end = datetime(2026, 7, 21, 4, 30, tzinfo=UTC)
    service.capture(
        GroupMessage(
            message_id="m1",
            group_id="g1",
            user_id="u1",
            text="message",
            sent_at=end - timedelta(minutes=1),
        )
    )

    assert await service.run_group("g1", end) == "alerted"
    assert len(qq.notifications) == 1


async def test_moderation_reports_alert_delivery_failure(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    database = Database(settings.app.database_path)
    database.initialize()
    engine = FakeEngine()
    engine.moderation_result = ModerationResult(
        safe=False,
        summary="risk",
        findings=[
            ModerationFinding(
                category="attack",
                severity="high",
                risk_score=0.9,
                reason="targeted attack",
            )
        ],
    )
    service = ModerationService(settings, database, engine, FailingNotificationQQ())
    end = datetime(2026, 7, 21, 4, 30, tzinfo=UTC)
    service.capture(
        GroupMessage(
            message_id="m1",
            group_id="g1",
            user_id="u1",
            text="message",
            sent_at=end - timedelta(minutes=1),
        )
    )

    assert await service.run_group("g1", end) == "alert_failed"


def test_record_only_message_is_saved_to_database_and_local_volume(tmp_path: Path) -> None:
    values = settings_for(tmp_path).model_dump()
    values["app"]["message_archive_path"] = str(tmp_path / "group-records")
    values["qq"]["bots"] = [
        {
            "id": "recorder",
            "managed_group_ids": ["g1"],
            "tasks": {"message_detection": {"record_only": True}},
        }
    ]
    settings = Settings.model_validate(values)
    database = Database(settings.app.database_path)
    database.initialize()
    service = ModerationService(
        settings,
        database,
        FakeEngine(),
        FakeQQ(),
        settings.qq_bot("recorder"),
        LocalMessageRecorder(settings.app.message_archive_path),
    )

    captured = service.capture(
        GroupMessage(
            bot_id="recorder",
            message_id="record-1",
            group_id="g1",
            user_id="u1",
            text="archive me",
            sent_at=datetime(2026, 7, 28, tzinfo=UTC),
        )
    )

    assert captured is True
    assert database.counts()["group_messages"] == 1
    assert (tmp_path / "group-records" / "recorder" / "g1" / "2026-07-28.jsonl").exists()


async def test_pending_feishu_sync_retries_when_qq_fetch_fails(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.feishu.enabled = True
    settings.feishu.driver = "cli"
    database = Database(settings.app.database_path)
    database.initialize()
    database.upsert_announcement(
        Announcement(announcement_id="n1", group_id="g1", content="notice")
    )
    feishu = FakeFeishu()
    service = AnnouncementService(settings, database, FailingFetchQQ(), feishu)

    result = await service.sync_group("g1")

    assert result["synced"] == 1
    assert "fetch_error" in result
    assert len(feishu.archived) == 1
    assert database.pending_announcements(group_id="g1") == []
