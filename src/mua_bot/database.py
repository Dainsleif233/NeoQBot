from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Announcement, GroupMessage, JoinDecision, JoinRequest, utc_now

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS join_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL DEFAULT 'default',
    event_id TEXT NOT NULL,
    request_flag TEXT NOT NULL,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    comment TEXT NOT NULL,
    sub_type TEXT NOT NULL,
    received_at TEXT NOT NULL,
    decision TEXT,
    confidence REAL,
    reason TEXT,
    action_status TEXT NOT NULL DEFAULT 'received',
    updated_at TEXT NOT NULL,
    UNIQUE(bot_id, request_flag)
);

CREATE TABLE IF NOT EXISTS group_messages (
    bot_id TEXT NOT NULL DEFAULT 'default',
    message_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    text TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    raw_event_json TEXT NOT NULL,
    PRIMARY KEY (bot_id, group_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_group_time
ON group_messages(group_id, sent_at);

CREATE TABLE IF NOT EXISTS moderation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL DEFAULT 'default',
    group_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    max_risk REAL NOT NULL,
    result_json TEXT NOT NULL,
    alerted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(bot_id, group_id, window_start, window_end)
);

CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL DEFAULT 'default',
    announcement_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    author_id TEXT NOT NULL,
    published_at TEXT,
    source_payload_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    sync_status TEXT NOT NULL DEFAULT 'pending',
    sync_attempts INTEGER NOT NULL DEFAULT 0,
    sync_error TEXT,
    UNIQUE(bot_id, group_id, announcement_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_announcements_sync_status
ON announcements(sync_status, id);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_log(created_at);

CREATE TABLE IF NOT EXISTS admin_users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    password_iterations INTEGER NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gui_sessions (
    token_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    csrf_token TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(username) REFERENCES admin_users(username) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_gui_sessions_expires ON gui_sessions(expires_at);
"""


class Database:
    """Small SQLite repository with explicit, auditable state transitions."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(SCHEMA)
            self._ensure_bot_columns(connection)
            self._migrate_legacy_bot_constraints(connection)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_group_time "
                "ON group_messages(group_id, sent_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_announcements_sync_status "
                "ON announcements(sync_status, id)"
            )

    @staticmethod
    def _ensure_bot_columns(connection: sqlite3.Connection) -> None:
        for table in ("join_requests", "group_messages", "moderation_runs", "announcements"):
            columns = {
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if "bot_id" not in columns:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN bot_id TEXT NOT NULL DEFAULT 'default'"
                )

    @staticmethod
    def _migrate_legacy_bot_constraints(connection: sqlite3.Connection) -> None:
        def normalized_schema(table: str) -> str:
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            return "".join(str(row["sql"] if row else "").lower().split())

        if "unique(bot_id,request_flag)" not in normalized_schema("join_requests"):
            connection.executescript(
                """
                ALTER TABLE join_requests RENAME TO join_requests_legacy;
                CREATE TABLE join_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL DEFAULT 'default',
                    event_id TEXT NOT NULL,
                    request_flag TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    comment TEXT NOT NULL,
                    sub_type TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    decision TEXT,
                    confidence REAL,
                    reason TEXT,
                    action_status TEXT NOT NULL DEFAULT 'received',
                    updated_at TEXT NOT NULL,
                    UNIQUE(bot_id, request_flag)
                );
                INSERT INTO join_requests (
                    id, bot_id, event_id, request_flag, group_id, user_id, comment, sub_type,
                    received_at, decision, confidence, reason, action_status, updated_at
                )
                SELECT id, bot_id, event_id, request_flag, group_id, user_id, comment, sub_type,
                       received_at, decision, confidence, reason, action_status, updated_at
                FROM join_requests_legacy;
                DROP TABLE join_requests_legacy;
                """
            )

        if "primarykey(bot_id,group_id,message_id)" not in normalized_schema("group_messages"):
            connection.executescript(
                """
                ALTER TABLE group_messages RENAME TO group_messages_legacy;
                CREATE TABLE group_messages (
                    bot_id TEXT NOT NULL DEFAULT 'default',
                    message_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    raw_event_json TEXT NOT NULL,
                    PRIMARY KEY (bot_id, group_id, message_id)
                );
                INSERT INTO group_messages (
                    bot_id, message_id, group_id, user_id, text, sent_at, raw_event_json
                )
                SELECT bot_id, message_id, group_id, user_id, text, sent_at, raw_event_json
                FROM group_messages_legacy;
                DROP TABLE group_messages_legacy;
                """
            )

        if "unique(bot_id,group_id,window_start,window_end)" not in normalized_schema(
            "moderation_runs"
        ):
            connection.executescript(
                """
                ALTER TABLE moderation_runs RENAME TO moderation_runs_legacy;
                CREATE TABLE moderation_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL DEFAULT 'default',
                    group_id TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    message_count INTEGER NOT NULL,
                    max_risk REAL NOT NULL,
                    result_json TEXT NOT NULL,
                    alerted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(bot_id, group_id, window_start, window_end)
                );
                INSERT INTO moderation_runs (
                    id, bot_id, group_id, window_start, window_end, message_count, max_risk,
                    result_json, alerted, created_at
                )
                SELECT id, bot_id, group_id, window_start, window_end, message_count, max_risk,
                       result_json, alerted, created_at
                FROM moderation_runs_legacy;
                DROP TABLE moderation_runs_legacy;
                """
            )

        if "unique(bot_id,group_id,announcement_id,content_hash)" not in normalized_schema(
            "announcements"
        ):
            connection.executescript(
                """
                ALTER TABLE announcements RENAME TO announcements_legacy;
                CREATE TABLE announcements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL DEFAULT 'default',
                    announcement_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    author_id TEXT NOT NULL,
                    published_at TEXT,
                    source_payload_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    sync_status TEXT NOT NULL DEFAULT 'pending',
                    sync_attempts INTEGER NOT NULL DEFAULT 0,
                    sync_error TEXT,
                    UNIQUE(bot_id, group_id, announcement_id, content_hash)
                );
                INSERT INTO announcements (
                    id, bot_id, announcement_id, group_id, content_hash, title, content,
                    author_id, published_at, source_payload_json, first_seen_at, last_seen_at,
                    sync_status, sync_attempts, sync_error
                )
                SELECT id, bot_id, announcement_id, group_id, content_hash, title, content,
                       author_id, published_at, source_payload_json, first_seen_at, last_seen_at,
                       sync_status, sync_attempts, sync_error
                FROM announcements_legacy;
                DROP TABLE announcements_legacy;
                """
            )

    def save_join_request(self, request: JoinRequest) -> bool:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO join_requests (
                    bot_id, event_id, request_flag, group_id, user_id, comment, sub_type,
                    received_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.bot_id,
                    request.event_id,
                    request.flag,
                    request.group_id,
                    request.user_id,
                    request.comment,
                    request.sub_type,
                    request.received_at.isoformat(),
                    now,
                ),
            )
            return cursor.rowcount == 1

    def update_join_decision(
        self, request: JoinRequest, decision: JoinDecision, action_status: str
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE join_requests
                SET decision = ?, confidence = ?, reason = ?, action_status = ?, updated_at = ?
                WHERE bot_id = ? AND request_flag = ?
                """,
                (
                    decision.decision,
                    decision.confidence,
                    decision.reason,
                    action_status,
                    utc_now().isoformat(),
                    request.bot_id,
                    request.flag,
                ),
            )

    def mark_join_detected(self, request: JoinRequest) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE join_requests SET action_status = 'detected', updated_at = ?
                WHERE bot_id = ? AND request_flag = ?
                """,
                (utc_now().isoformat(), request.bot_id, request.flag),
            )

    def save_message(self, message: GroupMessage) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO group_messages (
                    bot_id, message_id, group_id, user_id, text, sent_at, raw_event_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.bot_id,
                    message.message_id,
                    message.group_id,
                    message.user_id,
                    message.text,
                    message.sent_at.isoformat(),
                    json.dumps(message.raw_event, ensure_ascii=False),
                ),
            )
            return cursor.rowcount == 1

    def messages_between(
        self,
        group_id: str,
        start: datetime,
        end: datetime,
        limit: int,
        bot_id: str = "default",
    ) -> list[GroupMessage]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM group_messages
                WHERE bot_id = ? AND group_id = ? AND sent_at >= ? AND sent_at < ?
                ORDER BY sent_at DESC LIMIT ?
                """,
                (bot_id, group_id, start.isoformat(), end.isoformat(), limit),
            ).fetchall()
        rows = list(reversed(rows))
        return [
            GroupMessage(
                bot_id=row["bot_id"],
                message_id=row["message_id"],
                group_id=row["group_id"],
                user_id=row["user_id"],
                text=row["text"],
                sent_at=datetime.fromisoformat(row["sent_at"]),
                raw_event=json.loads(row["raw_event_json"]),
            )
            for row in rows
        ]

    def moderation_run_exists(
        self, group_id: str, start: datetime, end: datetime, bot_id: str = "default"
    ) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM moderation_runs
                WHERE bot_id = ? AND group_id = ? AND window_start = ? AND window_end = ?
                """,
                (bot_id, group_id, start.isoformat(), end.isoformat()),
            ).fetchone()
        return row is not None

    def save_moderation_run(
        self,
        group_id: str,
        start: datetime,
        end: datetime,
        message_count: int,
        max_risk: float,
        result: dict[str, Any],
        alerted: bool,
        bot_id: str = "default",
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO moderation_runs (
                    bot_id, group_id, window_start, window_end, message_count, max_risk,
                    result_json, alerted, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bot_id,
                    group_id,
                    start.isoformat(),
                    end.isoformat(),
                    message_count,
                    max_risk,
                    json.dumps(result, ensure_ascii=False),
                    int(alerted),
                    utc_now().isoformat(),
                ),
            )

    @staticmethod
    def _announcement_hash(announcement: Announcement) -> str:
        normalized = "\n".join(
            [announcement.title.strip(), announcement.content.strip(), announcement.author_id]
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def upsert_announcement(self, announcement: Announcement) -> bool:
        now = utc_now().isoformat()
        content_hash = self._announcement_hash(announcement)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO announcements (
                    bot_id, announcement_id, group_id, content_hash, title, content,
                    author_id, published_at, source_payload_json, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    announcement.bot_id,
                    announcement.announcement_id,
                    announcement.group_id,
                    content_hash,
                    announcement.title,
                    announcement.content,
                    announcement.author_id,
                    announcement.published_at.isoformat() if announcement.published_at else None,
                    json.dumps(announcement.source_payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            if cursor.rowcount == 0:
                connection.execute(
                    """
                    UPDATE announcements SET last_seen_at = ?
                    WHERE bot_id = ? AND group_id = ? AND announcement_id = ? AND content_hash = ?
                    """,
                    (
                        now,
                        announcement.bot_id,
                        announcement.group_id,
                        announcement.announcement_id,
                        content_hash,
                    ),
                )
            return cursor.rowcount == 1

    def pending_announcements(
        self, limit: int = 100, group_id: str | None = None, bot_id: str | None = None
    ) -> list[tuple[int, Announcement]]:
        where = "sync_status IN ('pending', 'failed')"
        values: list[object] = []
        if group_id is not None:
            where += " AND group_id = ?"
            values.append(group_id)
        if bot_id is not None:
            where += " AND bot_id = ?"
            values.append(bot_id)
        values.append(limit)
        params = tuple(values)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM announcements
                WHERE {where}
                ORDER BY id ASC LIMIT ?
                """,
                params,
            ).fetchall()
        items: list[tuple[int, Announcement]] = []
        for row in rows:
            items.append(
                (
                    row["id"],
                    Announcement(
                        bot_id=row["bot_id"],
                        announcement_id=row["announcement_id"],
                        group_id=row["group_id"],
                        title=row["title"],
                        content=row["content"],
                        author_id=row["author_id"],
                        published_at=(
                            datetime.fromisoformat(row["published_at"])
                            if row["published_at"]
                            else None
                        ),
                        source_payload=json.loads(row["source_payload_json"]),
                    ),
                )
            )
        return items

    def mark_announcement_sync(self, row_id: int, success: bool, error: str = "") -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE announcements
                SET sync_status = ?, sync_attempts = sync_attempts + 1, sync_error = ?
                WHERE id = ?
                """,
                ("synced" if success else "failed", error[:2000] or None, row_id),
            )

    def audit(
        self,
        action: str,
        status: str,
        subject_type: str,
        subject_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_log (
                    action, status, subject_type, subject_id, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    action,
                    status,
                    subject_type,
                    subject_id,
                    json.dumps(details or {}, ensure_ascii=False, default=str),
                    utc_now().isoformat(),
                ),
            )

    def ensure_admin_user(
        self,
        username: str,
        password_hash: str,
        password_salt: str,
        password_iterations: int,
    ) -> bool:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO admin_users (
                    username, password_hash, password_salt, password_iterations,
                    must_change_password, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (username, password_hash, password_salt, password_iterations, now, now),
            )
            return cursor.rowcount == 1

    def get_admin_user(self, username: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM admin_users WHERE username = ?", (username,)
            ).fetchone()
        return dict(row) if row else None

    def update_admin_password(
        self,
        username: str,
        password_hash: str,
        password_salt: str,
        password_iterations: int,
        keep_session_hash: str,
    ) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE admin_users
                SET password_hash = ?, password_salt = ?, password_iterations = ?,
                    must_change_password = 0, updated_at = ?
                WHERE username = ?
                """,
                (
                    password_hash,
                    password_salt,
                    password_iterations,
                    utc_now().isoformat(),
                    username,
                ),
            )
            connection.execute(
                "DELETE FROM gui_sessions WHERE username = ? AND token_hash != ?",
                (username, keep_session_hash),
            )
            return cursor.rowcount == 1

    def create_gui_session(
        self, token_hash: str, username: str, csrf_token: str, expires_at: datetime
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO gui_sessions (
                    token_hash, username, csrf_token, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    token_hash,
                    username,
                    csrf_token,
                    expires_at.isoformat(),
                    utc_now().isoformat(),
                ),
            )

    def get_gui_session(self, token_hash: str) -> dict[str, Any] | None:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM gui_sessions WHERE expires_at <= ?", (now,))
            row = connection.execute(
                """
                SELECT s.token_hash, s.username, s.csrf_token, s.expires_at,
                       u.must_change_password
                FROM gui_sessions s
                JOIN admin_users u ON u.username = s.username
                WHERE s.token_hash = ? AND s.expires_at > ?
                """,
                (token_hash, now),
            ).fetchone()
        return dict(row) if row else None

    def delete_gui_session(self, token_hash: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM gui_sessions WHERE token_hash = ?", (token_hash,))

    @staticmethod
    def _decoded_rows(
        rows: list[sqlite3.Row], json_columns: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for column in json_columns:
                if item.get(column):
                    try:
                        item[column] = json.loads(item[column])
                    except json.JSONDecodeError:
                        pass
            result.append(item)
        return result

    def recent_records(self, kind: str, limit: int = 50) -> list[dict[str, Any]]:
        definitions = {
            "joins": ("join_requests", "received_at", ()),
            "messages": ("group_messages", "sent_at", ("raw_event_json",)),
            "moderation": ("moderation_runs", "created_at", ("result_json",)),
            "announcements": ("announcements", "last_seen_at", ("source_payload_json",)),
            "audit": ("audit_log", "created_at", ("details_json",)),
        }
        if kind not in definitions:
            raise ValueError(f"Unsupported record kind: {kind}")
        table, order_column, json_columns = definitions[kind]
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM {table} ORDER BY {order_column} DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return self._decoded_rows(rows, json_columns)

    def counts(self) -> dict[str, int]:
        tables = ["join_requests", "group_messages", "moderation_runs", "announcements"]
        result: dict[str, int] = {}
        with self._lock, self._connect() as connection:
            for table in tables:
                result[table] = int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
        return result

    def prune(
        self,
        *,
        messages_before: datetime,
        joins_before: datetime,
        moderation_before: datetime,
        audit_before: datetime,
    ) -> dict[str, int]:
        statements = {
            "group_messages": ("sent_at", messages_before.isoformat()),
            "join_requests": ("received_at", joins_before.isoformat()),
            "moderation_runs": ("window_end", moderation_before.isoformat()),
            "audit_log": ("created_at", audit_before.isoformat()),
        }
        deleted: dict[str, int] = {}
        with self._lock, self._connect() as connection:
            for table, (column, cutoff) in statements.items():
                cursor = connection.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
                deleted[table] = cursor.rowcount
        return deleted
