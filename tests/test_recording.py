import json
from datetime import UTC, datetime, timedelta

from mua_bot.models import GroupMessage
from mua_bot.recording import LocalMessageRecorder


def test_local_message_recorder_writes_daily_jsonl_and_prunes(tmp_path) -> None:
    recorder = LocalMessageRecorder(tmp_path / "records")
    sent_at = datetime(2026, 7, 28, 10, 30, tzinfo=UTC)
    target = recorder.append(
        GroupMessage(
            bot_id="observer",
            message_id="m1",
            group_id="123456",
            user_id="10001",
            text="只记录这条消息",
            sent_at=sent_at,
            raw_event={"post_type": "message"},
        )
    )

    assert target == tmp_path / "records" / "observer" / "123456" / "2026-07-28.jsonl"
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["message_id"] == "m1"
    assert payload["text"] == "只记录这条消息"
    assert recorder.prune(sent_at - timedelta(days=1)) == 0
    assert recorder.prune(sent_at + timedelta(days=1)) == 1
    assert not target.exists()
