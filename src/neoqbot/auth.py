from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import GuiConfig, resolve_secret
from .database import Database

PBKDF2_ITERATIONS = 600_000
_DUMMY_SALT = b"NeoQBotAuthDummy"


def _hash_password(password: str, salt: bytes, iterations: int) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return base64.b64encode(digest).decode("ascii")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class GuiSession:
    username: str
    csrf_token: str
    must_change_password: bool
    token_hash: str


class GuiAuth:
    def __init__(self, database: Database, config: GuiConfig):
        self.database = database
        self.config = config

    def ensure_bootstrap_admin(self) -> bool:
        existing = self.database.get_admin_user(self.config.bootstrap_username)
        if existing and not bool(existing["must_change_password"]):
            return False
        password = resolve_secret(
            self.config.bootstrap_password, self.config.bootstrap_password_file
        )
        if len(password) < 16:
            raise RuntimeError(
                "GUI bootstrap password is missing or shorter than 16 characters; "
                "configure gui.bootstrap_password_file or NEOQBOT_GUI__BOOTSTRAP_PASSWORD"
            )
        salt = secrets.token_bytes(16)
        password_hash = _hash_password(password, salt, PBKDF2_ITERATIONS)
        encoded_salt = base64.b64encode(salt).decode("ascii")
        if existing:
            return self.database.reset_bootstrap_admin_password(
                self.config.bootstrap_username,
                password_hash,
                encoded_salt,
                PBKDF2_ITERATIONS,
            )
        return self.database.ensure_admin_user(
            self.config.bootstrap_username, password_hash, encoded_salt, PBKDF2_ITERATIONS
        )

    def login(self, username: str, password: str) -> tuple[str, GuiSession] | None:
        user = self.database.get_admin_user(username)
        if not user:
            _hash_password(password, _DUMMY_SALT, PBKDF2_ITERATIONS)
            return None
        try:
            salt = base64.b64decode(user["password_salt"])
            expected = _hash_password(password, salt, int(user["password_iterations"]))
        except (ValueError, TypeError):
            return None
        if not secrets.compare_digest(expected, str(user["password_hash"])):
            return None
        token = secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(32)
        token_digest = _token_hash(token)
        self.database.create_gui_session(
            token_digest,
            username,
            csrf,
            datetime.now(UTC) + timedelta(hours=self.config.session_hours),
            self.config.max_sessions_per_user,
        )
        return token, GuiSession(
            username=username,
            csrf_token=csrf,
            must_change_password=bool(user["must_change_password"]),
            token_hash=token_digest,
        )

    def session(self, token: str | None) -> GuiSession | None:
        if not token:
            return None
        token_digest = _token_hash(token)
        row = self.database.get_gui_session(token_digest)
        if not row:
            return None
        return GuiSession(
            username=str(row["username"]),
            csrf_token=str(row["csrf_token"]),
            must_change_password=bool(row["must_change_password"]),
            token_hash=token_digest,
        )

    def logout(self, token: str | None) -> None:
        if token:
            self.database.delete_gui_session(_token_hash(token))

    def change_password(
        self, session: GuiSession, current_password: str, new_password: str
    ) -> bool:
        if len(new_password) < 14:
            raise ValueError("新密码至少需要 14 个字符")
        if session.username.casefold() in new_password.casefold():
            raise ValueError("新密码不能包含管理员用户名")
        if new_password.casefold() in {
            "password123456",
            "administrator123",
            "admin123456789",
        }:
            raise ValueError("新密码过于常见，请使用随机生成的长密码")
        user = self.database.get_admin_user(session.username)
        if not user:
            return False
        salt = base64.b64decode(user["password_salt"])
        current_hash = _hash_password(current_password, salt, int(user["password_iterations"]))
        if not secrets.compare_digest(current_hash, str(user["password_hash"])):
            return False
        if secrets.compare_digest(current_password, new_password):
            raise ValueError("新密码不能与当前密码相同")
        new_salt = secrets.token_bytes(16)
        return self.database.update_admin_password(
            session.username,
            _hash_password(new_password, new_salt, PBKDF2_ITERATIONS),
            base64.b64encode(new_salt).decode("ascii"),
            PBKDF2_ITERATIONS,
            session.token_hash,
        )
