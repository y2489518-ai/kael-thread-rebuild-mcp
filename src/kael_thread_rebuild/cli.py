from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .config import DEFAULT_CONFIG_ENV, RebuildConfig
from .coordinator import RebuildCoordinator


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Kael Claude Code thread rebuild coordinator")
    result.add_argument("--config", type=Path, default=os.environ.get(DEFAULT_CONFIG_ENV, "config.toml"))
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    dirty = commands.add_parser("dirty")
    dirty.add_argument("--transcript", type=Path)
    plan = commands.add_parser("plan")
    plan.add_argument("--transcript", type=Path)
    request = commands.add_parser("request")
    request.add_argument("--reason", default="manual request")
    request.add_argument("--confirm", required=True)
    status = commands.add_parser("status")
    status.add_argument("--operation")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("--operation", required=True)
    cancel.add_argument("--confirm", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--operation", required=True)
    prepare.add_argument("--transcript", type=Path, required=True)
    activate = commands.add_parser("activate")
    activate.add_argument("--operation", required=True)
    rollback = commands.add_parser("rollback-request")
    rollback.add_argument("--operation", required=True)
    rollback.add_argument("--confirm", required=True)
    hook = commands.add_parser("hook-stop")
    hook.add_argument("--input", type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    config = RebuildConfig.load(args.config)
    coordinator = RebuildCoordinator(config)
    try:
        if args.command == "doctor":
            emit(coordinator.doctor())
        elif args.command == "dirty":
            emit(coordinator.dirty(args.transcript))
        elif args.command == "plan":
            emit(coordinator.plan(args.transcript))
        elif args.command == "request":
            emit(coordinator.request(args.reason, args.confirm))
        elif args.command == "status":
            emit(coordinator.status(args.operation))
        elif args.command == "cancel":
            emit(coordinator.cancel(args.operation, args.confirm))
        elif args.command == "prepare":
            emit(coordinator.prepare(args.operation, args.transcript))
        elif args.command == "activate":
            emit(coordinator.activate(args.operation))
        elif args.command == "rollback-request":
            emit(coordinator.request_rollback(args.operation, args.confirm))
        elif args.command == "hook-stop":
            raw = args.input.read_text(encoding="utf-8") if args.input else sys.stdin.read()
            hook_input = json.loads(raw or "{}")
            coordinator.handle_stop_hook(hook_input, args.config.resolve())
            emit({"suppressOutput": True})
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
