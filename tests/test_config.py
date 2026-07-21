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
