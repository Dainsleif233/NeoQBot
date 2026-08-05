from __future__ import annotations

import json
import os
import tempfile
from ipaddress import ip_network
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
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"
    database_path: str = "data/neoqbot.db"
    message_archive_path: str = "data/group-message-records"
    admin_api_token: str = ""
    admin_api_token_file: str = "data/secrets/admin-api.token"
    dry_run: bool = True
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "neoqbot", "testserver"]
    )
    management_allowed_networks: list[str] = Field(default_factory=list)
    require_https: bool = False
    forwarded_allow_ips: str = "127.0.0.1"
    max_request_body_bytes: int = Field(default=2 * 1024 * 1024, ge=16 * 1024, le=16 * 1024 * 1024)
    expose_api_docs: bool = False

    @field_validator("admin_api_token")
    @classmethod
    def strong_admin_token(cls, value: str) -> str:
        value = value.strip()
        if value and len(value) < 32:
            raise ValueError("app.admin_api_token 至少需要 32 个字符")
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def valid_allowed_hosts(cls, value: list[str]) -> list[str]:
        hosts = [host.strip() for host in value if host.strip()]
        if not hosts:
            raise ValueError("app.allowed_hosts 不能为空")
        if any("/" in host or "://" in host for host in hosts):
            raise ValueError("app.allowed_hosts 只填写主机名或 IP，不要填写 URL/路径")
        return list(dict.fromkeys(hosts))

    @field_validator("management_allowed_networks")
    @classmethod
    def valid_management_networks(cls, value: list[str]) -> list[str]:
        networks = [network.strip() for network in value if network.strip()]
        for network in networks:
            ip_network(network, strict=False)
        return list(dict.fromkeys(networks))


class QQConnectionConfig(BaseModel):
    enabled: bool = False
    onebot_base_url: str = "http://127.0.0.1:3000"
    access_token: str = ""
    access_token_file: str = "data/secrets/napcat-onebot.token"
    webhook_secret: str = ""
    request_timeout_seconds: float = 15.0
    webui_base_url: str = "http://127.0.0.1:6099"
    webui_token: str = ""
    webui_token_file: str = "data/secrets/napcat-webui.token"
    webui_public_url: str = ""
    webui_public_port: int = Field(default=6099, ge=1, le=65535)
    qrcode_path: str = "data/napcat-cache/qrcode.png"
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


class QQConfig(BaseModel):
    bots: list[QQBotConfig] = Field(
        default_factory=lambda: [QQBotConfig(id="default", name="默认 QQ Bot")],
        min_length=1,
    )

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
    enabled: bool = False
    auto_approve: bool = False
    auto_reject: bool = False
    minimum_confidence: float = Field(default=0.88, ge=0, le=1)
    policy: str = "申请者应说明加入目的，并同意群规；信息不充分时转人工审核。"
    required_keywords: list[str] = Field(default_factory=list)
    forbidden_keywords: list[str] = Field(default_factory=list)


class ModerationConfig(BaseModel):
    enabled: bool = False
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
    enabled: bool = False
    sync_interval_minutes: int = Field(default=30, ge=1, le=10080)
    sync_on_startup: bool = False


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


class FeishuConfig(BaseModel):
    bots: list[FeishuBotConfig] = Field(
        default_factory=lambda: [FeishuBotConfig(id="default", name="默认飞书 Bot", enabled=False)],
        min_length=1,
    )

    @model_validator(mode="after")
    def unique_bot_ids(self) -> FeishuConfig:
        identifiers = [bot.id for bot in self.bots]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("feishu.bots 中的 id 必须唯一")
        return self


class OrchestrationPosition(BaseModel):
    x: float = Field(default=80, ge=-10000, le=10000)
    y: float = Field(default=80, ge=-10000, le=10000)


class OrchestrationResourceConfig(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    kind: Literal["qq_group", "feishu_group", "knowledge_base"]
    name: str = Field(min_length=1, max_length=80)
    external_id: str = Field(default="", max_length=160)
    description: str = Field(default="", max_length=1000)
    enabled: bool = True
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("name", "external_id", "description", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class OrchestrationEdgeConfig(BaseModel):
    id: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    source: str = Field(min_length=1, max_length=96)
    target: str = Field(min_length=1, max_length=96)
    relation: Literal["manages", "observes", "archives_to", "searches", "syncs"] = "manages"
    enabled: bool = True


class OrchestrationConfig(BaseModel):
    resources: list[OrchestrationResourceConfig] = Field(default_factory=list)
    edges: list[OrchestrationEdgeConfig] = Field(default_factory=list)
    layout: dict[str, OrchestrationPosition] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_identifiers(self) -> OrchestrationConfig:
        resource_ids = [resource.id for resource in self.resources]
        if len(resource_ids) != len(set(resource_ids)):
            raise ValueError("orchestration.resources 中的 id 必须唯一")
        external_resources = [
            (resource.kind, resource.external_id)
            for resource in self.resources
            if resource.external_id
        ]
        if len(external_resources) != len(set(external_resources)):
            raise ValueError("同一类型的编排资源不能使用重复的平台标识")
        edge_ids = [edge.id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("orchestration.edges 中的 id 必须唯一")
        connections = [
            (edge.source, edge.target, edge.relation) for edge in self.edges if edge.enabled
        ]
        if len(connections) != len(set(connections)):
            raise ValueError("编排连接不能重复")
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
    bootstrap_password: str = Field(default="", max_length=512)
    bootstrap_password_file: str = "data/secrets/gui-bootstrap-password"
    session_hours: int = Field(default=8, ge=1, le=168)
    max_sessions_per_user: int = Field(default=5, ge=1, le=20)
    secure_cookie: bool = False
    allow_sensitive_settings_edits: bool = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NEOQBOT_",
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
    orchestration: OrchestrationConfig = Field(default_factory=OrchestrationConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    gui: GuiConfig = Field(default_factory=GuiConfig)

    @model_validator(mode="after")
    def validate_orchestration(self) -> Settings:
        resource_ids = {resource.id for resource in self.orchestration.resources}
        bot_node_ids = {f"qq-bot:{bot.id}" for bot in self.effective_qq_bots()} | {
            f"feishu-bot:{bot.id}" for bot in self.effective_feishu_bots()
        }
        valid_node_ids = resource_ids | bot_node_ids
        for edge in self.orchestration.edges:
            if edge.source == edge.target:
                raise ValueError(f"编排连接 {edge.id} 不能连接节点自身")
            if edge.source not in valid_node_ids or edge.target not in valid_node_ids:
                raise ValueError(f"编排连接 {edge.id} 指向未知节点")
        qq_groups = {
            resource.id: resource.external_id
            for resource in self.orchestration.resources
            if resource.kind == "qq_group" and resource.enabled and resource.external_id
        }
        for bot in self.qq.bots:
            source = f"qq-bot:{bot.id}"
            bot.managed_group_ids = sorted(
                {
                    qq_groups[edge.target]
                    for edge in self.orchestration.edges
                    if edge.enabled
                    and edge.source == source
                    and edge.target in qq_groups
                    and edge.relation in {"manages", "observes"}
                }
            )
        return self

    def effective_qq_bots(self) -> list[QQBotConfig]:
        return self.qq.bots

    def effective_feishu_bots(self) -> list[FeishuBotConfig]:
        return self.feishu.bots

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
        prefix = "NEOQBOT_"
        for name, raw_value in os.environ.items():
            if not name.upper().startswith(prefix):
                continue
            path_parts = name[len(prefix) :].lower().split("__")
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
        for bot in data["qq"]["bots"]:
            bot["access_token"] = "***" if bot["access_token"] else ""
            bot["webui_token"] = "***" if bot["webui_token"] else ""
            bot["webhook_secret"] = "***" if bot["webhook_secret"] else ""
        data["llm"]["api_key"] = "***" if self.llm.api_key else ""
        data["app"]["admin_api_token"] = "***" if self.app.admin_api_token else ""
        for bot in data["feishu"]["bots"]:
            bot["extra_environment"] = {key: "***" for key in bot["extra_environment"]}
        data["gui"]["bootstrap_password"] = "***"
        return json.loads(json.dumps(data, ensure_ascii=False, default=str))

    def deployment_security_errors(self) -> list[str]:
        """Return configuration errors that make serving the control plane unsafe."""
        errors: list[str] = []
        production = self.app.environment.lower() == "production"
        if production and not self.app.require_https:
            errors.append("生产环境必须设置 app.require_https=true 并使用可信 HTTPS 反向代理")
        if self.gui.enabled and self.app.require_https and not self.gui.secure_cookie:
            errors.append("app.require_https=true 时必须同时启用 gui.secure_cookie")
        if production and "*" in self.app.allowed_hosts:
            errors.append("生产环境禁止在 app.allowed_hosts 中使用通配符")
        if production and self.app.forwarded_allow_ips.strip() == "*":
            errors.append("生产环境禁止设置 app.forwarded_allow_ips='*'")
        if production and self.app.expose_api_docs:
            errors.append("生产环境禁止公开 OpenAPI/Swagger 文档")
        return errors

    def diagnostics(self) -> dict[str, list[str]]:
        errors = self.deployment_security_errors()
        warnings: list[str] = []
        qq_bots = [bot for bot in self.effective_qq_bots() if bot.enabled]
        if not qq_bots:
            warnings.append("没有启用任何 QQ Bot")
        for bot in qq_bots:
            prefix = f"qq.bots.{bot.id}"
            if not bot.managed_group_ids:
                errors.append(f"{prefix}.managed_group_ids 不能为空")
            if not bot.administrator_qq_ids:
                errors.append(f"{prefix}.administrator_qq_ids 不能为空")
            onebot_token = resolve_secret(bot.access_token, bot.access_token_file)
            if not onebot_token:
                errors.append(f"{prefix}.access_token 未设置，Webhook 与 OneBot API 将拒绝连接")
            if not bot.webhook_secret and not onebot_token:
                errors.append(f"{prefix} 未设置 Webhook HMAC 或 Bearer Token，事件入口已安全禁用")
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
            prefix = f"feishu.bots.{bot.id}"
            if bot.id in archive_targets and not bot.command_templates.get("archive_announcement"):
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
        admin_api_token = resolve_secret(self.app.admin_api_token, self.app.admin_api_token_file)
        if not admin_api_token:
            warnings.append("app.admin_api_token 未设置，管理 API 将禁用")
        elif len(admin_api_token) < 32:
            errors.append("管理 API Token 至少需要 32 个字符")
        if self.app.dry_run:
            warnings.append("app.dry_run=true，所有 QQ 出站动作均被抑制")
        if self.join_approval.auto_reject or any(
            bot.tasks.join_management.auto_reject for bot in qq_bots
        ):
            warnings.append("join_approval.auto_reject=true，建议保持人工拒绝")
        bootstrap_password = resolve_secret(
            self.gui.bootstrap_password, self.gui.bootstrap_password_file
        )
        if self.gui.enabled and len(bootstrap_password) < 16:
            errors.append("GUI 初始密码缺失或少于 16 个字符，服务将拒绝创建管理员")
        if not self.gui.secure_cookie:
            warnings.append("gui.secure_cookie=false，仅适合 HTTP 联调或由可信反向代理提供 HTTPS")
        if "*" in self.app.allowed_hosts:
            warnings.append("app.allowed_hosts 包含通配符，Host Header 防护已弱化")
        if self.app.forwarded_allow_ips.strip() == "*":
            warnings.append("app.forwarded_allow_ips='*' 会信任任意代理头，不建议使用")
        if self.app.expose_api_docs:
            warnings.append("OpenAPI 文档已公开，仅应在受限开发环境使用")
        if not self.app.management_allowed_networks:
            warnings.append("管理端未配置 IP/CIDR 白名单，请依赖本机绑定、VPN 或反向代理访问控制")
        connected_resources = {
            endpoint
            for edge in self.orchestration.edges
            if edge.enabled
            for endpoint in (edge.source, edge.target)
        }
        for resource in self.orchestration.resources:
            if resource.enabled and resource.id not in connected_resources:
                warnings.append(f"编排资源 {resource.name} 尚未连接到任何 Bot 或资源")
        return {"errors": errors, "warnings": warnings}

    def save(self, path: str | Path) -> None:
        config_path = Path(path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump(mode="json")
        environment_secrets = {
            "NEOQBOT_APP__ADMIN_API_TOKEN": ("app", "admin_api_token"),
            "NEOQBOT_LLM__API_KEY": ("llm", "api_key"),
            "NEOQBOT_GUI__BOOTSTRAP_PASSWORD": ("gui", "bootstrap_password"),
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
