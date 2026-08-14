from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import RebuildConfig
from .io import atomic_write_text, file_digest
from .state import StateStore, utc_now
from .tmux import TmuxController
from .transcript import (
    dump_jsonl,
    load_jsonl,
    select_turns,
    session_id_from_events,
    verify_candidate,
)


class RebuildError(RuntimeError):
    pass


class RebuildCoordinator:
    def __init__(self, config: RebuildConfig, tmux: TmuxController | None = None) -> None:
        self.config = config
        self.state = StateStore(config.state_dir)
        self.tmux = tmux or TmuxController(config)

    def _assert_transcript(self, path: str | Path) -> Path:
        value = Path(path).expanduser().resolve()
        try:
            value.relative_to(self.config.project_dir)
        except ValueError as exc:
            raise RebuildError("transcript is outside configured project_dir") from exc
        if value.suffix != ".jsonl" or not value.is_file():
            raise RebuildError("transcript must be an existing .jsonl file")
        return value

    def latest_transcript(self) -> Path:
        candidates = [path for path in self.config.project_dir.glob("*.jsonl") if path.is_file()]
        if not candidates:
            raise RebuildError(f"no transcript found under {self.config.project_dir}")
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def doctor(self) -> dict[str, Any]:
        project_exists = self.config.project_dir.is_dir()
        transcript_count = len(list(self.config.project_dir.glob("*.jsonl"))) if project_exists else 0
        claude_binary = shutil.which(self.config.resume_command[0])
        return {
            "ok": bool(project_exists and transcript_count and self.tmux.available() and self.tmux.target_alive() and claude_binary),
            "project_dir": str(self.config.project_dir),
            "project_dir_exists": project_exists,
            "transcript_count": transcript_count,
            "state_dir": str(self.config.state_dir),
            "tmux_target": self.config.tmux_target,
            "tmux_available": self.tmux.available(),
            "tmux_target_alive": self.tmux.target_alive(),
            "tmux_pane_command": self.tmux.pane_command() if self.tmux.available() else "",
            "claude_binary": claude_binary or "",
            "active_operation": self.state.active(),
        }

    def plan(self, transcript_path: str | Path | None = None) -> dict[str, Any]:
        source = self._assert_transcript(transcript_path or self.latest_transcript())
        result = select_turns(
            load_jsonl(source),
            target_tokens=self.config.target_tokens,
            tail_turns=self.config.tail_turns,
            max_event_chars=self.config.max_event_chars,
        )
        return {
            "ok": bool(result.events) and result.poison_score < 2,
            "source_path": str(source),
            "source_sha256": file_digest(source),
            "source_session_id": session_id_from_events(load_jsonl(source)),
            "stats": result.stats(),
            "blocked_reason": "possible poisoned recent context" if result.poison_score >= 2 else "",
            "note": "read-only; no transcript or tmux state was changed",
        }

    def request(self, reason: str, confirmation: str) -> dict[str, Any]:
        if confirmation != "REBUILD":
            raise RebuildError("confirmation must be exactly REBUILD")
        reason = str(reason or "manual request").strip()[:500]
        with self.state.lock():
            active = self.state.active()
            if active:
                if active.get("status") != "pending":
                    raise RebuildError(f"operation {active['operation_id']} is already {active['status']}")
                reasons = list(active.get("reasons") or [])
                if reason not in reasons:
                    reasons.append(reason)
                active["reasons"] = reasons
                self.state.save(active)
                return active
            operation = self.state.new_pending(reason)
            self.state.save(operation)
            return operation

    def cancel(self, operation_id: str, confirmation: str) -> dict[str, Any]:
        if confirmation != "CANCEL":
            raise RebuildError("confirmation must be exactly CANCEL")
        with self.state.lock():
            operation = self.state.load(operation_id)
            if operation.get("status") != "pending":
                raise RebuildError("only a pending operation can be cancelled")
            operation["status"] = "cancelled"
            operation["cancelled_at"] = utc_now()
            self.state.save(operation)
            return operation

    def status(self, operation_id: str | None = None) -> dict[str, Any]:
        operation = self.state.load(operation_id) if operation_id else self.state.latest()
        return {"ok": True, "operation": operation}

    def _wait_stable(self, path: Path) -> str:
        deadline = time.monotonic() + self.config.stable_file_timeout_seconds
        previous = file_digest(path)
        while time.monotonic() < deadline:
            time.sleep(self.config.stable_file_seconds)
            current = file_digest(path)
            if current == previous:
                return current
            previous = current
        raise RebuildError("source transcript did not become stable before timeout")

    def prepare(self, operation_id: str, transcript_path: str | Path) -> dict[str, Any]:
        source = self._assert_transcript(transcript_path)
        with self.state.lock():
            operation = self.state.load(operation_id)
            if operation.get("status") not in {"pending", "scheduled"}:
                raise RebuildError(f"prepare requires pending/scheduled status, got {operation.get('status')}")
            operation["status"] = "running"
            operation["source_path"] = str(source)
            self.state.save(operation)

        try:
            source_digest = self._wait_stable(source)
            source_events = load_jsonl(source)
            old_session_id = session_id_from_events(source_events)
            if not old_session_id:
                raise RebuildError("source transcript has no sessionId")
            result = select_turns(
                source_events,
                target_tokens=self.config.target_tokens,
                tail_turns=self.config.tail_turns,
                max_event_chars=self.config.max_event_chars,
            )
            if result.poison_score >= 2:
                raise RebuildError("recent context looks poisoned; clean rebuild from durable memory is required")
            if not result.events:
                raise RebuildError("no completed user/final turns were selected")
            new_session_id = str(result.events[0]["sessionId"])
            candidate = self.config.project_dir / f"{new_session_id}.jsonl"
            if candidate.exists():
                raise RebuildError("candidate transcript already exists")

            operation_dir = self.config.state_dir / "artifacts" / operation_id
            operation_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            backup = operation_dir / source.name
            shutil.copy2(source, backup)
            os.chmod(backup, 0o600)
            atomic_write_text(candidate, dump_jsonl(result.events), 0o600)
            verification = verify_candidate(candidate, result.manifest, new_session_id)
            if not verification["ok"]:
                raise RebuildError("candidate verification failed: " + "; ".join(verification["errors"]))

            with self.state.lock():
                operation = self.state.load(operation_id)
                operation.update(
                    {
                        "status": "verifying",
                        "source_sha256": source_digest,
                        "source_session_id": old_session_id,
                        "new_session_id": new_session_id,
                        "candidate_path": str(candidate),
                        "candidate_sha256": file_digest(candidate),
                        "backup_path": str(backup),
                        "manifest": list(result.manifest),
                        "stats": result.stats(),
                        "verification": verification,
                    }
                )
                self.state.save(operation)
                return operation
        except Exception as exc:
            with self.state.lock():
                operation = self.state.load(operation_id)
                operation["status"] = "failed"
                operation["error"] = str(exc)
                self.state.save(operation)
            raise

    def activate(self, operation_id: str) -> dict[str, Any]:
        with self.state.lock():
            operation = self.state.load(operation_id)
            if operation.get("status") != "verifying" or not operation.get("verification", {}).get("ok"):
                raise RebuildError("activation requires a verified candidate")
            source = self._assert_transcript(operation["source_path"])
            if file_digest(source) != operation["source_sha256"]:
                operation["status"] = "failed"
                operation["error"] = "source transcript changed after prepare; refusing activation"
                self.state.save(operation)
                raise RebuildError(operation["error"])
            candidate = self._assert_transcript(operation["candidate_path"])
            if file_digest(candidate) != operation["candidate_sha256"]:
                raise RebuildError("candidate transcript changed after verification")
            operation["status"] = "activating"
            self.state.save(operation)

        result = self.tmux.respawn(operation["new_session_id"])
        if not result.ok or not self.tmux.wait_healthy():
            rollback = self.tmux.respawn(operation["source_session_id"])
            rollback_healthy = rollback.ok and self.tmux.wait_healthy()
            with self.state.lock():
                operation = self.state.load(operation_id)
                operation["status"] = "rolled_back" if rollback_healthy else "failed"
                operation["error"] = result.stderr or "new session failed tmux health check"
                operation["rollback_ok"] = rollback_healthy
                self.state.save(operation)
            raise RebuildError(operation["error"])

        with self.state.lock():
            operation = self.state.load(operation_id)
            operation["status"] = "activated"
            operation["activated_at"] = utc_now()
            operation["tmux_command"] = result.command
            self.state.save(operation)
            return operation

    def request_rollback(self, operation_id: str, confirmation: str) -> dict[str, Any]:
        if confirmation != "ROLLBACK":
            raise RebuildError("confirmation must be exactly ROLLBACK")
        with self.state.lock():
            operation = self.state.load(operation_id)
            if operation.get("status") != "activated":
                raise RebuildError("only an activated operation can be rolled back")
            operation["status"] = "rollback_pending"
            operation["rollback_requested_at"] = utc_now()
            self.state.save(operation)
            return operation

    def perform_rollback(self, operation_id: str) -> dict[str, Any]:
        with self.state.lock():
            operation = self.state.load(operation_id)
            if operation.get("status") not in {"rollback_pending", "rollback_scheduled"}:
                raise RebuildError("rollback is not pending")
        result = self.tmux.respawn(operation["source_session_id"])
        healthy = result.ok and self.tmux.wait_healthy()
        with self.state.lock():
            operation = self.state.load(operation_id)
            operation["status"] = "rolled_back" if healthy else "failed"
            operation["rollback_ok"] = healthy
            operation["rollback_at"] = utc_now()
            if not healthy:
                operation["error"] = result.stderr or "rollback session failed tmux health check"
            self.state.save(operation)
            return operation

    def handle_stop_hook(self, hook_input: dict[str, Any], config_path: Path) -> dict[str, Any]:
        with self.state.lock():
            active = self.state.active()
            if not active or active.get("status") not in {"pending", "rollback_pending"}:
                return {"scheduled": False}
            transcript = str(hook_input.get("transcript_path") or "")
            previous_status = str(active["status"])
            if previous_status == "pending" and not transcript:
                raise RebuildError("Stop hook did not provide transcript_path")
            active["status"] = "scheduled" if previous_status == "pending" else "rollback_scheduled"
            active["scheduled_at"] = utc_now()
            self.state.save(active)
        argv = [
            sys.executable,
            "-m",
            "kael_thread_rebuild.worker",
            "--config",
            str(config_path),
            "--operation",
            str(active["operation_id"]),
        ]
        if transcript:
            argv.extend(["--transcript", transcript])
        try:
            with (self.config.state_dir / "worker.log").open("ab") as error_log:
                subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=error_log,
                    start_new_session=True,
                    close_fds=True,
                )
        except Exception:
            with self.state.lock():
                active = self.state.load(str(active["operation_id"]))
                active["status"] = previous_status
                self.state.save(active)
            raise
        return {"scheduled": True, "operation_id": active["operation_id"]}
