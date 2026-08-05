from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import yaml
from pydantic import ValidationError

from neoqbot.app import create_app
from neoqbot.config import Settings
from neoqbot.models import GroupMessage
from neoqbot.runtime import Runtime
from neoqbot.services import ModerationService


def assignment_settings() -> Settings:
    return Settings.model_validate(
        {
            "gui": {"enabled": False},
            "qq": {
                "bots": [
                    {
                        "id": "worker",
                        "name": "Worker",
                        "enabled": True,
                        "administrator_qq_ids": ["10001"],
                    }
                ]
            },
            "orchestration": {
                "resources": [
                    {
                        "id": "group-record",
                        "kind": "qq_group",
                        "name": "Record group",
                        "external_id": "100",
                    },
                    {
                        "id": "group-analyze",
                        "kind": "qq_group",
                        "name": "Analyze group",
                        "external_id": "200",
                    },
                ],
                "edges": [
                    {
                        "id": "worker-record",
                        "source": "qq-bot:worker",
                        "target": "group-record",
                        "relation": "observes",
                        "tasks": {
                            "message_detection": {
                                "record_only": True,
                                "interval_minutes": 60,
                            }
                        },
                    },
                    {
                        "id": "worker-analyze",
                        "source": "qq-bot:worker",
                        "target": "group-analyze",
                        "relation": "manages",
                        "tasks": {
                            "join_management": {
                                "detect_requests": True,
                                "minimum_confidence": 0.93,
                            },
                            "message_detection": {
                                "analyze": True,
                                "handle": True,
                                "interval_minutes": 15,
                                "window_minutes": 10,
                                "risk_threshold": 0.82,
                            },
                            "announcement_sync": {
                                "enabled": True,
                                "feishu_bot_id": "default",
                            },
                        },
                    },
                ],
            },
        }
    )


class SettingsAssignmentTests(unittest.TestCase):
    def test_tasks_are_scoped_to_each_bot_group_edge(self) -> None:
        settings = assignment_settings()

        record = settings.qq_group_assignment("worker", "100")
        analyze = settings.qq_group_assignment("worker", "200")

        self.assertIsNotNone(record)
        self.assertIsNotNone(analyze)
        assert record is not None
        assert analyze is not None
        self.assertTrue(record.tasks.message_detection.record_only)
        self.assertFalse(record.tasks.message_detection.analyze)
        self.assertEqual(record.tasks.message_detection.interval_minutes, 60)
        self.assertTrue(analyze.tasks.message_detection.analyze)
        self.assertTrue(analyze.tasks.message_detection.handle)
        self.assertEqual(analyze.tasks.message_detection.interval_minutes, 15)
        self.assertEqual(analyze.tasks.join_management.minimum_confidence, 0.93)
        self.assertNotIn("tasks", settings.model_dump()["qq"]["bots"][0])
        self.assertNotIn("managed_group_ids", settings.model_dump()["qq"]["bots"][0])

    def test_legacy_bot_tasks_and_groups_migrate_without_loss(self) -> None:
        settings = Settings.model_validate(
            {
                "gui": {"enabled": False},
                "qq": {
                    "bots": [
                        {
                            "id": "legacy",
                            "enabled": True,
                            "managed_group_ids": ["300", "400"],
                            "tasks": {
                                "message_detection": {
                                    "record_only": True,
                                    "interval_minutes": 45,
                                },
                                "announcement_sync": {"auto_sync": True},
                            },
                        }
                    ]
                },
            }
        )

        assignments = settings.qq_group_assignments("legacy")
        self.assertEqual([item.group_id for item in assignments], ["300", "400"])
        self.assertTrue(all(item.tasks.message_detection.record_only for item in assignments))
        self.assertTrue(all(item.tasks.announcement_sync.auto_sync for item in assignments))
        self.assertTrue(all(item.tasks.announcement_sync.enabled for item in assignments))

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "config.yaml"
            settings.save(path)
            saved = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertNotIn("tasks", saved["qq"]["bots"][0])
        self.assertNotIn("managed_group_ids", saved["qq"]["bots"][0])
        self.assertEqual(len(saved["orchestration"]["edges"]), 2)
        self.assertTrue(
            all(
                edge["tasks"]["message_detection"]["record_only"]
                for edge in saved["orchestration"]["edges"]
            )
        )

    def test_tasks_are_rejected_on_non_assignment_edges(self) -> None:
        with self.assertRaises(ValidationError):
            Settings.model_validate(
                {
                    "gui": {"enabled": False},
                    "orchestration": {
                        "edges": [
                            {
                                "id": "invalid-tasks",
                                "source": "qq-bot:default",
                                "target": "feishu-bot:default",
                                "relation": "searches",
                                "tasks": {"message_detection": {"record_only": True}},
                            }
                        ]
                    },
                }
            )

    def test_non_assignment_edges_do_not_serialize_empty_task_blocks(self) -> None:
        settings = Settings.model_validate(
            {
                "gui": {"enabled": False},
                "orchestration": {
                    "edges": [
                        {
                            "id": "search-edge",
                            "source": "qq-bot:default",
                            "target": "feishu-bot:default",
                            "relation": "searches",
                        }
                    ]
                },
            }
        )

        self.assertNotIn("tasks", settings.model_dump()["orchestration"]["edges"][0])

    def test_duplicate_active_assignments_are_rejected(self) -> None:
        config = assignment_settings().model_dump(mode="json")
        duplicate = config["orchestration"]["edges"][0].copy()
        duplicate.update({"id": "worker-record-duplicate", "relation": "manages"})
        config["orchestration"]["edges"].append(duplicate)
        with self.assertRaises(ValidationError):
            Settings.model_validate(config)


class ConfigurationPathTests(unittest.TestCase):
    def test_gui_receives_the_cli_selected_configuration_path(self) -> None:
        settings = Settings.model_validate({"gui": {"enabled": True}})
        selected = Path("data/custom-config.yaml")
        with (
            patch("neoqbot.app.build_container", return_value=object()),
            patch("neoqbot.app.register_gui") as register,
        ):
            create_app(settings, config_path=selected)

        self.assertEqual(register.call_args.kwargs["config_path"], selected)


class _MessageDatabase:
    def __init__(self) -> None:
        self.saved: list[GroupMessage] = []

    def save_message(self, message: GroupMessage) -> bool:
        self.saved.append(message)
        return True


class _Recorder:
    def __init__(self) -> None:
        self.saved: list[GroupMessage] = []

    def append(self, message: GroupMessage) -> None:
        self.saved.append(message)


class ServiceIsolationTests(unittest.TestCase):
    def test_message_capture_uses_the_current_group_assignment(self) -> None:
        settings = assignment_settings()
        database = _MessageDatabase()
        recorder = _Recorder()
        service = ModerationService(
            settings,
            database,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            settings.qq_bot("worker"),
            recorder,  # type: ignore[arg-type]
        )
        sent_at = datetime.now(UTC)

        captured = service.capture(
            GroupMessage(
                bot_id="worker",
                message_id="m1",
                group_id="100",
                user_id="u1",
                text="record me",
                sent_at=sent_at,
            )
        )
        ignored = service.capture(
            GroupMessage(
                bot_id="worker",
                message_id="m2",
                group_id="999",
                user_id="u1",
                text="outside orchestration",
                sent_at=sent_at,
            )
        )

        self.assertTrue(captured)
        self.assertFalse(ignored)
        self.assertEqual([item.message_id for item in database.saved], ["m1"])
        self.assertEqual([item.message_id for item in recorder.saved], ["m1"])


class _RuntimeService:
    def __init__(self) -> None:
        self.groups: list[str] = []

    async def run_group(self, group_id: str, _: datetime | None = None) -> str:
        self.groups.append(group_id)
        return "ok"

    async def sync_group(self, group_id: str) -> dict[str, int]:
        self.groups.append(group_id)
        return {"synced": 1}


class RuntimeIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_jobs_only_run_groups_with_the_matching_task(self) -> None:
        settings = assignment_settings()
        moderation = _RuntimeService()
        announcements = _RuntimeService()
        runtime = Runtime(
            settings,
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            {"worker": moderation},  # type: ignore[arg-type]
            {"worker": announcements},  # type: ignore[arg-type]
        )

        moderation_result = await runtime.run_bot_moderation("worker", datetime.now(UTC))
        announcement_result = await runtime.sync_bot_announcements("worker")

        self.assertEqual(moderation_result, {"200": "ok"})
        self.assertEqual(moderation.groups, ["200"])
        self.assertEqual(announcement_result, {"200": {"synced": 1}})
        self.assertEqual(announcements.groups, ["200"])


if __name__ == "__main__":
    unittest.main()
