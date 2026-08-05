from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class JoinRequest(BaseModel):
    bot_id: str = "default"
    event_id: str
    flag: str
    group_id: str
    user_id: str
    comment: str = ""
    sub_type: str = "add"
    received_at: datetime = Field(default_factory=utc_now)


class JoinDecision(BaseModel):
    decision: Literal["approve", "reject", "manual_review"]
    confidence: float = Field(ge=0, le=1)
    reason: str
    matched_rules: list[str] = Field(default_factory=list)


class GroupMessage(BaseModel):
    bot_id: str = "default"
    message_id: str
    group_id: str
    user_id: str
    text: str
    sent_at: datetime
    raw_event: dict[str, Any] = Field(default_factory=dict)


class ModerationFinding(BaseModel):
    category: str
    severity: Literal["low", "medium", "high", "critical"]
    risk_score: float = Field(ge=0, le=1)
    reason: str
    message_ids: list[str] = Field(default_factory=list)
    excerpts: list[str] = Field(default_factory=list)


class ModerationResult(BaseModel):
    safe: bool
    summary: str
    findings: list[ModerationFinding] = Field(default_factory=list)

    @property
    def max_risk(self) -> float:
        return max((item.risk_score for item in self.findings), default=0.0)


class Announcement(BaseModel):
    bot_id: str = "default"
    announcement_id: str
    group_id: str
    title: str = ""
    content: str
    author_id: str = ""
    published_at: datetime | None = None
    source_payload: dict[str, Any] = Field(default_factory=dict)


class SearchHit(BaseModel):
    title: str
    snippet: str
    url: str = ""
