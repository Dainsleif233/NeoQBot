from pathlib import Path

from mua_bot.auth import GuiAuth
from mua_bot.config import GuiConfig
from mua_bot.database import Database


def test_bootstrap_login_and_password_change(tmp_path: Path) -> None:
    database = Database(tmp_path / "auth.db")
    database.initialize()
    auth = GuiAuth(database, GuiConfig())

    assert auth.ensure_bootstrap_admin() is True
    assert auth.ensure_bootstrap_admin() is False
    login = auth.login("admin", "muaadmin")
    assert login is not None
    token, session = login
    assert session.must_change_password is True
    assert auth.change_password(session, "muaadmin", "a-new-secure-password") is True
    refreshed = auth.session(token)
    assert refreshed is not None
    assert refreshed.must_change_password is False
    assert auth.login("admin", "muaadmin") is None
    assert auth.login("admin", "a-new-secure-password") is not None
