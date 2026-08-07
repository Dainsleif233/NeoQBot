from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from neoqbot.adapters.onebot import onebot_plain_text
from neoqbot.app import create_app
from neoqbot.auth import GuiSession
from neoqbot.config import Settings
from neoqbot.database import Database
from neoqbot.gui import _record_range_cutoff
from neoqbot.models import Announcement, GroupMessage, JoinRequest
from neoqbot.recording import LocalMessageRecorder


def announcement(
    bot_id: str,
    *,
    content: str = "Welcome",
    announcement_id: str = "notice-1",
    published_at: datetime | None = None,
) -> Announcement:
    return Announcement(
        bot_id=bot_id,
        announcement_id=announcement_id,
        group_id="100",
        title="Group rules",
        content=content,
        author_id="owner",
        published_at=published_at,
    )


class OneBotPlainTextTests(unittest.TestCase):
    def test_structured_notice_message_uses_its_text_field(self) -> None:
        self.assertEqual(
            onebot_plain_text(
                {
                    "text": "公告正文",
                    "image": [{"id": "image-1", "width": 1200, "height": 600}],
                }
            ),
            "公告正文",
        )

    def test_onebot_segment_dict_uses_its_text_data(self) -> None:
        self.assertEqual(
            onebot_plain_text({"type": "text", "data": {"text": "segment text"}}),
            "segment text",
        )

    def test_saved_onebot_segment_list_payload_uses_text_data(self) -> None:
        self.assertEqual(
            Database._announcement_payload_text(
                {
                    "message": [
                        {"type": "text", "data": {"text": "archived text"}},
                        {"type": "image", "data": {"file": "image.png"}},
                    ]
                }
            ),
            "archived text",
        )


class AnnouncementArchiveTests(unittest.TestCase):
    def test_cross_bot_sync_is_group_level_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            database = Database(Path(directory) / "records.db")
            database.initialize()

            self.assertTrue(database.upsert_announcement(announcement("bot-a")))
            self.assertFalse(database.upsert_announcement(announcement("bot-b")))

            records = database.recent_records("announcements", group_id="100", limit=10)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["source_bot_ids_json"], ["bot-a", "bot-b"])

    def test_source_author_variance_does_not_create_a_false_version(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            database = Database(Path(directory) / "records.db")
            database.initialize()
            first = announcement("bot-a").model_copy(update={"author_id": ""})
            second = announcement("bot-b")

            self.assertTrue(database.upsert_announcement(first))
            self.assertFalse(database.upsert_announcement(second))
            records = database.recent_records("announcements", group_id="100", limit=10)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["author_id"], "owner")

    def test_transport_damaged_same_source_slot_is_idempotent_and_keeps_clean_text(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            database = Database(Path(directory) / "records.db")
            database.initialize()
            published = datetime(2026, 8, 6, 4, 15, 7, tzinfo=UTC)
            clean = "群公告同步应保存完整正文，并在后续轮询中保持唯一记录。"
            damaged = clean.replace("完整", "完\ufffd", 1)

            self.assertTrue(
                database.upsert_announcement(
                    announcement("bot-a", content=damaged, published_at=published)
                )
            )
            self.assertFalse(
                database.upsert_announcement(
                    announcement("bot-a", content=clean, published_at=published)
                )
            )
            self.assertFalse(
                database.upsert_announcement(
                    announcement("bot-a", content=damaged, published_at=published)
                )
            )

            records = database.recent_records("announcements", group_id="100", limit=10)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["content"], clean)
            self.assertEqual(
                records[0]["content_hash"],
                database._announcement_hash(
                    announcement("bot-a", content=clean, published_at=published)
                ),
            )

    def test_transport_damaged_versions_without_publish_time_are_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            database = Database(Path(directory) / "records.db")
            database.initialize()
            clean = "A notice without a publish timestamp should still remain unique."
            damaged = clean.replace("timestamp", "time\ufffdstamp", 1)

            self.assertTrue(database.upsert_announcement(announcement("bot-a", content=damaged)))
            self.assertFalse(database.upsert_announcement(announcement("bot-b", content=clean)))

            records = database.recent_records("announcements", group_id="100", limit=10)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["content"], clean)

    def test_transport_damage_does_not_merge_known_and_missing_publish_slots(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            database = Database(Path(directory) / "records.db")
            database.initialize()
            published = datetime(2026, 8, 6, 4, 15, 7, tzinfo=UTC)
            clean = "A timestamped revision must not absorb an undated archived version."
            damaged = clean.replace("archived", "arch\ufffdved", 1)

            self.assertTrue(database.upsert_announcement(announcement("bot-a", content=damaged)))
            self.assertTrue(
                database.upsert_announcement(
                    announcement("bot-a", content=clean, published_at=published)
                )
            )

            records = database.recent_records("announcements", group_id="100", limit=10)
            self.assertEqual(len(records), 2)
            self.assertEqual(sum(int(record["is_current"]) for record in records), 1)

    def test_transport_marker_does_not_hide_a_real_small_edit(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            database = Database(Path(directory) / "records.db")
            database.initialize()
            published = datetime(2026, 8, 6, 4, 15, 7, tzinfo=UTC)
            original = "Policy version one: " + "stable announcement text. " * 20
            revised = original.replace("version one", "version two").replace(
                "text", "te\ufffdxt", 1
            )

            self.assertTrue(
                database.upsert_announcement(
                    announcement("bot-a", content=original, published_at=published)
                )
            )
            self.assertTrue(
                database.upsert_announcement(
                    announcement("bot-a", content=revised, published_at=published)
                )
            )

            records = database.recent_records("announcements", group_id="100", limit=10)
            self.assertEqual(len(records), 2)
            current = next(record for record in records if record["is_current"])
            self.assertEqual(current["content"], revised)

    def test_transport_body_upgrade_merges_published_content_conflict(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            database = Database(Path(directory) / "records.db")
            database.initialize()
            published = datetime(2026, 8, 6, 4, 15, 7, tzinfo=UTC)
            clean = "The announcement body is complete and stable."
            damaged = clean.replace("complete", "comp\ufffdete", 1)

            self.assertTrue(
                database.upsert_announcement(
                    announcement(
                        "bot-a",
                        announcement_id="notice-a",
                        content=damaged,
                        published_at=published,
                    )
                )
            )
            self.assertTrue(
                database.upsert_announcement(
                    announcement(
                        "bot-b",
                        announcement_id="notice-b",
                        content=clean,
                        published_at=published,
                    )
                )
            )
            self.assertFalse(
                database.upsert_announcement(
                    announcement(
                        "bot-a",
                        announcement_id="notice-a",
                        content=clean,
                        published_at=published,
                    )
                )
            )

            records = database.recent_records("announcements", group_id="100", limit=10)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["content"], clean)
            self.assertEqual(records[0]["source_bot_ids_json"], ["bot-a", "bot-b"])
            self.assertEqual(records[0]["source_announcement_ids_json"], ["notice-a", "notice-b"])

    def test_same_content_and_publish_minute_deduplicates_different_notice_ids(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            database = Database(Path(directory) / "records.db")
            database.initialize()
            published = datetime(2026, 8, 6, 4, 15, 7, tzinfo=UTC)

            self.assertTrue(
                database.upsert_announcement(
                    announcement("bot-a", announcement_id="notice-a", published_at=published)
                )
            )
            self.assertFalse(
                database.upsert_announcement(
                    announcement(
                        "bot-b",
                        announcement_id="notice-b",
                        published_at=published + timedelta(seconds=40),
                    )
                )
            )

            records = database.recent_records("announcements", group_id="100", limit=10)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["source_bot_ids_json"], ["bot-a", "bot-b"])
            self.assertEqual(records[0]["source_announcement_ids_json"], ["notice-a", "notice-b"])

            self.assertEqual(database.reconcile_announcements("100", {"notice-b"}), 0)
            record = database.recent_records("announcements", group_id="100", limit=1)[0]
            self.assertFalse(record["is_deleted"])

    def test_same_content_at_a_different_time_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            database = Database(Path(directory) / "records.db")
            database.initialize()
            published = datetime(2026, 8, 6, 4, 15, tzinfo=UTC)

            self.assertTrue(
                database.upsert_announcement(
                    announcement("bot-a", announcement_id="notice-a", published_at=published)
                )
            )
            self.assertTrue(
                database.upsert_announcement(
                    announcement(
                        "bot-a",
                        announcement_id="notice-b",
                        published_at=published + timedelta(minutes=2),
                    )
                )
            )

            records = database.recent_records("announcements", group_id="100", limit=10)
            self.assertEqual(len(records), 2)

    def test_merged_notice_ids_keep_one_version_lineage(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            database = Database(Path(directory) / "records.db")
            database.initialize()
            published = datetime(2026, 8, 6, 4, 15, tzinfo=UTC)
            original_a = announcement("bot-a", announcement_id="notice-a", published_at=published)
            original_b = announcement("bot-b", announcement_id="notice-b", published_at=published)
            updated_b = announcement(
                "bot-b",
                announcement_id="notice-b",
                content="Updated rules",
                published_at=published,
            )

            database.upsert_announcement(original_a)
            database.upsert_announcement(original_b)
            self.assertTrue(database.upsert_announcement(updated_b))
            self.assertFalse(database.upsert_announcement(original_a))

            records = database.recent_records("announcements", group_id="100", limit=10)
            self.assertEqual(len(records), 2)
            self.assertEqual(sum(int(item["is_current"]) for item in records), 1)
            current = next(item for item in records if item["is_current"])
            self.assertEqual(current["content"], "Updated rules")
            self.assertEqual(current["source_announcement_ids_json"], ["notice-a", "notice-b"])

    def test_versions_are_preserved_and_deleted_notices_are_tagged(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            database = Database(Path(directory) / "records.db")
            database.initialize()
            database.upsert_announcement(announcement("bot-a"))
            database.upsert_announcement(announcement("bot-a", content="Updated rules"))

            records = database.recent_records("announcements", group_id="100", limit=10)
            self.assertEqual(len(records), 2)
            self.assertEqual(sum(int(item["is_current"]) for item in records), 1)

            self.assertEqual(database.reconcile_announcements("100", set()), 1)
            records = database.recent_records("announcements", group_id="100", limit=10)
            self.assertTrue(all(item["is_deleted"] for item in records))
            self.assertTrue(all(item["deleted_at"] for item in records))

    def test_pending_archive_rows_are_claimed_once(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            database = Database(Path(directory) / "records.db")
            database.initialize()
            database.upsert_announcement(announcement("bot-a"))

            first = database.claim_pending_announcements(group_id="100")
            second = database.claim_pending_announcements(group_id="100")

            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])
            database.mark_announcement_sync(first[0][0], True)
            self.assertEqual(database.claim_pending_announcements(group_id="100"), [])

    def test_initialize_collapses_existing_cross_bot_duplicates(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "legacy.db"
            database = Database(path)
            database.initialize()
            content_hash = database._announcement_hash(announcement("bot-a"))
            now = datetime.now(UTC).isoformat()
            connection = sqlite3.connect(path)
            try:
                connection.execute("DROP INDEX uq_announcements_group_version")
                connection.execute("DROP INDEX uq_announcements_group_published_content")
                connection.execute(
                    """
                    INSERT INTO announcements (
                        bot_id, announcement_id, group_id, content_hash, title, content,
                        author_id, published_at, source_payload_json, first_seen_at,
                        last_seen_at, source_bot_ids_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?)
                    """,
                    (
                        "bot-a",
                        "notice-1",
                        "100",
                        content_hash,
                        "Group rules",
                        "Welcome",
                        "owner",
                        now,
                        now,
                        now,
                        '["bot-a"]',
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO announcements (
                        bot_id, announcement_id, group_id, content_hash, title, content,
                        author_id, published_at, source_payload_json, first_seen_at,
                        last_seen_at, source_bot_ids_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?)
                    """,
                    (
                        "bot-b",
                        "notice-2",
                        "100",
                        content_hash,
                        "Group rules",
                        "Welcome",
                        "owner",
                        now,
                        now,
                        now,
                        '["bot-b"]',
                    ),
                )
                connection.execute("DELETE FROM migration_markers")
                connection.commit()
            finally:
                connection.close()

            database.initialize()
            records = database.recent_records("announcements", group_id="100", limit=10)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["source_bot_ids_json"], ["bot-a", "bot-b"])
            self.assertEqual(records[0]["source_announcement_ids_json"], ["notice-1", "notice-2"])

    def test_initialize_collapses_transport_damaged_source_versions(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "legacy.db"
            database = Database(path)
            database.initialize()
            published = datetime(2026, 8, 6, 4, 15, 7, tzinfo=UTC)
            clean = "群公告同步应保存完整正文，并在后续轮询中保持唯一记录。"
            damaged = clean.replace("完整", "完\ufffd", 1)
            now = datetime.now(UTC).isoformat()
            connection = sqlite3.connect(path)
            try:
                for content in (damaged, clean):
                    stored_content = f"{{'text': {content!r}, 'image': []}}"
                    source_payload = json.dumps(
                        {"message": {"text": content, "image": []}}, ensure_ascii=False
                    )
                    connection.execute(
                        """
                        INSERT INTO announcements (
                            bot_id, announcement_id, group_id, content_hash, title, content,
                            author_id, published_at, source_payload_json, first_seen_at,
                            last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "bot-a",
                            "notice-1",
                            "100",
                            database._announcement_hash(
                                announcement(
                                    "bot-a", content=stored_content, published_at=published
                                )
                            ),
                            "Group rules",
                            stored_content,
                            "owner",
                            published.isoformat(),
                            source_payload,
                            now,
                            now,
                        ),
                    )
                connection.execute("DELETE FROM migration_markers")
                connection.commit()
            finally:
                connection.close()

            database.initialize()
            records = database.recent_records("announcements", group_id="100", limit=10)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["content"], clean)
            self.assertEqual(
                records[0]["content_hash"],
                database._announcement_hash(
                    announcement("bot-a", content=clean, published_at=published)
                ),
            )

    def test_initialize_keeps_content_rollbacks_at_distinct_publish_slots(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "records.db"
            database = Database(path)
            database.initialize()
            published = datetime(2026, 8, 6, 4, 15, 7, tzinfo=UTC)
            versions = [
                ("Version A", published),
                ("Version B", published + timedelta(minutes=1)),
                ("Version A", published + timedelta(minutes=2)),
            ]
            for content, version_time in versions:
                self.assertTrue(
                    database.upsert_announcement(
                        announcement(
                            "bot-a",
                            content=content,
                            published_at=version_time,
                        )
                    )
                )

            connection = sqlite3.connect(path)
            try:
                connection.execute("DELETE FROM migration_markers")
                connection.commit()
            finally:
                connection.close()
            database.initialize()
            records = database.recent_records("announcements", group_id="100", limit=10)
            self.assertEqual(len(records), 3)
            current = next(record for record in records if record["is_current"])
            self.assertEqual(current["content"], "Version A")
            self.assertEqual(
                current["published_slot"],
                (published + timedelta(minutes=2)).replace(second=0).isoformat(),
            )

    def test_initialize_uses_latest_observation_when_clean_keeper_has_an_older_id(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "legacy.db"
            database = Database(path)
            database.initialize()
            published = datetime(2026, 8, 6, 4, 15, 7, tzinfo=UTC)
            clean = "The clean rollback body should remain the current archived version."
            damaged = clean.replace("rollback", "roll\ufffdack", 1)
            revised = "A genuinely different intermediate announcement revision."
            observed = datetime(2026, 8, 6, 5, 0, tzinfo=UTC)
            connection = sqlite3.connect(path)
            try:
                connection.execute("DROP INDEX uq_announcements_group_version")
                connection.execute("DROP INDEX uq_announcements_group_published_content")
                connection.execute("DELETE FROM announcements")
                for index, (content, seen_at) in enumerate(
                    (
                        (clean, observed),
                        (revised, observed + timedelta(minutes=1)),
                        (damaged, observed + timedelta(minutes=2)),
                    ),
                    start=1,
                ):
                    source_payload = json.dumps(
                        {"message": {"text": content, "image": []}}, ensure_ascii=False
                    )
                    connection.execute(
                        """
                        INSERT INTO announcements (
                            bot_id, announcement_id, group_id, content_hash, title, content,
                            author_id, published_at, source_payload_json, first_seen_at,
                            last_seen_at, published_slot
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "bot-a",
                            "notice-1",
                            "100",
                            database._announcement_hash(
                                announcement("bot-a", content=content, published_at=published)
                            ),
                            "Group rules",
                            content,
                            "owner",
                            published.isoformat(),
                            source_payload,
                            (observed + timedelta(seconds=index)).isoformat(),
                            seen_at.isoformat(),
                            published.replace(second=0).isoformat(),
                        ),
                    )
                connection.execute("DELETE FROM migration_markers")
                connection.commit()
            finally:
                connection.close()

            database.initialize()
            records = database.recent_records("announcements", group_id="100", limit=10)
            self.assertEqual(len(records), 2)
            current = next(record for record in records if record["is_current"])
            self.assertEqual(current["content"], clean)
            self.assertEqual(current["last_seen_at"], (observed + timedelta(minutes=2)).isoformat())

    def test_announcement_archive_migration_runs_only_once_after_marker_is_written(self) -> None:
        class CountingDatabase(Database):
            def __init__(self, path: Path) -> None:
                super().__init__(path)
                self.migration_runs = 0

            def _migrate_announcement_archive(self, connection: sqlite3.Connection) -> None:
                self.migration_runs += 1
                super()._migrate_announcement_archive(connection)

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            database = CountingDatabase(Path(directory) / "records.db")
            database.initialize()
            database.initialize()
            self.assertEqual(database.migration_runs, 1)

    def test_initialize_rebuilds_legacy_content_version_unique_constraint(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    CREATE TABLE announcements (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        bot_id TEXT NOT NULL DEFAULT 'default',
                        announcement_id TEXT NOT NULL,
                        group_id TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        title TEXT NOT NULL,
                        content TEXT NOT NULL,
                        author_id TEXT NOT NULL,
                        published_at TEXT,
                        source_payload_json TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        sync_status TEXT NOT NULL DEFAULT 'pending',
                        sync_attempts INTEGER NOT NULL DEFAULT 0,
                        sync_error TEXT,
                        sync_claimed_at TEXT,
                        is_current INTEGER NOT NULL DEFAULT 1,
                        is_deleted INTEGER NOT NULL DEFAULT 0,
                        deleted_at TEXT,
                        source_bot_ids_json TEXT NOT NULL DEFAULT '[]',
                        source_announcement_ids_json TEXT NOT NULL DEFAULT '[]',
                        published_slot TEXT NOT NULL DEFAULT '',
                        UNIQUE(bot_id, group_id, announcement_id, content_hash)
                    )
                    """
                )
                connection.commit()
            finally:
                connection.close()

            database = Database(path)
            database.initialize()
            published = datetime(2026, 8, 6, 4, 15, 7, tzinfo=UTC)
            self.assertTrue(
                database.upsert_announcement(
                    announcement("bot-a", content="Version A", published_at=published)
                )
            )
            self.assertTrue(
                database.upsert_announcement(
                    announcement(
                        "bot-a",
                        content="Version B",
                        published_at=published + timedelta(minutes=1),
                    )
                )
            )
            self.assertTrue(
                database.upsert_announcement(
                    announcement(
                        "bot-a",
                        content="Version A",
                        published_at=published + timedelta(minutes=2),
                    )
                )
            )

            records = database.recent_records("announcements", group_id="100", limit=10)
            self.assertEqual(len(records), 3)


class GroupRecordManagementTests(unittest.TestCase):
    def test_clear_group_removes_only_selected_recent_jsonl_messages(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            recorder = LocalMessageRecorder(Path(directory) / "messages")
            old = datetime.now(UTC) - timedelta(days=10)
            recent = datetime.now(UTC) - timedelta(hours=1)
            for message_id, sent_at in (("old", old), ("recent", recent)):
                recorder.append(
                    GroupMessage(
                        bot_id="bot-a",
                        message_id=message_id,
                        group_id="100",
                        user_id="user",
                        text=message_id,
                        sent_at=sent_at,
                    )
                )

            result = recorder.clear_group("100", since=datetime.now(UTC) - timedelta(days=1))

            self.assertEqual(result["records"], 1)
            remaining = list((Path(directory) / "messages").glob("*/*/*.jsonl"))
            self.assertEqual(len(remaining), 1)
            self.assertIn('"message_id":"old"', remaining[0].read_text(encoding="utf-8"))

    def test_today_cutoff_uses_browser_timezone(self) -> None:
        now = datetime(2026, 8, 6, 2, 30, tzinfo=UTC)
        cutoff = _record_range_cutoff("today", -480, now=now)
        self.assertEqual(cutoff, datetime(2026, 8, 5, 16, 0, tzinfo=UTC))

    def test_group_workspace_exports_and_clears_records(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            settings = Settings.model_validate(
                {
                    "app": {
                        "database_path": str(root / "workspace.db"),
                        "message_archive_path": str(root / "messages"),
                    },
                    "gui": {
                        "enabled": True,
                        "bootstrap_password": "temporary-test-password",
                    },
                    "qq": {
                        "bots": [
                            {
                                "id": "worker",
                                "name": "Worker",
                                "enabled": True,
                                "connection_mode": "external",
                            }
                        ]
                    },
                    "feishu": {"bots": []},
                    "orchestration": {
                        "resources": [
                            {
                                "id": "group-record",
                                "kind": "qq_group",
                                "name": "Record group",
                                "external_id": "100",
                            }
                        ],
                        "edges": [
                            {
                                "id": "worker-record",
                                "source": "qq-bot:worker",
                                "target": "group-record",
                                "relation": "observes",
                                "tasks": {"message_detection": {"record": True}},
                            }
                        ],
                    },
                }
            )
            app = create_app(settings, config_path=root / "config.yaml")
            app.state.container.auth.session = lambda _: GuiSession(
                username="admin",
                csrf_token="test-csrf",
                must_change_password=False,
                role="admin",
                token_hash="test-token-hash",
            )
            message = GroupMessage(
                bot_id="worker",
                message_id="message-1",
                group_id="100",
                user_id="member",
                sender_name="Member Card",
                text="hello",
                sent_at=datetime.now(UTC),
            )
            app.state.container.database.save_message(message)
            app.state.container.message_recorder.append(message)
            app.state.container.database.upsert_announcement(announcement("worker"))
            app.state.container.database.save_join_request(
                JoinRequest(
                    bot_id="worker",
                    event_id="event-1",
                    flag="flag-1",
                    group_id="100",
                    user_id="candidate",
                    comment="please approve",
                )
            )

            with TestClient(app) as client:
                client.cookies.set("neoqbot_session", "test-session")
                export = client.get(
                    "/api/gui/orchestration/group/export",
                    params={"group_id": "100", "resource_id": "group-record", "kind": "all"},
                )
                wrong_resource = client.get(
                    "/api/gui/orchestration/group",
                    params={"group_id": "100", "resource_id": "another-group"},
                )
                cleared = client.delete(
                    "/api/gui/orchestration/group/records/messages",
                    params={"group_id": "100", "resource_id": "group-record", "range": "all"},
                    headers={"X-CSRF-Token": "test-csrf"},
                )

            self.assertEqual(export.status_code, 200)
            payload = json.loads(export.content)
            self.assertEqual(payload["schema"], "neoqbot.group-records.v1")
            self.assertEqual(payload["counts"]["messages"], 1)
            self.assertEqual(payload["records"]["messages"][0]["sender_name"], "Member Card")
            self.assertEqual(payload["counts"]["announcements"], 1)
            self.assertEqual(payload["counts"]["joins"], 1)
            self.assertIn("attachment", export.headers["content-disposition"])
            self.assertEqual(wrong_resource.status_code, 404)
            self.assertEqual(cleared.status_code, 200)
            self.assertEqual(cleared.json()["database_records"], 1)
            self.assertEqual(cleared.json()["message_archive"]["records"], 1)


if __name__ == "__main__":
    unittest.main()
