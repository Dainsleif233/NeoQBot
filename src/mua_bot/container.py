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
from .recording import LocalMessageRecorder
from .runtime import Runtime
from .services import AnnouncementService, JoinApprovalService, ModerationService, SearchService


@dataclass
class Container:
    settings: Settings
    database: Database
    qq: OneBotClient
    qq_clients: dict[str, OneBotClient]
    engine: DecisionEngine
    feishu: FeishuGateway
    feishu_clients: dict[str, FeishuGateway]
    runtime: Runtime
    auth: GuiAuth
    message_recorder: LocalMessageRecorder

    async def close(self) -> None:
        await self.runtime.stop()
        for client in self.qq_clients.values():
            await client.close()
        close = getattr(self.engine, "close", None)
        if close is not None:
            await close()


def build_container(settings: Settings) -> Container:
    database = Database(settings.app.database_path)
    database.initialize()
    message_recorder = LocalMessageRecorder(settings.app.message_archive_path)
    auth = GuiAuth(database, settings.gui)
    auth.ensure_bootstrap_admin()
    qq_bots = settings.effective_qq_bots()
    qq_clients = {
        bot.id: OneBotClient(bot, dry_run=settings.app.dry_run) for bot in qq_bots
    }
    qq = qq_clients[qq_bots[0].id]
    if settings.llm.driver == "openai_compatible":
        engine: DecisionEngine = OpenAICompatibleDecisionEngine(
            settings.llm, settings.join_approval, settings.moderation
        )
    else:
        engine = RuleBasedDecisionEngine(settings.join_approval, settings.moderation)
    feishu_bots = settings.effective_feishu_bots()
    feishu_clients: dict[str, FeishuGateway] = {}
    for bot in feishu_bots:
        if bot.enabled and bot.driver == "cli":
            feishu_clients[bot.id] = FeishuCliGateway(bot)
        else:
            feishu_clients[bot.id] = DisabledFeishuGateway()
    feishu = feishu_clients[feishu_bots[0].id]
    first_enabled_feishu = next((bot for bot in feishu_bots if bot.enabled), feishu_bots[0])

    join_services: dict[str, JoinApprovalService] = {}
    moderation_services: dict[str, ModerationService] = {}
    announcement_services: dict[str, AnnouncementService] = {}
    search_services: dict[str, SearchService] = {}
    for bot in qq_bots:
        client = qq_clients[bot.id]
        target_feishu_id = bot.tasks.announcement_sync.feishu_bot_id or first_enabled_feishu.id
        target_feishu = settings.feishu_bot(target_feishu_id) or first_enabled_feishu
        target_gateway = feishu_clients.get(target_feishu.id, feishu)
        search_feishu_id = bot.search_feishu_bot_id or target_feishu.id
        search_feishu = settings.feishu_bot(search_feishu_id) or target_feishu
        search_gateway = feishu_clients.get(search_feishu.id, target_gateway)
        join_services[bot.id] = JoinApprovalService(settings, database, engine, client, bot)
        moderation_services[bot.id] = ModerationService(
            settings, database, engine, client, bot, message_recorder
        )
        announcement_services[bot.id] = AnnouncementService(
            settings, database, client, target_gateway, bot, target_feishu
        )
        search_services[bot.id] = SearchService(
            settings, search_gateway, client, bot, search_feishu
        )
    event_handler = EventHandler(join_services, moderation_services, search_services)
    runtime = Runtime(
        settings,
        database,
        event_handler,
        moderation_services,
        announcement_services,
        message_recorder,
    )
    return Container(
        settings,
        database,
        qq,
        qq_clients,
        engine,
        feishu,
        feishu_clients,
        runtime,
        auth,
        message_recorder,
    )
