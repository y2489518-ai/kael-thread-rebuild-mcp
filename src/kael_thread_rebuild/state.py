from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .io import atomic_write_json


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self.root = state_dir
        self.operations = state_dir / "operations"
        self.lock_path = state_dir / "coordinator.lock"

    def initialize(self) -> None:
        self.operations.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.initialize()
        with self.lock_path.open("a+") as stream:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def path(self, operation_id: str) -> Path:
        if not operation_id or any(char not in "0123456789abcdef-" for char in operation_id.lower()):
            raise ValueError("invalid operation id")
        return self.operations / f"{operation_id}.json"

    def save(self, operation: dict[str, Any]) -> None:
        operation["updated_at"] = utc_now()
        atomic_write_json(self.path(str(operation["operation_id"])), operation)

    def load(self, operation_id: str) -> dict[str, Any]:
        with self.path(operation_id).open(encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError("operation state is not an object")
        return value

    def all(self) -> list[dict[str, Any]]:
        self.initialize()
        result: list[dict[str, Any]] = []
        for path in sorted(self.operations.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                result.append(value)
        return result

    def latest(self) -> dict[str, Any] | None:
        items = self.all()
        return items[0] if items else None

    def active(self) -> dict[str, Any] | None:
        active_statuses = {
            "pending",
            "scheduled",
            "running",
            "verifying",
            "activating",
            "rollback_pending",
            "rollback_scheduled",
        }
        return next((item for item in self.all() if item.get("status") in active_statuses), None)

    def new_pending(self, reason: str) -> dict[str, Any]:
        now = utc_now()
        return {
            "operation_id": str(uuid.uuid4()),
            "status": "pending",
            "reasons": [reason],
            "requested_at": now,
            "created_at": now,
            "updated_at": now,
        }
