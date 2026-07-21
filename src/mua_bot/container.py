from __future__ import annotations

from dataclasses import dataclass

from .adapters.feishu_cli import DisabledFeishuGateway, FeishuCliGateway
from .adapters.llm import OpenAICompatibleDecisionEngine, RuleBasedDecisionEngine
from .adapters.onebot import OneBotClient
from .auth import GuiAuth
from .config import Settings
from .database import Database
from .events import EventHandler
from .ports import DecisionEngine, FeishuGateway
from .runtime import Runtime
from .services import AnnouncementService, JoinApprovalService, ModerationService, SearchService


@dataclass
class Container:
    settings: Settings
    database: Database
    qq: OneBotClient
    engine: DecisionEngine
    feishu: FeishuGateway
    runtime: Runtime
    auth: GuiAuth

    async def close(self) -> None:
        await self.runtime.stop()
        await self.qq.close()
        close = getattr(self.engine, "close", None)
        if close is not None:
            await close()


def build_container(settings: Settings) -> Container:
    database = Database(settings.app.database_path)
    database.initialize()
    auth = GuiAuth(database, settings.gui)
    auth.ensure_bootstrap_admin()
    qq = OneBotClient(settings.qq, dry_run=settings.app.dry_run)
    if settings.llm.driver == "openai_compatible":
        engine: DecisionEngine = OpenAICompatibleDecisionEngine(
            settings.llm, settings.join_approval, settings.moderation
        )
    else:
        engine = RuleBasedDecisionEngine(settings.join_approval, settings.moderation)
    if settings.feishu.enabled and settings.feishu.driver == "cli":
        feishu: FeishuGateway = FeishuCliGateway(settings.feishu)
    else:
        feishu = DisabledFeishuGateway()

    join_service = JoinApprovalService(settings, database, engine, qq)
    moderation_service = ModerationService(settings, database, engine, qq)
    announcement_service = AnnouncementService(settings, database, qq, feishu)
    search_service = SearchService(settings, feishu, qq)
    event_handler = EventHandler(join_service, moderation_service, search_service)
    runtime = Runtime(settings, database, event_handler, moderation_service, announcement_service)
    return Container(settings, database, qq, engine, feishu, runtime, auth)
