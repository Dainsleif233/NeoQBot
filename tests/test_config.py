from pathlib import Path

from mua_bot.config import Settings


def test_environment_overrides_yaml(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "app:\n  dry_run: true\nqq:\n  managed_group_ids: ['1']\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MUA_APP__DRY_RUN", "false")
    monkeypatch.setenv("MUA_QQ__MANAGED_GROUP_IDS", '["2", "3"]')

    settings = Settings.load(config)

    assert settings.app.dry_run is False
    assert settings.qq.managed_group_ids == ["2", "3"]


def test_redacted_config_hides_secrets() -> None:
    settings = Settings.model_validate(
        {
            "app": {"admin_api_token": "admin"},
            "qq": {"access_token": "qq", "webhook_secret": "secret"},
            "llm": {"api_key": "llm"},
            "feishu": {"extra_environment": {"FEISHU_TOKEN": "token"}},
        }
    )

    redacted = settings.redacted_dict()

    assert redacted["app"]["admin_api_token"] == "***"
    assert redacted["qq"]["access_token"] == "***"
    assert redacted["llm"]["api_key"] == "***"
    assert redacted["feishu"]["extra_environment"]["FEISHU_TOKEN"] == "***"


def test_diagnostics_reports_missing_production_inputs() -> None:
    settings = Settings.model_validate(
        {
            "app": {"dry_run": False},
            "qq": {"managed_group_ids": [], "administrator_qq_ids": []},
            "llm": {"driver": "openai_compatible", "api_key": ""},
            "feishu": {"enabled": True, "driver": "cli"},
        }
    )

    diagnostics = settings.diagnostics()

    assert "qq.managed_group_ids 不能为空" in diagnostics["errors"]
    assert "llm.api_key 未设置" in diagnostics["errors"]
    assert any("archive_announcement" in item for item in diagnostics["errors"])


def test_save_does_not_persist_environment_secret(tmp_path: Path, monkeypatch) -> None:
    settings = Settings.model_validate({"llm": {"api_key": "secret-from-environment"}})
    monkeypatch.setenv("MUA_LLM__API_KEY", "secret-from-environment")
    path = tmp_path / "config.yaml"

    settings.save(path)

    saved = path.read_text(encoding="utf-8")
    assert "secret-from-environment" not in saved


def test_multi_bot_task_dependencies_are_applied() -> None:
    settings = Settings.model_validate(
        {
            "qq": {
                "bots": [
                    {
                        "id": "worker",
                        "name": "Worker",
                        "managed_group_ids": ["g1"],
                        "administrator_qq_ids": ["a1"],
                        "tasks": {
                            "join_management": {"execute_management": True},
                            "message_detection": {"handle": True},
                            "announcement_sync": {"auto_sync": True},
                        },
                    }
                ]
            }
        }
    )

    bot = settings.qq_bot("worker")
    assert bot is not None
    assert bot.tasks.join_management.enabled is True
    assert bot.tasks.join_management.detect_requests is True
    assert bot.tasks.message_detection.enabled is True
    assert bot.tasks.message_detection.polling_detection is True
    assert bot.tasks.message_detection.analyze is True
    assert bot.tasks.announcement_sync.enabled is True


def test_legacy_single_bot_config_remains_effective() -> None:
    settings = Settings.model_validate(
        {
            "qq": {"managed_group_ids": ["g1"], "administrator_qq_ids": ["a1"]},
            "moderation": {"enabled": False},
        }
    )

    bot = settings.qq_bot()
    assert bot is not None
    assert bot.id == "default"
    assert bot.managed_group_ids == ["g1"]
    assert bot.tasks.message_detection.enabled is False


def test_record_only_enables_message_task_without_analysis() -> None:
    settings = Settings.model_validate(
        {
            "qq": {
                "bots": [
                    {
                        "id": "recorder",
                        "managed_group_ids": ["123"],
                        "tasks": {"message_detection": {"record_only": True}},
                    }
                ]
            }
        }
    )

    task = settings.qq_bot("recorder").tasks.message_detection
    assert task.enabled is True
    assert task.record_only is True
    assert task.polling_detection is False
    assert task.analyze is False


def test_orchestration_accepts_many_to_many_group_links() -> None:
    settings = Settings.model_validate(
        {
            "qq": {
                "bots": [
                    {"id": "observer", "managed_group_ids": ["legacy"]},
                    {"id": "worker", "managed_group_ids": ["legacy"]},
                ]
            },
            "orchestration": {
                "resources": [
                    {
                        "id": "group-one",
                        "kind": "qq_group",
                        "name": "群一",
                        "external_id": "g1",
                    },
                    {
                        "id": "group-two",
                        "kind": "qq_group",
                        "name": "群二",
                        "external_id": "g2",
                    },
                    {"id": "handbook", "kind": "knowledge_base", "name": "管理手册"},
                ],
                "edges": [
                    {
                        "id": "observer-g1",
                        "source": "qq-bot:observer",
                        "target": "group-one",
                    },
                    {
                        "id": "worker-g1",
                        "source": "qq-bot:worker",
                        "target": "group-one",
                    },
                    {
                        "id": "worker-g2",
                        "source": "qq-bot:worker",
                        "target": "group-two",
                        "relation": "observes",
                    },
                    {
                        "id": "g1-handbook",
                        "source": "group-one",
                        "target": "handbook",
                        "relation": "archives_to",
                    },
                ],
            },
        }
    )

    assert len(settings.orchestration.resources) == 3
    assert len(settings.orchestration.edges) == 4
    assert settings.qq_bot("observer").managed_group_ids == ["g1"]
    assert settings.qq_bot("worker").managed_group_ids == ["g1", "g2"]


def test_orchestration_rejects_unknown_endpoint() -> None:
    try:
        Settings.model_validate(
            {
                "orchestration": {
                    "resources": [],
                    "edges": [{"id": "broken", "source": "qq-bot:default", "target": "missing"}],
                }
            }
        )
    except ValueError as exc:
        assert "未知节点" in str(exc)
    else:
        raise AssertionError("unknown orchestration endpoints must be rejected")


def test_orchestration_rejects_duplicate_platform_resource() -> None:
    try:
        Settings.model_validate(
            {
                "orchestration": {
                    "resources": [
                        {
                            "id": "group-one",
                            "kind": "qq_group",
                            "name": "群一",
                            "external_id": "123",
                        },
                        {
                            "id": "group-copy",
                            "kind": "qq_group",
                            "name": "群一副本",
                            "external_id": "123",
                        },
                    ]
                }
            }
        )
    except ValueError as exc:
        assert "重复的平台标识" in str(exc)
    else:
        raise AssertionError("duplicate orchestration resources must be rejected")
