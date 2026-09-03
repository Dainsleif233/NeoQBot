from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from .config import FeishuBotConfig, QQBotConfig, QQJoinTaskConfig, Settings
from .database import Database
from .models import (
    GroupMessage,
    JoinDecision,
    JoinRequest,
    ModerationResult,
)
from .ports import DecisionEngine, FeishuGateway, QQGateway
from .recording import LocalMessageRecorder

logger = logging.getLogger(__name__)

# Reviewer recorded when the automated pipeline (model + auto action) reviews a
# join request. Human reviews record the GUI operator username instead.
AUTOMATED_REVIEWER = "system"


class JoinApprovalService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        engine: DecisionEngine,
        qq: QQGateway,
        bot: QQBotConfig | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.engine = engine
        self.qq = qq
        self.bot = bot or settings.qq_bot()
        if self.bot is None:
            raise ValueError("No QQ Bot is configured")

    async def handle(self, request: JoinRequest) -> str:
        assignment = self.settings.qq_group_assignment(self.bot.id, request.group_id)
        if assignment is None:
            return "unmanaged_group"
        task = assignment.tasks.join_management
        if not task.enabled or not task.detect_requests:
            return "disabled"
        if not self.database.save_join_request(request):
            return "duplicate"
        if not task.execute_management:
            self.database.audit(
                "join_detected",
                "recorded",
                "join_request",
                request.flag,
                {"bot_id": self.bot.id, "request": request.model_dump(mode="json")},
            )
            return "detected"

        try:
            decision = await self.engine.review_join(
                request, self.settings.join_approval_for_group(assignment.resource_id)
            )
        except Exception as exc:
            logger.exception("Join review failed")
            decision = JoinDecision(
                decision="manual_review",
                confidence=0,
                reason=f"模型审核失败：{type(exc).__name__}",
                matched_rules=["engine_error"],
            )

        threshold_met = decision.confidence >= task.minimum_confidence
        action_status = "manual_review"
        try:
            if decision.decision == "approve" and threshold_met and task.auto_approve:
                await self.qq.approve_join(request, approve=True)
                action_status = "dry_run_approve" if self.settings.app.dry_run else "approved"
            elif decision.decision == "reject" and threshold_met and task.auto_reject:
                await self.qq.approve_join(request, approve=False, reason=decision.reason[:120])
                action_status = "dry_run_reject" if self.settings.app.dry_run else "rejected"
            else:
                await self._notify_manual_review(request, decision, threshold_met, task)
        except Exception as exc:
            logger.exception("Join decision action failed")
            action_status = "action_failed"
            self.database.audit(
                "join_action",
                "failed",
                "join_request",
                request.flag,
                {"error": str(exc), "decision": decision.model_dump()},
            )
            try:
                await self.qq.notify_administrators(
                    "[NeoQBot 入群动作失败]\n"
                    f"群：{request.group_id}\n申请人：{request.user_id}\n"
                    f"模型建议：{decision.decision}\n错误：{type(exc).__name__}"
                )
            except Exception:
                logger.exception("Failed to notify administrators about join action failure")
        reviewer = AUTOMATED_REVIEWER if action_status != "manual_review" else ""
        self.database.update_join_decision(request, decision, action_status, reviewer=reviewer)
        self.database.audit(
            "join_review",
            action_status,
            "join_request",
            request.flag,
            {
                "request": request.model_dump(mode="json"),
                "decision": decision.model_dump(),
                "reviewer": reviewer,
            },
        )
        return action_status

    async def apply_manual_decision(
        self, request: JoinRequest, approve: bool, reason: str, reviewer: str
    ) -> str:
        """Record an administrator's manual approve/reject decision for a join request.

        The reviewer is the GUI operator username so the audit trail captures who
        actually performed the join review. Automated decisions record ``system``
        instead (see :data:`AUTOMATED_REVIEWER`).
        """
        # An already-decided request should not re-invoke the (likely expired) OneBot
        # flag. Admins may still retry after a prior failure (action_failed).
        existing = self.database.get_join_request(
            request.flag, group_id=request.group_id, bot_id=request.bot_id
        )
        terminal_states = {
            "approved",
            "rejected",
            "manual_approve",
            "manual_reject",
            "dry_run_approve",
            "dry_run_reject",
            "dry_run_manual_approve",
            "dry_run_manual_reject",
        }
        if existing is not None and existing.get("action_status") in terminal_states:
            return str(existing["action_status"])

        decision = JoinDecision(
            decision="approve" if approve else "reject",
            confidence=1.0,
            reason=reason or "管理员手动审核",
            matched_rules=["manual_review"],
        )
        dry_run = self.settings.app.dry_run
        approve_status = "dry_run_manual_approve" if dry_run else "manual_approve"
        reject_status = "dry_run_manual_reject" if dry_run else "manual_reject"
        action_status = approve_status if approve else reject_status
        try:
            await self.qq.approve_join(request, approve=approve, reason=reason[:120])
        except Exception as exc:
            logger.exception("Manual join action failed")
            action_status = "action_failed"
            self.database.audit(
                "join_action",
                "failed",
                "join_request",
                request.flag,
                {"error": str(exc), "reviewer": reviewer, "manual": True},
            )
        self.database.update_join_decision(request, decision, action_status, reviewer=reviewer)
        self.database.audit(
            "join_review",
            action_status,
            "join_request",
            request.flag,
            {
                "reviewer": reviewer,
                "manual": True,
                "request": request.model_dump(mode="json"),
                "decision": decision.model_dump(),
            },
        )
        return action_status

    async def _notify_manual_review(
        self,
        request: JoinRequest,
        decision: JoinDecision,
        threshold_met: bool,
        task: QQJoinTaskConfig,
    ) -> None:
        if not threshold_met:
            execution_note = "置信度不足"
        elif decision.decision == "approve" and not task.auto_approve:
            execution_note = "自动同意未启用"
        elif decision.decision == "reject" and not task.auto_reject:
            execution_note = "自动拒绝未启用"
        else:
            execution_note = "模型要求人工复核"
        message = (
            "[NeoQBot 入群审核]\n"
            f"群：{request.group_id}\n申请人：{request.user_id}\n"
            f"申请内容：{request.comment or '(空)'}\n"
            f"建议：{decision.decision}（置信度 {decision.confidence:.2f}）\n"
            f"原因：{decision.reason}\n"
            f"未自动执行：{execution_note}；本次转人工。"
        )
        await self.qq.notify_administrators(message)


class ModerationService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        engine: DecisionEngine,
        qq: QQGateway,
        bot: QQBotConfig | None = None,
        recorder: LocalMessageRecorder | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.engine = engine
        self.qq = qq
        self.recorder = recorder
        self.bot = bot or settings.qq_bot()
        if self.bot is None:
            raise ValueError("No QQ Bot is configured")

    def capture(self, message: GroupMessage) -> bool:
        assignment = self.settings.qq_group_assignment(self.bot.id, message.group_id)
        if assignment is None:
            return False
        task = assignment.tasks.message_detection
        if not task.record and not task.scheduled_analysis:
            return False
        saved = self.database.save_message(message)
        if saved and task.record and self.recorder is not None:
            try:
                self.recorder.append(message)
            except OSError as exc:
                logger.exception("Failed to append local group message archive")
                self.database.audit(
                    "message_archive",
                    "failed",
                    "group_message",
                    message.message_id,
                    {"bot_id": message.bot_id, "group_id": message.group_id, "error": str(exc)},
                )
        return saved

    async def run_group(self, group_id: str, window_end: datetime | None = None) -> str:
        assignment = self.settings.qq_group_assignment(self.bot.id, group_id)
        if assignment is None:
            return "unmanaged_group"
        task = assignment.tasks.message_detection
        if not task.scheduled_analysis:
            return "disabled"
        end = (window_end or datetime.now(UTC)).astimezone(UTC)
        start = end - timedelta(minutes=task.window_minutes)
        if self.database.moderation_run_exists(group_id, start, end, self.bot.id):
            return "duplicate"
        messages = self.database.messages_between(
            group_id, start, end, task.max_messages_per_run, self.bot.id
        )
        if not messages:
            result = ModerationResult(safe=True, summary="监测窗口内没有群聊文本消息")
        else:
            try:
                result = await self.engine.moderate_messages(messages)
            except Exception as exc:
                logger.exception("Moderation failed for group %s", group_id)
                self.database.audit(
                    "moderation",
                    "failed",
                    "group",
                    group_id,
                    {"window_start": start, "window_end": end, "error": str(exc)},
                )
                return "failed"

        alert = bool(result.findings and result.max_risk >= task.risk_threshold)
        alert_delivered = False
        alert_failed = False
        if alert:
            try:
                await self.qq.notify_administrators(
                    self._format_alert(group_id, start, end, len(messages), result)
                )
                alert_delivered = True
            except Exception as exc:
                logger.exception("Failed to deliver moderation alert")
                alert_failed = True
                self.database.audit(
                    "moderation_alert",
                    "failed",
                    "group",
                    group_id,
                    {"error": str(exc), "result": result.model_dump()},
                )
        self.database.save_moderation_run(
            group_id,
            start,
            end,
            len(messages),
            result.max_risk,
            result.model_dump(mode="json"),
            alert_delivered,
            self.bot.id,
        )
        audit_status = (
            "alerted" if alert_delivered else "alert_failed" if alert_failed else "completed"
        )
        self.database.audit(
            "moderation",
            audit_status,
            "group",
            group_id,
            {
                "window_start": start,
                "window_end": end,
                "message_count": len(messages),
                "max_risk": result.max_risk,
            },
        )
        if alert_delivered:
            return "alerted"
        if alert_failed:
            return "alert_failed"
        return "safe"

    @staticmethod
    def _format_alert(
        group_id: str,
        start: datetime,
        end: datetime,
        message_count: int,
        result: ModerationResult,
    ) -> str:
        lines = [
            "[NeoQBot 群聊风险提醒]",
            f"群：{group_id}",
            f"窗口：{start.isoformat()} ~ {end.isoformat()}",
            f"消息数：{message_count}；最高风险：{result.max_risk:.2f}",
            f"摘要：{result.summary}",
        ]
        for index, finding in enumerate(result.findings[:5], start=1):
            lines.append(
                f"{index}. [{finding.severity}/{finding.category}] {finding.reason} "
                f"(risk={finding.risk_score:.2f}, messages={','.join(finding.message_ids[:8])})"
            )
            for excerpt in finding.excerpts[:3]:
                lines.append(f"   引用：{excerpt[:160]}")
        lines.append("请管理员结合上下文人工确认；NeoQBot 不自动禁言或踢人。")
        return "\n".join(lines)


class AnnouncementService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        qq: QQGateway,
        feishu: FeishuGateway | dict[str, FeishuGateway],
        bot: QQBotConfig | None = None,
        feishu_config: FeishuBotConfig | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.qq = qq
        self.feishu_clients = feishu if isinstance(feishu, dict) else {}
        self.bot = bot or settings.qq_bot()
        self.default_feishu_config = feishu_config or next(
            (item for item in settings.effective_feishu_bots() if item.enabled),
            settings.feishu_bot(),
        )
        if not isinstance(feishu, dict) and self.default_feishu_config is not None:
            self.feishu_clients[self.default_feishu_config.id] = feishu
        if self.bot is None:
            raise ValueError("No QQ Bot is configured")

    async def sync_group(self, group_id: str) -> dict[str, object]:
        assignment = self.settings.qq_group_assignment(self.bot.id, group_id)
        if assignment is None:
            return {"fetched": 0, "new": 0, "synced": 0, "failed": 0}
        if not assignment.tasks.announcement_sync.enabled:
            return {"fetched": 0, "new": 0, "synced": 0, "failed": 0}
        announcements = []
        fetch_error = ""
        try:
            announcements = await self.qq.fetch_announcements(group_id)
            announcements = [
                item.model_copy(update={"bot_id": self.bot.id}) for item in announcements
            ]
        except Exception as exc:
            logger.exception("Announcement fetch failed for group %s", group_id)
            fetch_error = str(exc)
        new_count = sum(self.database.upsert_announcement(item) for item in announcements)
        deleted_count = 0
        if not fetch_error:
            deleted_count = self.database.reconcile_announcements(
                group_id, {item.announcement_id for item in announcements}
            )
        stats: dict[str, object] = await self.retry_pending(group_id)
        stats.update(
            {"fetched": len(announcements), "new": new_count, "marked_deleted": deleted_count}
        )
        if fetch_error:
            stats["fetch_error"] = fetch_error
        self.database.audit(
            "announcement_fetch", "failed" if fetch_error else "completed", "group", group_id, stats
        )
        return stats

    async def retry_pending(self, group_id: str | None = None) -> dict[str, int]:
        feishu_config, feishu = self._feishu_target(group_id)
        if feishu_config is None or feishu is None or not feishu_config.enabled:
            return {"synced": 0, "failed": 0}
        synced = 0
        failed = 0
        for row_id, announcement in self.database.claim_pending_announcements(group_id=group_id):
            try:
                await feishu.archive_announcement(announcement)
                self.database.mark_announcement_sync(row_id, True)
                synced += 1
            except Exception as exc:
                logger.exception("Announcement sync failed")
                self.database.mark_announcement_sync(row_id, False, str(exc))
                failed += 1
        return {"synced": synced, "failed": failed}

    def _feishu_target(
        self, group_id: str | None
    ) -> tuple[FeishuBotConfig | None, FeishuGateway | None]:
        target_id = ""
        if group_id is not None:
            assignment = self.settings.qq_group_assignment(self.bot.id, group_id)
            if assignment is not None:
                target_id = assignment.tasks.announcement_sync.feishu_bot_id
        target = self.settings.feishu_bot(target_id) if target_id else self.default_feishu_config
        if target is None:
            return None, None
        return target, self.feishu_clients.get(target.id)


class SearchService:
    def __init__(
        self,
        settings: Settings,
        feishu: FeishuGateway,
        qq: QQGateway,
        bot: QQBotConfig | None = None,
        feishu_config: FeishuBotConfig | None = None,
    ) -> None:
        self.settings = settings
        self.feishu = feishu
        self.qq = qq
        self.bot = bot or settings.qq_bot()
        self.feishu_config = feishu_config or settings.feishu_bot()
        if self.bot is None:
            raise ValueError("No QQ Bot is configured")

    def extract_query(self, text: str) -> str | None:
        normalized = text.strip()
        prefixes = self.feishu_config.search_prefixes if self.feishu_config else []
        for prefix in prefixes:
            if normalized.startswith(prefix):
                return normalized[len(prefix) :].strip()
        return None

    async def handle_admin_message(self, user_id: str, text: str) -> str:
        if user_id not in self.bot.administrator_qq_ids:
            return "unauthorized"
        query = self.extract_query(text)
        if query is None:
            return "not_a_search"
        if not query:
            await self.qq.send_private_message(user_id, "用法：搜索 <关键词>")
            return "empty_query"
        if self.feishu_config is None or not self.feishu_config.enabled:
            await self.qq.send_private_message(user_id, "飞书检索尚未启用。")
            return "feishu_disabled"
        try:
            hits = await self.feishu.search(query, self.feishu_config.max_search_results)
        except Exception as exc:
            logger.exception("Feishu search failed")
            await self.qq.send_private_message(user_id, f"飞书检索失败：{type(exc).__name__}")
            return "failed"
        if not hits:
            reply = f"飞书中没有找到与“{query}”相关的内容。"
        else:
            lines = [f"飞书搜索“{query}”结果："]
            for index, hit in enumerate(hits, start=1):
                lines.append(f"{index}. {hit.title}\n{hit.snippet[:500]}")
                if hit.url:
                    lines.append(hit.url)
            reply = "\n".join(lines)
        await self.qq.send_private_message(user_id, reply[:4000])
        return "replied"
