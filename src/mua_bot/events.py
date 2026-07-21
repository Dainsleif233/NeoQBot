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
        join_service: JoinApprovalService,
        moderation_service: ModerationService,
        search_service: SearchService,
    ) -> None:
        self.join_service = join_service
        self.moderation_service = moderation_service
        self.search_service = search_service

    async def handle(self, event: dict[str, Any]) -> str:
        post_type = event.get("post_type")
        if post_type == "request" and event.get("request_type") == "group":
            event_id = _event_id(event)
            request = JoinRequest(
                event_id=event_id,
                flag=str(event.get("flag") or event_id),
                group_id=str(event.get("group_id", "")),
                user_id=str(event.get("user_id", "")),
                comment=str(event.get("comment") or ""),
                sub_type=str(event.get("sub_type") or "add"),
                received_at=_event_time(event),
            )
            return await self.join_service.handle(request)

        if post_type == "message":
            text = onebot_plain_text(event.get("message", event.get("raw_message", "")))
            message_type = event.get("message_type")
            if message_type == "group":
                message = GroupMessage(
                    message_id=str(event.get("message_id") or _event_id(event)),
                    group_id=str(event.get("group_id", "")),
                    user_id=str(event.get("user_id", "")),
                    text=text,
                    sent_at=_event_time(event),
                    raw_event=event,
                )
                return "captured" if self.moderation_service.capture(message) else "ignored"
            if message_type == "private":
                return await self.search_service.handle_admin_message(
                    str(event.get("user_id", "")), text
                )
        return "ignored"
