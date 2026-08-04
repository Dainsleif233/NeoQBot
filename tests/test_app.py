import hashlib
import hmac
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from mua_bot.app import create_app
from mua_bot.config import Settings
from mua_bot.models import GroupMessage


def app_settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "app": {"database_path": str(tmp_path / "app.db"), "dry_run": True},
            "qq": {
                "managed_group_ids": ["g1"],
                "administrator_qq_ids": ["a1"],
                "webhook_secret": "secret",
            },
            "moderation": {"enabled": False},
            "announcements": {"enabled": False},
            "retention": {"enabled": False},
        }
    )


def test_health_and_signed_webhook(tmp_path: Path) -> None:
    body = json.dumps({"post_type": "meta_event"}).encode()
    signature = "sha1=" + hmac.new(b"secret", body, hashlib.sha1).hexdigest()

    with TestClient(create_app(app_settings(tmp_path))) as client:
        assert client.get("/healthz").status_code == 200
        gui = client.get("/gui/")
        assert "MUA-Bot Console" in gui.text
        assert "/gui/assets/app.js?v=0.3.0" in gui.text
        assert gui.headers["cache-control"] == "no-store, max-age=0, must-revalidate"
        accepted = client.post(
            "/webhooks/onebot",
            content=body,
            headers={"Content-Type": "application/json", "X-Signature": signature},
        )
        rejected = client.post(
            "/webhooks/onebot",
            content=body,
            headers={"Content-Type": "application/json", "X-Signature": "sha1=bad"},
        )

    assert accepted.status_code == 202
    assert rejected.status_code == 401


def test_bot_specific_webhook_uses_its_own_secret(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    values = settings.model_dump()
    values["qq"]["bots"] = [
        {
            "id": "observer",
            "name": "Observer",
            "webhook_secret": "observer-secret",
            "managed_group_ids": ["g1"],
            "administrator_qq_ids": ["a1"],
        },
        {
            "id": "worker",
            "name": "Worker",
            "webhook_secret": "worker-secret",
            "managed_group_ids": ["g1"],
            "administrator_qq_ids": ["a1"],
        },
    ]
    settings = Settings.model_validate(values)
    body = json.dumps({"post_type": "meta_event"}).encode()
    worker_signature = "sha1=" + hmac.new(b"worker-secret", body, hashlib.sha1).hexdigest()

    with TestClient(create_app(settings)) as client:
        accepted = client.post(
            "/webhooks/onebot/worker",
            content=body,
            headers={"Content-Type": "application/json", "X-Signature": worker_signature},
        )
        rejected = client.post(
            "/webhooks/onebot/observer",
            content=body,
            headers={"Content-Type": "application/json", "X-Signature": worker_signature},
        )

    assert accepted.status_code == 202
    assert accepted.json()["bot_id"] == "worker"
    assert rejected.status_code == 401


def test_onebot_webhook_accepts_napcat_bearer_token(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    values = settings.model_dump()
    values["qq"]["webhook_secret"] = ""
    values["qq"]["access_token"] = "onebot-token"
    values["qq"]["access_token_file"] = ""
    body = json.dumps({"post_type": "meta_event"}).encode()

    with TestClient(create_app(Settings.model_validate(values))) as client:
        accepted = client.post(
            "/webhooks/onebot",
            content=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer onebot-token"},
        )
        rejected = client.post(
            "/webhooks/onebot",
            content=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer bad"},
        )

    assert accepted.status_code == 202
    assert rejected.status_code == 401


def test_gui_forces_default_password_change(tmp_path: Path) -> None:
    with TestClient(create_app(app_settings(tmp_path))) as client:
        login = client.post(
            "/api/gui/auth/login",
            json={"username": "admin", "password": "muaadmin"},
        )
        assert login.status_code == 200
        session = login.json()
        assert session["must_change_password"] is True

        blocked = client.get("/api/gui/dashboard")
        assert blocked.status_code == 403

        changed = client.post(
            "/api/gui/auth/password",
            headers={"X-CSRF-Token": session["csrf_token"]},
            json={
                "current_password": "muaadmin",
                "new_password": "a-new-secure-password",
            },
        )
        assert changed.status_code == 200
        assert client.get("/api/gui/dashboard").status_code == 200


def test_gui_saves_and_hot_reloads_settings(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setenv("MUA_CONFIG", str(config_path))
    with TestClient(create_app(app_settings(tmp_path))) as client:
        login = client.post(
            "/api/gui/auth/login",
            json={"username": "admin", "password": "muaadmin"},
        ).json()
        client.post(
            "/api/gui/auth/password",
            headers={"X-CSRF-Token": login["csrf_token"]},
            json={
                "current_password": "muaadmin",
                "new_password": "a-new-secure-password",
            },
        )
        config = client.get("/api/gui/settings").json()["config"]
        config["app"]["environment"] = "gui-test"

        saved = client.put(
            "/api/gui/settings",
            headers={"X-CSRF-Token": login["csrf_token"]},
            json={"config": config},
        )

        assert saved.status_code == 200
        assert client.get("/api/gui/dashboard").json()["environment"] == "gui-test"
        assert config_path.exists()


def test_gui_group_orchestration_returns_group_activity(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    values = settings.model_dump()
    values["orchestration"] = {
        "resources": [
            {
                "id": "community",
                "kind": "qq_group",
                "name": "社区群",
                "external_id": "g1",
            }
        ],
        "edges": [
            {
                "id": "default-community",
                "source": "qq-bot:default",
                "target": "community",
                "relation": "manages",
            }
        ],
    }
    with TestClient(create_app(Settings.model_validate(values))) as client:
        login = client.post(
            "/api/gui/auth/login", json={"username": "admin", "password": "muaadmin"}
        ).json()
        client.post(
            "/api/gui/auth/password",
            headers={"X-CSRF-Token": login["csrf_token"]},
            json={
                "current_password": "muaadmin",
                "new_password": "a-new-secure-password",
            },
        )
        response = client.get(
            "/api/gui/orchestration/group",
            params={"group_id": "g1", "resource_id": "community"},
        )
        dashboard = client.get("/api/gui/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["resource"]["name"] == "社区群"
    assert payload["managers"][0]["id"] == "default"
    assert payload["counts"]["messages"] == 0
    assert dashboard.json()["orchestration"]["groups"] == 1
    assert dashboard.json()["orchestration"]["edges"] == 1


def test_gui_rejects_stale_settings_revision(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MUA_CONFIG", str(tmp_path / "config.yaml"))
    with TestClient(create_app(app_settings(tmp_path))) as client:
        login = client.post(
            "/api/gui/auth/login", json={"username": "admin", "password": "muaadmin"}
        ).json()
        client.post(
            "/api/gui/auth/password",
            headers={"X-CSRF-Token": login["csrf_token"]},
            json={
                "current_password": "muaadmin",
                "new_password": "a-new-secure-password",
            },
        )
        initial = client.get("/api/gui/settings").json()
        first_config = initial["config"]
        first_config["app"]["environment"] = "first-writer"
        saved = client.put(
            "/api/gui/settings",
            headers={"X-CSRF-Token": login["csrf_token"]},
            json={"config": first_config, "revision": initial["revision"]},
        )
        stale_config = initial["config"]
        stale_config["app"]["environment"] = "stale-writer"
        conflict = client.put(
            "/api/gui/settings",
            headers={"X-CSRF-Token": login["csrf_token"]},
            json={"config": stale_config, "revision": initial["revision"]},
        )

    assert saved.status_code == 200
    assert saved.json()["revision"] != initial["revision"]
    assert conflict.status_code == 409
    assert "其他会话" in conflict.json()["detail"]


def test_gui_records_supports_search_and_pagination(tmp_path: Path) -> None:
    app = create_app(app_settings(tmp_path))
    with TestClient(app) as client:
        login = client.post(
            "/api/gui/auth/login", json={"username": "admin", "password": "muaadmin"}
        ).json()
        client.post(
            "/api/gui/auth/password",
            headers={"X-CSRF-Token": login["csrf_token"]},
            json={
                "current_password": "muaadmin",
                "new_password": "a-new-secure-password",
            },
        )
        for index, text in enumerate(("needle first", "needle second")):
            app.state.container.database.save_message(
                GroupMessage(
                    message_id=str(index),
                    group_id="g1",
                    user_id="u1",
                    text=text,
                    sent_at=datetime(2026, 7, 21, 4, 0, index, tzinfo=UTC),
                )
            )
        first_page = client.get(
            "/api/gui/records/messages",
            params={"limit": 1, "offset": 0, "group_id": "g1", "search": "needle"},
        )
        second_page = client.get(
            "/api/gui/records/messages",
            params={"limit": 1, "offset": 1, "group_id": "g1", "search": "needle"},
        )

    assert first_page.status_code == 200
    assert first_page.json()["has_more"] is True
    assert first_page.json()["records"][0]["text"] == "needle second"
    assert second_page.json()["has_more"] is False
    assert second_page.json()["records"][0]["text"] == "needle first"


def test_gui_serves_napcat_qrcode_only_to_authenticated_admin(tmp_path: Path) -> None:
    qrcode = tmp_path / "qrcode.png"
    qrcode.write_bytes(b"\x89PNG\r\n\x1a\npreview")
    settings = app_settings(tmp_path)
    values = settings.model_dump()
    values["qq"]["qrcode_path"] = str(qrcode)

    with TestClient(create_app(Settings.model_validate(values))) as client:
        endpoint = "/api/gui/integrations/qq/qrcode"
        assert client.get(endpoint).status_code == 401
        login = client.post(
            "/api/gui/auth/login",
            json={"username": "admin", "password": "muaadmin"},
        ).json()
        client.post(
            "/api/gui/auth/password",
            headers={"X-CSRF-Token": login["csrf_token"]},
            json={
                "current_password": "muaadmin",
                "new_password": "a-new-secure-password",
            },
        )

        response = client.get(endpoint)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store, max-age=0, must-revalidate"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_gui_refuses_to_serve_expired_napcat_qrcode(tmp_path: Path) -> None:
    qrcode = tmp_path / "qrcode.png"
    qrcode.write_bytes(b"\x89PNG\r\n\x1a\nexpired")
    old = time.time() - 120
    os.utime(qrcode, (old, old))
    values = app_settings(tmp_path).model_dump()
    values["qq"]["qrcode_path"] = str(qrcode)

    with TestClient(create_app(Settings.model_validate(values))) as client:
        login = client.post(
            "/api/gui/auth/login", json={"username": "admin", "password": "muaadmin"}
        ).json()
        client.post(
            "/api/gui/auth/password",
            headers={"X-CSRF-Token": login["csrf_token"]},
            json={
                "current_password": "muaadmin",
                "new_password": "a-new-secure-password",
            },
        )
        response = client.get("/api/gui/integrations/qq/qrcode")

    assert response.status_code == 410
    assert "二维码已过期" in response.json()["detail"]


def test_gui_refreshes_qrcode_and_reveals_webui_token_to_admin(tmp_path: Path) -> None:
    qrcode = tmp_path / "qrcode.png"
    qrcode.write_bytes(b"\x89PNG\r\n\x1a\nold")
    values = app_settings(tmp_path).model_dump()
    values["qq"]["qrcode_path"] = str(qrcode)
    app = create_app(Settings.model_validate(values))
    napcat = app.state.container.napcat_clients["default"]

    async def refresh() -> dict[str, bool]:
        qrcode.write_bytes(b"\x89PNG\r\n\x1a\nnew")
        return {"refreshed": True}

    napcat.refresh_qrcode = refresh
    napcat.token = lambda: "napcat-webui-token"

    with TestClient(app) as client:
        login = client.post(
            "/api/gui/auth/login", json={"username": "admin", "password": "muaadmin"}
        ).json()
        client.post(
            "/api/gui/auth/password",
            headers={"X-CSRF-Token": login["csrf_token"]},
            json={
                "current_password": "muaadmin",
                "new_password": "a-new-secure-password",
            },
        )
        refreshed = client.post(
            "/api/gui/integrations/qq/qrcode/refresh",
            headers={"X-CSRF-Token": login["csrf_token"]},
        )
        token = client.post(
            "/api/gui/integrations/qq/webui-token",
            headers={"X-CSRF-Token": login["csrf_token"]},
        )

    assert refreshed.status_code == 200
    assert refreshed.json()["changed"] is True
    assert token.json()["token"] == "napcat-webui-token"
