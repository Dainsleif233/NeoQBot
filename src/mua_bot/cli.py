from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from typing import Any

import uvicorn

from .app import create_app
from .config import Settings
from .container import build_container
from .database import Database


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mua-bot", description="MUA-Bot control command")
    parser.add_argument(
        "--config", default=os.getenv("MUA_CONFIG", "config.yaml"), help="YAML configuration path"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="Start the HTTP service and schedulers")
    subparsers.add_parser("init-db", help="Initialize or migrate the SQLite database")
    subparsers.add_parser("doctor", help="Check QQ, Feishu, configuration, and database")
    subparsers.add_parser("run-moderation", help="Run one moderation cycle immediately")
    subparsers.add_parser("sync-announcements", help="Run one announcement sync immediately")
    subparsers.add_parser("prune", help="Apply configured data-retention policy immediately")
    subparsers.add_parser("show-config", help="Print effective redacted configuration")
    return parser


async def _run_once(settings: Settings, command: str) -> dict[str, Any]:
    container = build_container(settings)
    try:
        if command == "doctor":
            result: dict[str, Any] = {
                "database": {"ok": True, "counts": container.database.counts()},
                "config": {
                    "ok": not settings.diagnostics()["errors"],
                    "managed_groups": settings.qq.managed_group_ids,
                    "dry_run": settings.app.dry_run,
                    "diagnostics": settings.diagnostics(),
                },
            }
            try:
                result["qq"] = await container.qq.doctor()
            except Exception as exc:
                result["qq"] = {"ok": False, "error": str(exc)}
            try:
                result["feishu"] = await container.feishu.doctor()
            except Exception as exc:
                result["feishu"] = {"ok": False, "error": str(exc)}
            return result
        if command == "run-moderation":
            return await container.runtime.run_all_moderation()
        if command == "sync-announcements":
            return await container.runtime.sync_all_announcements()
        if command == "prune":
            return await container.runtime.run_maintenance()
        raise ValueError(f"Unsupported one-shot command: {command}")
    finally:
        await container.qq.close()
        close = getattr(container.engine, "close", None)
        if close is not None:
            await close()


def main() -> None:
    args = _parser().parse_args()
    settings = Settings.load(args.config)
    logging.basicConfig(
        level=getattr(logging, settings.app.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command == "serve":
        uvicorn.run(
            create_app(settings),
            host=settings.app.host,
            port=settings.app.port,
            log_level=settings.app.log_level.lower(),
        )
        return
    if args.command == "init-db":
        database = Database(settings.app.database_path)
        database.initialize()
        print(json.dumps({"ok": True, "database": database.counts()}, ensure_ascii=False))
        return
    if args.command == "show-config":
        print(json.dumps(settings.redacted_dict(), ensure_ascii=False, indent=2))
        return
    result = asyncio.run(_run_once(settings, args.command))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
