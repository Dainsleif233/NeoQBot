from datetime import UTC, datetime, timedelta
from pathlib import Path

from mua_bot.database import Database
from mua_bot.models import Announcement, GroupMessage


def test_message_idempotency_and_window_query(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    database.initialize()
    message = GroupMessage(
        message_id="m1",
        group_id="g1",
        user_id="u1",
        text="hello",
        sent_at=datetime(2026, 7, 21, 4, 0, tzinfo=UTC),
    )

    assert database.save_message(message) is True
    assert database.save_message(message) is False
    assert (
        len(
            database.messages_between(
                "g1",
                datetime(2026, 7, 21, 3, 59, tzinfo=UTC),
                datetime(2026, 7, 21, 4, 1, tzinfo=UTC),
                10,
            )
        )
        == 1
    )


def test_same_message_id_isolated_between_bots(tmp_path: Path) -> None:
    database = Database(tmp_path / "multi-bot.db")
    database.initialize()
    sent_at = datetime.now(UTC)

    assert database.save_message(
        GroupMessage(
            bot_id="bot-a",
            message_id="same",
            group_id="g1",
            user_id="u1",
            text="from a",
            sent_at=sent_at,
        )
    )
    assert database.save_message(
        GroupMessage(
            bot_id="bot-b",
            message_id="same",
            group_id="g1",
            user_id="u1",
            text="from b",
            sent_at=sent_at,
        )
    )


def test_announcement_content_change_creates_version(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    database.initialize()
    first = Announcement(announcement_id="n1", group_id="g1", content="v1")
    second = Announcement(announcement_id="n1", group_id="g1", content="v2")

    assert database.upsert_announcement(first) is True
    assert database.upsert_announcement(first) is False
    assert database.upsert_announcement(second) is True
    assert len(database.pending_announcements()) == 2


def test_message_limit_keeps_latest_messages_in_chronological_order(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    database.initialize()
    base = datetime(2026, 7, 21, 4, 0, tzinfo=UTC)
    for index in range(5):
        database.save_message(
            GroupMessage(
                message_id=str(index),
                group_id="g1",
                user_id="u1",
                text=str(index),
                sent_at=base.replace(second=index),
            )
        )

    messages = database.messages_between("g1", base, base.replace(minute=1), limit=3)

    assert [message.message_id for message in messages] == ["2", "3", "4"]


def test_retention_prunes_messages_but_keeps_announcements(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    database.initialize()
    old = datetime(2025, 1, 1, tzinfo=UTC)
    database.save_message(
        GroupMessage(message_id="old", group_id="g1", user_id="u1", text="old", sent_at=old)
    )
    database.upsert_announcement(
        Announcement(announcement_id="keep", group_id="g1", content="permanent archive")
    )

    deleted = database.prune(
        messages_before=old + timedelta(days=1),
        joins_before=old,
        moderation_before=old,
        audit_before=old,
    )

    assert deleted["group_messages"] == 1
    assert database.counts()["announcements"] == 1


def test_group_overview_filters_records_by_group_and_bot(tmp_path: Path) -> None:
    database = Database(tmp_path / "overview.db")
    database.initialize()
    sent_at = datetime(2026, 7, 21, 4, 0, tzinfo=UTC)
    for bot_id, group_id, text in (
        ("observer", "g1", "keep"),
        ("worker", "g1", "other bot"),
        ("observer", "g2", "other group"),
    ):
        database.save_message(
            GroupMessage(
                bot_id=bot_id,
                message_id=f"{bot_id}-{group_id}",
                group_id=group_id,
                user_id="u1",
                text=text,
                sent_at=sent_at,
            )
        )

    overview = database.group_overview("g1", bot_ids=["observer"])

    assert overview["counts"]["messages"] == 1
    assert overview["records"]["messages"][0]["text"] == "keep"


def test_recent_records_supports_search_and_offset(tmp_path: Path) -> None:
    database = Database(tmp_path / "records.db")
    database.initialize()
    base = datetime(2026, 7, 21, 4, 0, tzinfo=UTC)
    for index, text in enumerate(("first message", "needle message", "latest message")):
        database.save_message(
            GroupMessage(
                bot_id="observer",
                message_id=str(index),
                group_id="g1",
                user_id="u1",
                text=text,
                sent_at=base + timedelta(seconds=index),
            )
        )

    searched = database.recent_records(
        "messages", 10, bot_id="observer", group_id="g1", search="needle"
    )
    second_page = database.recent_records("messages", 1, offset=1)

    assert [record["text"] for record in searched] == ["needle message"]
    assert [record["text"] for record in second_page] == ["needle message"]
