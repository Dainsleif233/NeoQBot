from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from neoqbot.config import Settings
from neoqbot.database import Database
from neoqbot.events import EventHandler
from neoqbot.models import JoinDecision, JoinRequest
from neoqbot.services import JoinApprovalService


def _admin_settings(*, detect_requests: bool) -> Settings:
    return Settings.model_validate(
        {
            "gui": {"enabled": False},
            "qq": {
                "bots": [
                    {
                        "id": "worker",
                        "name": "Worker",
                        "enabled": True,
                        "connection_mode": "external",
                        "administrator_qq_ids": ["10001"],
                    }
                ]
            },
            "orchestration": {
                "resources": [
                    {
                        "id": "group-main",
                        "kind": "qq_group",
                        "name": "Main",
                        "external_id": "100",
                    }
                ],
                "edges": [
                    {
                        "id": "worker-manages-main",
                        "source": "qq-bot:worker",
                        "target": "group-main",
                        "relation": "manages",
                        "enabled": True,
                        "tasks": {
                            "join_management": {
                                "enabled": True,
                                "detect_requests": detect_requests,
                                "execute_management": False,
                                "auto_approve": False,
                                "auto_reject": False,
                                "minimum_confidence": 0.88,
                            }
                        },
                    }
                ],
            },
        }
    )


def _temp_db() -> Database:
    path = Path(tempfile.mkdtemp()) / "test.db"
    database = Database(path)
    database.initialize()
    return database


def _pending_request(
    bot_id: str = "worker", group_id: str = "100", user_id: str = "200"
) -> JoinRequest:
    return JoinRequest(
        bot_id=bot_id,
        event_id="evt-1",
        flag="flag-1",
        group_id=group_id,
        user_id=user_id,
        comment="hello",
    )


class JoinAdminDecisionDbTests(unittest.TestCase):
    def test_records_admin_approval_for_pending_request(self) -> None:
        database = _temp_db()
        database.save_join_request(_pending_request())
        status = database.record_admin_join_decision(
            "worker", "100", "200", "999", "approved_by_admin"
        )
        self.assertEqual(status, "approved_by_admin")
        rows = database.recent_records("joins", group_id="100", bot_id="worker")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["action_status"], "approved_by_admin")
        self.assertEqual(row["handled_by"], "999")
        self.assertEqual(row["decision"], "approve")
        self.assertTrue(row["handled_at"])

    def test_returns_no_pending_request_when_absent(self) -> None:
        database = _temp_db()
        status = database.record_admin_join_decision(
            "worker", "100", "200", "999", "approved_by_admin"
        )
        self.assertEqual(status, "no_pending_request")
        self.assertEqual(database.recent_records("joins", group_id="100"), [])

    def test_does_not_overwrite_finalized_request(self) -> None:
        database = _temp_db()
        request = _pending_request()
        database.save_join_request(request)
        database.update_join_decision(
            request, JoinDecision(decision="approve", confidence=0.9, reason="x"), "approved"
        )
        status = database.record_admin_join_decision(
            "worker", "100", "200", "999", "approved_by_admin"
        )
        self.assertEqual(status, "no_pending_request")
        row = database.recent_records("joins", group_id="100", bot_id="worker")[0]
        self.assertEqual(row["action_status"], "approved")
        self.assertEqual(row["handled_by"], "")

    def test_records_latest_pending_when_multiple_requests(self) -> None:
        database = _temp_db()
        database.save_join_request(
            JoinRequest(
                bot_id="worker",
                event_id="evt-old",
                flag="flag-old",
                group_id="100",
                user_id="200",
                comment="first",
            )
        )
        database.save_join_request(
            JoinRequest(
                bot_id="worker",
                event_id="evt-new",
                flag="flag-new",
                group_id="100",
                user_id="200",
                comment="second",
            )
        )
        status = database.record_admin_join_decision(
            "worker", "100", "200", "999", "approved_by_admin"
        )
        self.assertEqual(status, "approved_by_admin")
        rows = database.recent_records("joins", group_id="100", bot_id="worker")
        finalized = [r for r in rows if r["action_status"] == "approved_by_admin"]
        self.assertEqual(len(finalized), 1)
        self.assertEqual(finalized[0]["request_flag"], "flag-new")
        self.assertEqual(finalized[0]["handled_by"], "999")
        pending = [r for r in rows if r["action_status"] == "received"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["request_flag"], "flag-old")


class JoinAdminApprovalServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_admin_approval(self) -> None:
        settings = _admin_settings(detect_requests=True)
        database = _temp_db()
        database.save_join_request(_pending_request())
        service = JoinApprovalService(
            settings, database, object(), object(), settings.qq_bot("worker")
        )
        event = {
            "post_type": "notice",
            "notice_type": "group_increase",
            "sub_type": "approve",
            "group_id": "100",
            "user_id": "200",
            "operator_id": "999",
        }
        result = await service.record_admin_approval(event)
        self.assertEqual(result, "approved_by_admin")
        row = database.recent_records("joins", group_id="100", bot_id="worker")[0]
        self.assertEqual(row["handled_by"], "999")

    async def test_operator_zero_recorded_as_unknown(self) -> None:
        settings = _admin_settings(detect_requests=True)
        database = _temp_db()
        database.save_join_request(_pending_request())
        service = JoinApprovalService(
            settings, database, object(), object(), settings.qq_bot("worker")
        )
        result = await service.record_admin_approval(
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "sub_type": "approve",
                "group_id": "100",
                "user_id": "200",
                "operator_id": "0",
            }
        )
        self.assertEqual(result, "approved_by_admin")
        self.assertEqual(
            database.recent_records("joins", group_id="100", bot_id="worker")[0]["handled_by"], ""
        )

    async def test_no_pending_request(self) -> None:
        settings = _admin_settings(detect_requests=True)
        database = _temp_db()
        service = JoinApprovalService(
            settings, database, object(), object(), settings.qq_bot("worker")
        )
        result = await service.record_admin_approval(
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "sub_type": "approve",
                "group_id": "100",
                "user_id": "200",
                "operator_id": "999",
            }
        )
        self.assertEqual(result, "no_pending_request")

    async def test_detect_disabled_returns_disabled(self) -> None:
        settings = _admin_settings(detect_requests=False)
        database = _temp_db()
        database.save_join_request(_pending_request())
        service = JoinApprovalService(
            settings, database, object(), object(), settings.qq_bot("worker")
        )
        result = await service.record_admin_approval(
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "sub_type": "approve",
                "group_id": "100",
                "user_id": "200",
                "operator_id": "999",
            }
        )
        self.assertEqual(result, "disabled")
        self.assertEqual(
            database.recent_records("joins", group_id="100", bot_id="worker")[0]["action_status"],
            "received",
        )

    async def test_event_handler_routes_group_increase(self) -> None:
        settings = _admin_settings(detect_requests=True)
        database = _temp_db()
        database.save_join_request(_pending_request())
        service = JoinApprovalService(
            settings, database, object(), object(), settings.qq_bot("worker")
        )
        handler = EventHandler({"worker": service}, {"worker": object()}, {"worker": object()})
        result = await handler.handle(
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "sub_type": "approve",
                "group_id": "100",
                "user_id": "200",
                "operator_id": "999",
            },
            "worker",
        )
        self.assertEqual(result, "approved_by_admin")

    async def test_event_handler_ignores_invite(self) -> None:
        settings = _admin_settings(detect_requests=True)
        database = _temp_db()
        database.save_join_request(_pending_request())
        service = JoinApprovalService(
            settings, database, object(), object(), settings.qq_bot("worker")
        )
        handler = EventHandler({"worker": service}, {"worker": object()}, {"worker": object()})
        result = await handler.handle(
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "sub_type": "invite",
                "group_id": "100",
                "user_id": "200",
                "operator_id": "999",
            },
            "worker",
        )
        self.assertEqual(result, "ignored")

    async def test_event_handler_unmanaged_group(self) -> None:
        settings = _admin_settings(detect_requests=True)
        database = _temp_db()
        service = JoinApprovalService(
            settings, database, object(), object(), settings.qq_bot("worker")
        )
        handler = EventHandler({"worker": service}, {"worker": object()}, {"worker": object()})
        result = await handler.handle(
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "sub_type": "approve",
                "group_id": "999",
                "user_id": "200",
                "operator_id": "999",
            },
            "worker",
        )
        self.assertEqual(result, "unmanaged_group")
