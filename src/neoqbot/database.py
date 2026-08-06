from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
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
    sync_claimed_at TEXT,
    is_current INTEGER NOT NULL DEFAULT 1,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT,
    source_bot_ids_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(bot_id, group_id, announcement_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_announcements_sync_status
ON announcements(sync_status, id);
CREATE INDEX IF NOT EXISTS idx_announcements_group_seen
ON announcements(group_id, last_seen_at DESC);

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
    role TEXT NOT NULL DEFAULT 'operator',
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
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(admin_users)").fetchall()
            }
            if "role" not in columns:
                # Before multi-user support every GUI account was an administrator.
                connection.execute(
                    "ALTER TABLE admin_users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'"
                )
            self._ensure_column(connection, "announcements", "sync_claimed_at", "TEXT")
            self._ensure_column(
                connection, "announcements", "is_current", "INTEGER NOT NULL DEFAULT 1"
            )
            self._ensure_column(
                connection, "announcements", "is_deleted", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(connection, "announcements", "deleted_at", "TEXT")
            self._ensure_column(
                connection,
                "announcements",
                "source_bot_ids_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self._migrate_announcement_archive(connection)

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, declaration: str
    ) -> None:
        columns = {
            str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @staticmethod
    def _source_bot_ids(row: sqlite3.Row) -> set[str]:
        bot_ids = {str(row["bot_id"])}
        try:
            bot_ids.update(str(item) for item in json.loads(row["source_bot_ids_json"] or "[]"))
        except (json.JSONDecodeError, TypeError):
            pass
        return {item for item in bot_ids if item}

    def _migrate_announcement_archive(self, connection: sqlite3.Connection) -> None:
        """Collapse historical cross-Bot duplicates before enforcing group-level identity."""
        connection.execute("DROP INDEX IF EXISTS uq_announcements_group_version")
        rows = connection.execute("SELECT * FROM announcements ORDER BY id ASC").fetchall()
        grouped: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
        for row in rows:
            canonical_hash = self._announcement_content_hash(str(row["title"]), str(row["content"]))
            key = (str(row["group_id"]), str(row["announcement_id"]), canonical_hash)
            grouped.setdefault(key, []).append(row)

        status_priority = {"synced": 4, "pending": 3, "failed": 2, "syncing": 1}
        for (_, _, canonical_hash), duplicates in grouped.items():
            keeper = duplicates[0]
            bot_ids: set[str] = set()
            for row in duplicates:
                bot_ids.update(self._source_bot_ids(row))
            status = max(
                (str(row["sync_status"]) for row in duplicates),
                key=lambda value: status_priority.get(value, 0),
            )
            if status == "syncing":
                status = "failed"
            active = any(not bool(row["is_deleted"]) for row in duplicates)
            errors = [str(row["sync_error"]) for row in duplicates if row["sync_error"]]
            authors = [str(row["author_id"]) for row in duplicates if row["author_id"]]
            published = [str(row["published_at"]) for row in duplicates if row["published_at"]]
            source_payload = max(
                (str(row["source_payload_json"] or "{}") for row in duplicates),
                key=len,
            )
            for duplicate in duplicates[1:]:
                connection.execute("DELETE FROM announcements WHERE id = ?", (duplicate["id"],))
            connection.execute(
                """
                UPDATE announcements
                SET content_hash = ?, author_id = ?, published_at = ?, source_payload_json = ?,
                    first_seen_at = ?, last_seen_at = ?, sync_status = ?,
                    sync_attempts = ?, sync_error = ?, sync_claimed_at = NULL,
                    is_deleted = ?, deleted_at = ?, source_bot_ids_json = ?
                WHERE id = ?
                """,
                (
                    canonical_hash,
                    authors[0] if authors else "",
                    min(published) if published else None,
                    source_payload,
                    min(str(row["first_seen_at"]) for row in duplicates),
                    max(str(row["last_seen_at"]) for row in duplicates),
                    status,
                    max(int(row["sync_attempts"]) for row in duplicates),
                    errors[-1] if errors else None,
                    0 if active else 1,
                    None
                    if active
                    else max(
                        (str(row["deleted_at"]) for row in duplicates if row["deleted_at"]),
                        default=None,
                    ),
                    json.dumps(sorted(bot_ids), ensure_ascii=False),
                    int(keeper["id"]),
                ),
            )

        connection.execute("UPDATE announcements SET is_current = 0")
        versions = connection.execute(
            """
            SELECT id, group_id, announcement_id FROM announcements
            ORDER BY group_id, announcement_id, id DESC
            """
        ).fetchall()
        current_keys: set[tuple[str, str]] = set()
        for row in versions:
            key = (str(row["group_id"]), str(row["announcement_id"]))
            if key in current_keys:
                continue
            current_keys.add(key)
            connection.execute("UPDATE announcements SET is_current = 1 WHERE id = ?", (row["id"],))
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_announcements_group_version
            ON announcements(group_id, announcement_id, content_hash)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_announcements_group_seen
            ON announcements(group_id, last_seen_at DESC)
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
    def _announcement_content_hash(title: str, content: str) -> str:
        def normalized_text(value: str) -> str:
            lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            return "\n".join(line.rstrip() for line in lines).strip()

        normalized = "\n".join([normalized_text(title), normalized_text(content)])
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @classmethod
    def _announcement_hash(cls, announcement: Announcement) -> str:
        return cls._announcement_content_hash(announcement.title, announcement.content)

    def upsert_announcement(self, announcement: Announcement) -> bool:
        now = utc_now().isoformat()
        content_hash = self._announcement_hash(announcement)
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM announcements
                WHERE group_id = ? AND announcement_id = ? AND content_hash = ?
                """,
                (announcement.group_id, announcement.announcement_id, content_hash),
            ).fetchone()
            if existing is not None:
                bot_ids = self._source_bot_ids(existing)
                bot_ids.add(announcement.bot_id)
                connection.execute(
                    """
                    UPDATE announcements
                    SET last_seen_at = ?, is_deleted = 0, deleted_at = NULL,
                        source_bot_ids_json = ?,
                        author_id = CASE WHEN author_id = '' THEN ? ELSE author_id END,
                        published_at = COALESCE(published_at, ?),
                        source_payload_json = CASE
                            WHEN source_payload_json IN ('', '{}', 'null') THEN ?
                            ELSE source_payload_json
                        END
                    WHERE id = ?
                    """,
                    (
                        now,
                        json.dumps(sorted(bot_ids), ensure_ascii=False),
                        announcement.author_id,
                        announcement.published_at.isoformat()
                        if announcement.published_at
                        else None,
                        json.dumps(announcement.source_payload, ensure_ascii=False),
                        int(existing["id"]),
                    ),
                )
                connection.execute(
                    """
                    UPDATE announcements SET is_deleted = 0, deleted_at = NULL
                    WHERE group_id = ? AND announcement_id = ?
                    """,
                    (announcement.group_id, announcement.announcement_id),
                )
                return False

            connection.execute(
                """
                UPDATE announcements SET is_current = 0
                WHERE group_id = ? AND announcement_id = ?
                """,
                (announcement.group_id, announcement.announcement_id),
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO announcements (
                    bot_id, announcement_id, group_id, content_hash, title, content,
                    author_id, published_at, source_payload_json, first_seen_at, last_seen_at,
                    is_current, is_deleted, source_bot_ids_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?)
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
                    json.dumps([announcement.bot_id], ensure_ascii=False),
                ),
            )
            return cursor.rowcount == 1

    def reconcile_announcements(self, group_id: str, seen_ids: set[str]) -> int:
        """Mark announcements missing from a successful full fetch without deleting the archive."""
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            conditions = ["group_id = ?", "is_deleted = 0"]
            values: list[object] = [group_id]
            if seen_ids:
                placeholders = ", ".join("?" for _ in seen_ids)
                conditions.append(f"announcement_id NOT IN ({placeholders})")
                values.extend(sorted(seen_ids))
            ids = connection.execute(
                "SELECT DISTINCT announcement_id FROM announcements WHERE "
                + " AND ".join(conditions),
                tuple(values),
            ).fetchall()
            if ids:
                deleted_ids = [str(row["announcement_id"]) for row in ids]
                placeholders = ", ".join("?" for _ in deleted_ids)
                connection.execute(
                    f"""
                    UPDATE announcements SET is_deleted = 1, deleted_at = ?
                    WHERE group_id = ? AND announcement_id IN ({placeholders})
                    """,
                    (now, group_id, *deleted_ids),
                )
            if seen_ids:
                placeholders = ", ".join("?" for _ in seen_ids)
                connection.execute(
                    f"""
                    UPDATE announcements SET is_deleted = 0, deleted_at = NULL
                    WHERE group_id = ? AND announcement_id IN ({placeholders})
                    """,
                    (group_id, *sorted(seen_ids)),
                )
            return len(ids)

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

    def claim_pending_announcements(
        self, limit: int = 100, group_id: str | None = None
    ) -> list[tuple[int, Announcement]]:
        """Atomically claim archive work so concurrent Bot loops cannot export it twice."""
        stale_before = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
        claimed_at = utc_now().isoformat()
        where = "sync_status IN ('pending', 'failed')"
        values: list[object] = []
        if group_id is not None:
            where += " AND group_id = ?"
            values.append(group_id)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE announcements
                SET sync_status = 'failed', sync_claimed_at = NULL,
                    sync_error = COALESCE(sync_error, 'stale sync claim recovered')
                WHERE sync_status = 'syncing'
                  AND (sync_claimed_at IS NULL OR sync_claimed_at < ?)
                """,
                (stale_before,),
            )
            rows = connection.execute(
                f"SELECT * FROM announcements WHERE {where} ORDER BY id ASC LIMIT ?",
                (*values, max(1, min(limit, 1000))),
            ).fetchall()
            if rows:
                placeholders = ", ".join("?" for _ in rows)
                connection.execute(
                    f"""
                    UPDATE announcements SET sync_status = 'syncing', sync_claimed_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    (claimed_at, *(int(row["id"]) for row in rows)),
                )
        items: list[tuple[int, Announcement]] = []
        for row in rows:
            items.append(
                (
                    int(row["id"]),
                    Announcement(
                        bot_id=str(row["bot_id"]),
                        announcement_id=str(row["announcement_id"]),
                        group_id=str(row["group_id"]),
                        title=str(row["title"]),
                        content=str(row["content"]),
                        author_id=str(row["author_id"]),
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
                SET sync_status = ?, sync_attempts = sync_attempts + 1, sync_error = ?,
                    sync_claimed_at = NULL
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
                    role, must_change_password, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'admin', 1, ?, ?)
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

    def get_gui_administrator(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM admin_users
                WHERE role = 'admin'
                ORDER BY created_at, username
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def rename_gui_user(self, username: str, new_username: str) -> bool:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM admin_users WHERE username = ? COLLATE NOCASE", (new_username,)
            ).fetchone()
            if exists:
                return False
            cursor = connection.execute(
                """
                INSERT INTO admin_users (
                    username, password_hash, password_salt, password_iterations,
                    role, must_change_password, created_at, updated_at
                )
                SELECT ?, password_hash, password_salt, password_iterations,
                       role, must_change_password, created_at, ?
                FROM admin_users
                WHERE username = ?
                """,
                (new_username, now, username),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                "UPDATE gui_sessions SET username = ? WHERE username = ?",
                (new_username, username),
            )
            connection.execute("DELETE FROM admin_users WHERE username = ?", (username,))
            return True

    def list_gui_users(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT u.username, u.role, u.must_change_password, u.created_at, u.updated_at,
                       COUNT(s.token_hash) AS active_sessions
                FROM admin_users u
                LEFT JOIN gui_sessions s ON s.username = u.username AND s.expires_at > ?
                GROUP BY u.username
                ORDER BY CASE WHEN u.role = 'admin' THEN 0 ELSE 1 END, u.username
                """,
                (utc_now().isoformat(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_gui_user(
        self,
        username: str,
        password_hash: str,
        password_salt: str,
        password_iterations: int,
    ) -> bool:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM admin_users WHERE username = ? COLLATE NOCASE", (username,)
            ).fetchone()
            if exists:
                return False
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO admin_users (
                    username, password_hash, password_salt, password_iterations,
                    role, must_change_password, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'operator', 1, ?, ?)
                """,
                (username, password_hash, password_salt, password_iterations, now, now),
            )
            return cursor.rowcount == 1

    def reset_gui_user_password(
        self,
        username: str,
        password_hash: str,
        password_salt: str,
        password_iterations: int,
    ) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE admin_users
                SET password_hash = ?, password_salt = ?, password_iterations = ?,
                    must_change_password = 1, updated_at = ?
                WHERE username = ? AND role = 'operator'
                """,
                (
                    password_hash,
                    password_salt,
                    password_iterations,
                    utc_now().isoformat(),
                    username,
                ),
            )
            if cursor.rowcount:
                connection.execute("DELETE FROM gui_sessions WHERE username = ?", (username,))
            return cursor.rowcount == 1

    def delete_gui_user(self, username: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM admin_users WHERE username = ? AND role = 'operator'",
                (username,),
            )
            return cursor.rowcount == 1

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

    def reset_bootstrap_admin_password(
        self,
        username: str,
        password_hash: str,
        password_salt: str,
        password_iterations: int,
    ) -> bool:
        """Replace an unchanged bootstrap credential and invalidate its sessions."""
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE admin_users
                SET password_hash = ?, password_salt = ?, password_iterations = ?, updated_at = ?
                WHERE username = ? AND must_change_password = 1
                """,
                (
                    password_hash,
                    password_salt,
                    password_iterations,
                    utc_now().isoformat(),
                    username,
                ),
            )
            if cursor.rowcount:
                connection.execute("DELETE FROM gui_sessions WHERE username = ?", (username,))
            return cursor.rowcount == 1

    def create_gui_session(
        self,
        token_hash: str,
        username: str,
        csrf_token: str,
        expires_at: datetime,
        max_sessions: int = 5,
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
            connection.execute(
                """
                DELETE FROM gui_sessions
                WHERE username = ? AND token_hash NOT IN (
                    SELECT token_hash FROM gui_sessions
                    WHERE username = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                )
                """,
                (username, username, max_sessions),
            )

    def get_gui_session(self, token_hash: str) -> dict[str, Any] | None:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM gui_sessions WHERE expires_at <= ?", (now,))
            row = connection.execute(
                """
                SELECT s.token_hash, s.username, s.csrf_token, s.expires_at,
                       u.must_change_password, u.role
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

    @staticmethod
    def _record_definitions() -> dict[str, tuple[str, str, tuple[str, ...], tuple[str, ...]]]:
        return {
            "joins": (
                "join_requests",
                "received_at",
                (),
                ("group_id", "user_id", "comment", "decision", "action_status"),
            ),
            "messages": (
                "group_messages",
                "sent_at",
                ("raw_event_json",),
                ("group_id", "user_id", "text", "message_id"),
            ),
            "moderation": (
                "moderation_runs",
                "created_at",
                ("result_json",),
                ("group_id", "result_json"),
            ),
            "announcements": (
                "announcements",
                "last_seen_at",
                ("source_payload_json", "source_bot_ids_json"),
                ("group_id", "title", "content", "announcement_id", "sync_status"),
            ),
            "audit": (
                "audit_log",
                "created_at",
                ("details_json",),
                ("action", "status", "subject_type", "subject_id", "details_json"),
            ),
        }

    def _record_query_parts(
        self,
        kind: str,
        *,
        group_id: str | None = None,
        bot_id: str | None = None,
        search: str | None = None,
        since: datetime | None = None,
    ) -> tuple[str, str, tuple[str, ...], list[str], list[object]]:
        definitions = self._record_definitions()
        if kind not in definitions:
            raise ValueError(f"Unsupported record kind: {kind}")
        table, order_column, json_columns, search_columns = definitions[kind]
        conditions: list[str] = []
        values: list[object] = []
        if group_id is not None and kind != "audit":
            conditions.append("group_id = ?")
            values.append(group_id)
        if bot_id is not None and kind != "audit":
            conditions.append("bot_id = ?")
            values.append(bot_id)
        if since is not None:
            conditions.append(f"{order_column} >= ?")
            values.append(since.isoformat())
        if search and search.strip():
            escaped_search = (
                search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            conditions.append(
                "(" + " OR ".join(f"{column} LIKE ? ESCAPE '\\'" for column in search_columns) + ")"
            )
            values.extend(f"%{escaped_search}%" for _ in search_columns)
        return table, order_column, json_columns, conditions, values

    def recent_records(
        self,
        kind: str,
        limit: int = 50,
        *,
        group_id: str | None = None,
        bot_id: str | None = None,
        search: str | None = None,
        offset: int = 0,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        table, order_column, json_columns, conditions, values = self._record_query_parts(
            kind,
            group_id=group_id,
            bot_id=bot_id,
            search=search,
            since=since,
        )
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        values.append(max(1, min(limit, 201)))
        values.append(max(0, offset))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM {table}{where} ORDER BY {order_column} DESC LIMIT ? OFFSET ?",
                tuple(values),
            ).fetchall()
        return self._decoded_rows(rows, json_columns)

    def all_group_records(
        self, kind: str, group_id: str, *, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        table, order_column, json_columns, conditions, values = self._record_query_parts(
            kind, group_id=group_id, since=since
        )
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM {table}{where} ORDER BY {order_column} ASC", tuple(values)
            ).fetchall()
        return self._decoded_rows(rows, json_columns)

    def delete_group_records(
        self, kind: str, group_id: str, *, since: datetime | None = None
    ) -> int:
        if kind not in {"joins", "messages", "moderation", "announcements"}:
            raise ValueError(f"Unsupported group record kind: {kind}")
        table, _, _, conditions, values = self._record_query_parts(
            kind, group_id=group_id, since=since
        )
        where = " AND ".join(conditions)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(f"DELETE FROM {table} WHERE {where}", tuple(values))
            return cursor.rowcount

    def group_overview(
        self, group_id: str, *, bot_ids: list[str] | None = None, limit: int = 40
    ) -> dict[str, Any]:
        definitions = {
            kind: values[:3]
            for kind, values in self._record_definitions().items()
            if kind in {"joins", "messages", "moderation", "announcements"}
        }
        counts: dict[str, int] = {}
        records: dict[str, list[dict[str, Any]]] = {}
        capped_limit = max(1, min(limit, 100))
        with self._lock, self._connect() as connection:
            for kind, (table, order_column, json_columns) in definitions.items():
                conditions = ["group_id = ?"]
                values: list[object] = [group_id]
                if bot_ids:
                    placeholders = ", ".join("?" for _ in bot_ids)
                    conditions.append(f"bot_id IN ({placeholders})")
                    values.extend(bot_ids)
                where = " AND ".join(conditions)
                counts[kind] = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {where}", tuple(values)
                    ).fetchone()[0]
                )
                rows = connection.execute(
                    f"SELECT * FROM {table} WHERE {where} ORDER BY {order_column} DESC LIMIT ?",
                    (*values, capped_limit),
                ).fetchall()
                records[kind] = self._decoded_rows(rows, json_columns)
        return {"group_id": group_id, "counts": counts, "records": records}

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
