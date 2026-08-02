import hashlib
import json
from pathlib import Path

import httpx

from mua_bot.config import QQConnectionConfig, resolve_secret
from mua_bot.napcat import NapCatWebUiClient, initialize_napcat


def test_resolve_secret_prefers_mounted_file(tmp_path: Path) -> None:
    secret = tmp_path / "token"
    secret.write_text("mounted-token\n", encoding="utf-8")

    assert resolve_secret("inline-token", str(secret)) == "mounted-token"
    assert resolve_secret("inline-token", str(tmp_path / "missing")) == "inline-token"


def test_initialize_napcat_writes_webui_and_onebot_config(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    secret_dir = tmp_path / "secrets"
    config_dir.mkdir()
    (config_dir / "onebot11_123456.json").write_text(
        json.dumps({"network": {"httpServers": []}}), encoding="utf-8"
    )

    result = initialize_napcat(config_dir, secret_dir)
    onebot_token = (secret_dir / "napcat-onebot.token").read_text(encoding="utf-8").strip()
    webui_token = (secret_dir / "napcat-webui.token").read_text(encoding="utf-8").strip()
    webui = json.loads((config_dir / "webui.json").read_text(encoding="utf-8"))
    onebot = json.loads((config_dir / "onebot11.json").read_text(encoding="utf-8"))

    assert result["onebot_config"].endswith("onebot11.json")
    assert webui["token"] == webui_token
    assert webui["host"] == "0.0.0.0"
    server = onebot["network"]["httpServers"][0]
    client = onebot["network"]["httpClients"][0]
    assert server["port"] == 3000
    assert server["token"] == onebot_token
    assert client["url"] == "http://mua-bot:8080/webhooks/onebot/default"
    assert client["token"] == onebot_token
    account_config = json.loads(
        (config_dir / "onebot11_123456.json").read_text(encoding="utf-8")
    )
    assert account_config["network"]["httpServers"][0]["token"] == onebot_token

    initialize_napcat(config_dir, secret_dir)
    assert (secret_dir / "napcat-onebot.token").read_text(encoding="utf-8").strip() == onebot_token
    assert (secret_dir / "napcat-webui.token").read_text(encoding="utf-8").strip() == webui_token


async def test_napcat_client_hashes_token_and_reuses_credential() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"code": 0, "data": {"Credential": "session"}})
        assert request.headers["Authorization"] == "Bearer session"
        return httpx.Response(
            200,
            json={"code": 0, "data": {"isLogin": False, "isOffline": False}},
        )

    config = QQConnectionConfig(webui_token="webui-secret", webui_token_file="")
    client = NapCatWebUiClient(config, transport=httpx.MockTransport(handler))
    try:
        status = await client.check_login_status()
    finally:
        await client.close()

    login_body = json.loads(requests[0].content)
    expected = hashlib.sha256(b"webui-secret.napcat").hexdigest()
    assert login_body == {"hash": expected}
    assert status["isLogin"] is False
    assert len(requests) == 2
