from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def resolve_secret(value: str, file_path: str = "") -> str:
    """Prefer a mounted secret file while retaining direct-value compatibility."""
    if file_path:
        try:
            mounted = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError:
            mounted = ""
        if mounted:
            return mounted
    return value.strip()


class AppConfig(BaseModel):
    environment: str = "development"
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"
    database_path: str = "data/mua-bot.db"
    message_archive_path: str = "data/group-message-records"
    admin_api_token: str = ""
    dry_run: bool = True


class QQConnectionConfig(BaseModel):
    enabled: bool = True
    onebot_base_url: str = "http://qq-bridge:3000"
    access_token: str = ""
    access_token_file: str = "/app/data/secrets/napcat-onebot.token"
    webhook_secret: str = ""
    request_timeout_seconds: float = 15.0
    webui_base_url: str = "http://qq-bridge:6099"
    webui_token: str = ""
    webui_token_file: str = "/app/data/secrets/napcat-webui.token"
    webui_public_url: str = ""
    webui_public_port: int = Field(default=6099, ge=1, le=65535)
    qrcode_path: str = "/app/napcat-cache/qrcode.png"
    managed_group_ids: list[str] = Field(default_factory=list)
    administrator_qq_ids: list[str] = Field(default_factory=list)
    announcement_actions: list[str] = Field(
        default_factory=lambda: ["get_group_notice", "_get_group_notice"]
    )

    @field_validator("managed_group_ids", "administrator_qq_ids", mode="before")
    @classmethod
    def ids_as_strings(cls, value: object) -> object:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return value


class QQJoinTaskConfig(BaseModel):
    enabled: bool = False
    detect_requests: bool = False
    execute_management: bool = False
    auto_approve: bool = False
    auto_reject: bool = False
    minimum_confidence: float = Field(default=0.88, ge=0, le=1)

    @model_validator(mode="after")
    def apply_dependencies(self) -> QQJoinTaskConfig:
        if self.execute_management:
            self.enabled = True
            self.detect_requests = True
        if self.detect_requests:
            self.enabled = True
        return self


class QQMessageTaskConfig(BaseModel):
    enabled: bool = False
    record_only: bool = False
    realtime_detection: bool = False
    polling_detection: bool = False
    analyze: bool = False
    handle: bool = False
    interval_minutes: int = Field(default=30, ge=1, le=1440)
    window_minutes: int = Field(default=5, ge=1, le=1440)
    risk_threshold: float = Field(default=0.7, ge=0, le=1)
    max_messages_per_run: int = Field(default=300, ge=1, le=5000)

    @model_validator(mode="after")
    def apply_dependencies(self) -> QQMessageTaskConfig:
        if self.handle:
            self.enabled = True
            self.polling_detection = True
            self.analyze = True
        if self.analyze:
            self.enabled = True
            self.polling_detection = True
        if self.record_only or self.realtime_detection or self.polling_detection:
            self.enabled = True
        return self


class QQAnnouncementTaskConfig(BaseModel):
    enabled: bool = False
    auto_sync: bool = False
    sync_interval_minutes: int = Field(default=30, ge=1, le=10080)
    sync_on_startup: bool = False
    feishu_bot_id: str = ""

    @model_validator(mode="after")
    def apply_dependencies(self) -> QQAnnouncementTaskConfig:
        if self.auto_sync or self.sync_on_startup:
            self.enabled = True
        return self


class QQTaskConfig(BaseModel):
    join_management: QQJoinTaskConfig = Field(default_factory=QQJoinTaskConfig)
    message_detection: QQMessageTaskConfig = Field(default_factory=QQMessageTaskConfig)
    announcement_sync: QQAnnouncementTaskConfig = Field(default_factory=QQAnnouncementTaskConfig)


class QQBotConfig(QQConnectionConfig):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(default="QQ Bot", min_length=1, max_length=80)
    tasks: QQTaskConfig = Field(default_factory=QQTaskConfig)
    search_feishu_bot_id: str = ""


class QQConfig(QQConnectionConfig):
    bots: list[QQBotConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_bot_ids(self) -> QQConfig:
        identifiers = [bot.id for bot in self.bots]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("qq.bots 中的 id 必须唯一")
        return self


class LLMConfig(BaseModel):
    driver: Literal["openai_compatible", "rules"] = "rules"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "replace-with-agnes-model"
    timeout_seconds: float = 60.0
    temperature: float = 0.0
    max_retries: int = 2
    json_response_format: bool = True


class JoinApprovalConfig(BaseModel):
    enabled: bool = True
    auto_approve: bool = False
    auto_reject: bool = False
    minimum_confidence: float = Field(default=0.88, ge=0, le=1)
    policy: str = "申请者应说明加入目的，并同意群规；信息不充分时转人工审核。"
    required_keywords: list[str] = Field(default_factory=list)
    forbidden_keywords: list[str] = Field(default_factory=list)


class ModerationConfig(BaseModel):
    enabled: bool = True
    interval_minutes: int = Field(default=30, ge=1, le=1440)
    window_minutes: int = Field(default=5, ge=1, le=1440)
    risk_threshold: float = Field(default=0.7, ge=0, le=1)
    max_messages_per_run: int = Field(default=300, ge=1, le=5000)
    policy: str = (
        "识别违法违规、涉政、色情、仇恨或群体对立、人身攻击、骚扰、诈骗、"
        "泄露隐私以及其他违反群规的内容。正常事实讨论不应仅因关键词命中而判违规。"
    )
    rule_keywords: dict[str, list[str]] = Field(default_factory=dict)


class AnnouncementConfig(BaseModel):
    enabled: bool = True
    sync_interval_minutes: int = Field(default=30, ge=1, le=10080)
    sync_on_startup: bool = True


class FeishuConnectionConfig(BaseModel):
    enabled: bool = False
    driver: Literal["cli", "disabled"] = "disabled"
    executable: str = "feishu"
    timeout_seconds: float = 60.0
    search_prefixes: list[str] = Field(default_factory=lambda: ["搜索 ", "查询 ", "/search "])
    max_search_results: int = Field(default=5, ge=1, le=20)
    # Each item is one argv token. No shell is involved. Supported actions:
    # archive_announcement, search, doctor.
    command_templates: dict[str, list[str]] = Field(default_factory=dict)
    archive_payload_stdin: bool = False
    extra_environment: dict[str, str] = Field(default_factory=dict)


class FeishuBotConfig(FeishuConnectionConfig):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(default="飞书 Bot", min_length=1, max_length=80)


class FeishuConfig(FeishuConnectionConfig):
    bots: list[FeishuBotConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_bot_ids(self) -> FeishuConfig:
        identifiers = [bot.id for bot in self.bots]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("feishu.bots 中的 id 必须唯一")
        return self


class RuntimeConfig(BaseModel):
    event_workers: int = Field(default=2, ge=1, le=32)
    shutdown_grace_seconds: float = Field(default=15.0, ge=0.1, le=300)


class RetentionConfig(BaseModel):
    enabled: bool = True
    message_days: int = Field(default=30, ge=1, le=3650)
    join_request_days: int = Field(default=180, ge=1, le=3650)
    moderation_run_days: int = Field(default=365, ge=1, le=3650)
    audit_days: int = Field(default=365, ge=1, le=3650)


class GuiConfig(BaseModel):
    enabled: bool = True
    bootstrap_username: str = Field(default="admin", min_length=1, max_length=64)
    bootstrap_password: str = Field(default="muaadmin", min_length=8, max_length=512)
    session_hours: int = Field(default=12, ge=1, le=720)
    secure_cookie: bool = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MUA_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    app: AppConfig = Field(default_factory=AppConfig)
    qq: QQConfig = Field(default_factory=QQConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    join_approval: JoinApprovalConfig = Field(default_factory=JoinApprovalConfig)
    moderation: ModerationConfig = Field(default_factory=ModerationConfig)
    announcements: AnnouncementConfig = Field(default_factory=AnnouncementConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    gui: GuiConfig = Field(default_factory=GuiConfig)

    def effective_qq_bots(self) -> list[QQBotConfig]:
        if self.qq.bots:
            return self.qq.bots
        return [
            QQBotConfig(
                id="default",
                name="默认 QQ Bot",
                enabled=self.qq.enabled,
                onebot_base_url=self.qq.onebot_base_url,
                access_token=self.qq.access_token,
                access_token_file=self.qq.access_token_file,
                webhook_secret=self.qq.webhook_secret,
                request_timeout_seconds=self.qq.request_timeout_seconds,
                webui_base_url=self.qq.webui_base_url,
                webui_token=self.qq.webui_token,
                webui_token_file=self.qq.webui_token_file,
                webui_public_url=self.qq.webui_public_url,
                webui_public_port=self.qq.webui_public_port,
                qrcode_path=self.qq.qrcode_path,
                managed_group_ids=self.qq.managed_group_ids,
                administrator_qq_ids=self.qq.administrator_qq_ids,
                announcement_actions=self.qq.announcement_actions,
                tasks=QQTaskConfig(
                    join_management=QQJoinTaskConfig(
                        enabled=self.join_approval.enabled,
                        detect_requests=self.join_approval.enabled,
                        execute_management=self.join_approval.enabled,
                        auto_approve=self.join_approval.auto_approve,
                        auto_reject=self.join_approval.auto_reject,
                        minimum_confidence=self.join_approval.minimum_confidence,
                    ),
                    message_detection=QQMessageTaskConfig(
                        enabled=self.moderation.enabled,
                        realtime_detection=self.moderation.enabled,
                        polling_detection=self.moderation.enabled,
                        analyze=self.moderation.enabled,
                        handle=self.moderation.enabled,
                        interval_minutes=self.moderation.interval_minutes,
                        window_minutes=self.moderation.window_minutes,
                        risk_threshold=self.moderation.risk_threshold,
                        max_messages_per_run=self.moderation.max_messages_per_run,
                    ),
                    announcement_sync=QQAnnouncementTaskConfig(
                        enabled=self.announcements.enabled,
                        auto_sync=self.announcements.enabled,
                        sync_interval_minutes=self.announcements.sync_interval_minutes,
                        sync_on_startup=self.announcements.sync_on_startup,
                    ),
                ),
            )
        ]

    def effective_feishu_bots(self) -> list[FeishuBotConfig]:
        if self.feishu.bots:
            return self.feishu.bots
        return [
            FeishuBotConfig(
                id="default",
                name="默认飞书 Bot",
                enabled=self.feishu.enabled,
                driver=self.feishu.driver,
                executable=self.feishu.executable,
                timeout_seconds=self.feishu.timeout_seconds,
                search_prefixes=self.feishu.search_prefixes,
                max_search_results=self.feishu.max_search_results,
                command_templates=self.feishu.command_templates,
                archive_payload_stdin=self.feishu.archive_payload_stdin,
                extra_environment=self.feishu.extra_environment,
            )
        ]

    def qq_bot(self, bot_id: str | None = None) -> QQBotConfig | None:
        bots = self.effective_qq_bots()
        if bot_id is None:
            return bots[0] if bots else None
        return next((bot for bot in bots if bot.id == bot_id), None)

    def feishu_bot(self, bot_id: str | None = None) -> FeishuBotConfig | None:
        bots = self.effective_feishu_bots()
        if bot_id is None:
            return bots[0] if bots else None
        return next((bot for bot in bots if bot.id == bot_id), None)

    def managed_group_ids(self) -> list[str]:
        return sorted(
            {
                group_id
                for bot in self.effective_qq_bots()
                if bot.enabled
                for group_id in bot.managed_group_ids
            }
        )

    @classmethod
    def load(cls, path: str | Path | None = None) -> Settings:
        config_path = Path(path or "config.yaml")
        values: dict[str, object] = {}
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"Configuration root must be a mapping: {config_path}")
            values = loaded
        # Explicitly overlay nested environment variables on YAML. BaseSettings normally gives
        # constructor values priority, while deployments reasonably expect secrets in env to win.
        for name, raw_value in os.environ.items():
            if not name.upper().startswith("MUA_"):
                continue
            path_parts = name[4:].lower().split("__")
            if not path_parts or path_parts == ["config"]:
                continue
            cursor = values
            for part in path_parts[:-1]:
                child = cursor.get(part)
                if not isinstance(child, dict):
                    child = {}
                    cursor[part] = child
                cursor = child
            if raw_value.lstrip().startswith(("[", "{")):
                try:
                    parsed_value = json.loads(raw_value)
                except json.JSONDecodeError:
                    parsed_value = raw_value
            else:
                parsed_value = raw_value
            cursor[path_parts[-1]] = parsed_value
        return cls(**values)

    def redacted_dict(self) -> dict[str, object]:
        data = self.model_dump()
        data["qq"]["access_token"] = "***" if self.qq.access_token else ""
        data["qq"]["webui_token"] = "***" if self.qq.webui_token else ""
        data["qq"]["webhook_secret"] = "***" if self.qq.webhook_secret else ""
        for bot in data["qq"]["bots"]:
            bot["access_token"] = "***" if bot["access_token"] else ""
            bot["webui_token"] = "***" if bot["webui_token"] else ""
            bot["webhook_secret"] = "***" if bot["webhook_secret"] else ""
        data["llm"]["api_key"] = "***" if self.llm.api_key else ""
        data["app"]["admin_api_token"] = "***" if self.app.admin_api_token else ""
        data["feishu"]["extra_environment"] = {key: "***" for key in self.feishu.extra_environment}
        for bot in data["feishu"]["bots"]:
            bot["extra_environment"] = {key: "***" for key in bot["extra_environment"]}
        data["gui"]["bootstrap_password"] = "***"
        return json.loads(json.dumps(data, ensure_ascii=False, default=str))

    def diagnostics(self) -> dict[str, list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        qq_bots = [bot for bot in self.effective_qq_bots() if bot.enabled]
        if not qq_bots:
            warnings.append("没有启用任何 QQ Bot")
        for bot in qq_bots:
            prefix = "qq" if not self.qq.bots else f"qq.bots.{bot.id}"
            if not bot.managed_group_ids:
                errors.append(f"{prefix}.managed_group_ids 不能为空")
            if not bot.administrator_qq_ids:
                errors.append(f"{prefix}.administrator_qq_ids 不能为空")
            onebot_token = resolve_secret(bot.access_token, bot.access_token_file)
            if not onebot_token:
                warnings.append(f"{prefix}.access_token 未设置")
            if not bot.webhook_secret and not onebot_token:
                warnings.append(f"{prefix} 未设置 Webhook HMAC 或 Bearer Token，事件入口未鉴权")
        if self.llm.driver == "openai_compatible":
            if not self.llm.api_key:
                errors.append("llm.api_key 未设置")
            if not self.llm.model or self.llm.model.startswith("replace-with"):
                errors.append("llm.model 仍是占位值")
        feishu_bots = self.effective_feishu_bots()
        default_feishu = next((bot for bot in feishu_bots if bot.enabled), feishu_bots[0])
        archive_targets = {
            bot.tasks.announcement_sync.feishu_bot_id or default_feishu.id
            for bot in qq_bots
            if bot.tasks.announcement_sync.enabled
        }
        search_targets = {
            bot.search_feishu_bot_id
            or bot.tasks.announcement_sync.feishu_bot_id
            or default_feishu.id
            for bot in qq_bots
        }
        for bot in feishu_bots:
            if not bot.enabled:
                continue
            prefix = "feishu" if not self.feishu.bots else f"feishu.bots.{bot.id}"
            if bot.id in archive_targets and not bot.command_templates.get(
                "archive_announcement"
            ):
                errors.append(f"{prefix}.command_templates.archive_announcement 未配置")
            if bot.id in search_targets and not bot.command_templates.get("search"):
                errors.append(f"{prefix}.command_templates.search 未配置")
        feishu_ids = {bot.id for bot in feishu_bots}
        for bot in qq_bots:
            announcement_target = bot.tasks.announcement_sync.feishu_bot_id
            if announcement_target and announcement_target not in feishu_ids:
                errors.append(
                    f"qq.bots.{bot.id}.tasks.announcement_sync.feishu_bot_id 指向未知飞书 Bot"
                )
            if bot.search_feishu_bot_id and bot.search_feishu_bot_id not in feishu_ids:
                errors.append(f"qq.bots.{bot.id}.search_feishu_bot_id 指向未知飞书 Bot")
        if not self.app.admin_api_token:
            warnings.append("app.admin_api_token 未设置，管理 API 将禁用")
        if self.app.dry_run:
            warnings.append("app.dry_run=true，所有 QQ 出站动作均被抑制")
        if self.join_approval.auto_reject or any(
            bot.tasks.join_management.auto_reject for bot in qq_bots
        ):
            warnings.append("join_approval.auto_reject=true，建议保持人工拒绝")
        if self.gui.bootstrap_username == "admin" and self.gui.bootstrap_password == "muaadmin":
            warnings.append("GUI 仍配置默认初始凭据，首次登录后必须修改密码")
        if not self.gui.secure_cookie:
            warnings.append("gui.secure_cookie=false，仅适合 HTTP 联调或由可信反向代理提供 HTTPS")
        return {"errors": errors, "warnings": warnings}

    def save(self, path: str | Path) -> None:
        config_path = Path(path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump(mode="json")
        environment_secrets = {
            "MUA_APP__ADMIN_API_TOKEN": ("app", "admin_api_token"),
            "MUA_QQ__ACCESS_TOKEN": ("qq", "access_token"),
            "MUA_QQ__WEBUI_TOKEN": ("qq", "webui_token"),
            "MUA_QQ__WEBHOOK_SECRET": ("qq", "webhook_secret"),
            "MUA_LLM__API_KEY": ("llm", "api_key"),
            "MUA_GUI__BOOTSTRAP_PASSWORD": ("gui", "bootstrap_password"),
        }
        for environment_name, (section, key) in environment_secrets.items():
            if environment_name in os.environ:
                data[section][key] = ""
        payload = yaml.safe_dump(
            data,
            allow_unicode=True,
            sort_keys=False,
        )
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=config_path.parent,
                prefix=f".{config_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_name = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, config_path)
        finally:
            if temporary_name and Path(temporary_name).exists():
                Path(temporary_name).unlink()
