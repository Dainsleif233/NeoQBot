from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from .adapters.onebot import onebot_plain_text
from .models import GroupMessage, JoinRequest
from .services import JoinApprovalService, ModerationService, SearchService


def _event_id(event: dict[str, Any]) -> str:
    explicit = event.get("id") or event.get("event_id") or event.get("message_id")
    if explicit is not None:
        return str(explicit)
    encoded = json.dumps(event, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _event_time(event: dict[str, Any]) -> datetime:
    try:
        return datetime.fromtimestamp(float(event.get("time")), tz=UTC)
    except (TypeError, ValueError, OSError):
        return datetime.now(UTC)


class EventHandler:
    def __init__(
        self,
        join_service: JoinApprovalService | dict[str, JoinApprovalService],
        moderation_service: ModerationService | dict[str, ModerationService],
        search_service: SearchService | dict[str, SearchService],
    ) -> None:
        self.join_services = (
            join_service if isinstance(join_service, dict) else {"default": join_service}
        )
        self.moderation_services = (
            moderation_service
            if isinstance(moderation_service, dict)
            else {"default": moderation_service}
        )
        self.search_services = (
            search_service if isinstance(search_service, dict) else {"default": search_service}
        )

    async def handle(self, event: dict[str, Any], bot_id: str = "default") -> str:
        join_service = self.join_services.get(bot_id)
        moderation_service = self.moderation_services.get(bot_id)
        search_service = self.search_services.get(bot_id)
        if join_service is None or moderation_service is None or search_service is None:
            return "unknown_bot"
        post_type = event.get("post_type")
        if post_type == "request" and event.get("request_type") == "group":
            event_id = _event_id(event)
            request = JoinRequest(
                bot_id=bot_id,
                event_id=event_id,
                flag=str(event.get("flag") or event_id),
                group_id=str(event.get("group_id", "")),
                user_id=str(event.get("user_id", "")),
                comment=str(event.get("comment") or ""),
                sub_type=str(event.get("sub_type") or "add"),
                received_at=_event_time(event),
            )
            return await join_service.handle(request)

        if post_type == "notice":
            notice_type = event.get("notice_type")
            if notice_type == "group_increase":
                sub_type = event.get("sub_type")
                if sub_type == "approve":
                    return await join_service.record_admin_approval(event)
                # "invite" = an admin invited a member (not a join-request resolution); ignore.
                return "ignored"
            return "ignored"

        if post_type == "message":
            text = onebot_plain_text(event.get("message", event.get("raw_message", "")))
            message_type = event.get("message_type")
            if message_type == "group":
                sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
                message = GroupMessage(
                    bot_id=bot_id,
                    message_id=str(event.get("message_id") or _event_id(event)),
                    group_id=str(event.get("group_id", "")),
                    user_id=str(event.get("user_id", "")),
                    sender_name=str(
                        sender.get("card") or sender.get("nickname") or event.get("user_id", "")
                    ),
                    text=text,
                    sent_at=_event_time(event),
                    raw_event=event,
                )
                return "captured" if moderation_service.capture(message) else "ignored"
            if message_type == "private":
                return await search_service.handle_admin_message(
                    str(event.get("user_id", "")), text
                )
        return "ignored"
