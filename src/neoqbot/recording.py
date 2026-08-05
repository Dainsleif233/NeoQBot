from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path

from .models import GroupMessage


class LocalMessageRecorder:
    """Append-only, human-readable group message archive stored outside SQLite."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._lock = threading.RLock()

    @staticmethod
    def _safe_part(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
        return cleaned[:96] or "unknown"

    def append(self, message: GroupMessage) -> Path:
        day = message.sent_at.strftime("%Y-%m-%d")
        target = (
            self.root
            / self._safe_part(message.bot_id)
            / self._safe_part(message.group_id)
            / f"{day}.jsonl"
        )
        line = (
            json.dumps(message.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
            + "\n"
        )
        with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
        return target

    def prune(self, before: datetime) -> int:
        if not self.root.exists():
            return 0
        removed = 0
        cutoff = before.date()
        with self._lock:
            for target in self.root.glob("*/*/*.jsonl"):
                try:
                    day = datetime.strptime(target.stem, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if day < cutoff:
                    target.unlink(missing_ok=True)
                    removed += 1
            directories = sorted(
                (path for path in self.root.glob("**/*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            )
            for directory in directories:
                try:
                    directory.rmdir()
                except OSError:
                    pass
        return removed
