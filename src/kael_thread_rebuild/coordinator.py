from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import RebuildConfig
from .dirty import evaluate as evaluate_dirty
from .io import atomic_write_text, file_digest, sha256_text
from .state import StateStore, utc_now
from .systemd import make_controller
from .tmux import TmuxController
from .transcript import (
    build_source,
    dump_jsonl,
    event_text,
    freeze_startup,
    load_jsonl,
    session_id_from_events,
    verify_candidate,
)


class RebuildError(RuntimeError):
    pass


class RebuildCoordinator:
    def __init__(self, config: RebuildConfig, tmux: TmuxController | None = None) -> None:
        self.config = config
        self.state = StateStore(config.state_dir)
        self.tmux = tmux or make_controller(config)

    # ---------- helpers ----------

    def _assert_transcript(self, path: str | Path) -> Path:
        value = Path(path).expanduser().resolve()
        try:
            value.relative_to(self.config.project_dir)
        except ValueError as exc:
            raise RebuildError("transcript is outside configured project_dir") from exc
        if value.suffix != ".jsonl" or not value.is_file():
            raise RebuildError("transcript must be an existing .jsonl file")
        return value

    def _known_candidates(self, *, only_unactivated: bool = True) -> set[Path]:
        """本工具自己造出来的候选文件。

        它们躺在 project_dir 里，mtime 永远是最新的，如果不认出来会被当成
        "最新的真实会话"，也会被 CAS 当成"第三方开的新 session"。
        """
        found: set[Path] = set()
        for operation in self.state.all():
            path = operation.get("candidate_path")
            if not path:
                continue
            if only_unactivated and operation.get("status") == "activated":
                continue
            try:
                found.add(Path(str(path)).resolve())
            except (OSError, ValueError):
                continue
        return found

    def latest_transcript(self) -> Path:
        orphans = self._known_candidates()
        candidates = [
            path
            for path in self.config.project_dir.glob("*.jsonl")
            if path.is_file() and path.resolve() not in orphans
        ]
        if not candidates:
            raise RebuildError(f"no transcript found under {self.config.project_dir}")
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def _build(self, rows: list[dict[str, Any]]):
        return build_source(
            rows,
            max_event_chars=self.config.max_event_chars,
            carry_max_tokens=self.config.carry_max_tokens,
            include_open_tail=self.config.include_open_tail,
            freeze_startup_snapshot=self.config.freeze_startup_snapshot,
            stamp_turns=self.config.stamp_turns,
            poison_pattern=self.config.poison_pattern,
            carry_overflow=self.config.carry_overflow,
        )

    def _pane_identity(self) -> str:
        try:
            return self.tmux.pane_pid()
        except AttributeError:
            return ""

    def _newer_transcripts(self, source: Path, since_mtime: float, *, ignore: set[Path] | None = None) -> list[str]:
        """找出比冻结时刻更新的其他 transcript。

        有别的 jsonl 在动，说明这个 pane 后来开了另一段 session；
        这时激活会盖掉第三方，必须停手。

        自己刚写出来的候选文件必须排除：它天然比 source 新几秒，不排除的话
        每一次续窗都会把自己判成"第三方冲突"，永远激活不了。
        """
        skip = {source.resolve()} | (ignore or set())
        found: list[str] = []
        for path in self.config.project_dir.glob("*.jsonl"):
            if not path.is_file() or path.resolve() in skip:
                continue
            try:
                if path.stat().st_mtime > since_mtime + 1.0:
                    found.append(path.name)
            except OSError:
                continue
        return sorted(found)

    # ---------- read-only ----------

    def doctor(self) -> dict[str, Any]:
        project_exists = self.config.project_dir.is_dir()
        transcript_count = len(list(self.config.project_dir.glob("*.jsonl"))) if project_exists else 0
        claude_binary = shutil.which(self.config.resume_command[0])
        workdir_ok = self.config.workdir_matches_project()
        return {
            "ok": bool(
                project_exists
                and transcript_count
                and self.tmux.available()
                and self.tmux.target_alive()
                and claude_binary
                and (workdir_ok or not self.config.verify_workdir_matches_project)
            ),
            "workdir_matches_project": workdir_ok,
            "claude_workdir": str(self.config.claude_workdir),
            "expected_project_dirname": self.config.expected_project_dirname,
            "actual_project_dirname": self.config.project_dir.name,
            "project_dir": str(self.config.project_dir),
            "project_dir_exists": project_exists,
            "transcript_count": transcript_count,
            "state_dir": str(self.config.state_dir),
            "tmux_target": self.config.tmux_target,
            "tmux_available": self.tmux.available(),
            "tmux_target_alive": self.tmux.target_alive(),
            "tmux_pane_command": self.tmux.pane_command() if self.tmux.available() else "",
            "tmux_pane_pid": self._pane_identity(),
            "claude_binary": claude_binary or "",
            "active_operation": self.state.active(),
        }

    def dirty(self, transcript_path: str | Path | None = None) -> dict[str, Any]:
        """只读：当前 thread 攒了多少运行负担，该不该重建。"""
        source = self._assert_transcript(transcript_path or self.latest_transcript())
        report = evaluate_dirty(
            load_jsonl(source),
            dirty_budget_bytes=self.config.dirty_budget_bytes,
            rebuild_on_original_image_view=self.config.rebuild_on_original_image_view,
        )
        report["source_path"] = str(source)
        report["ok"] = True
        return report

    def plan(self, transcript_path: str | Path | None = None) -> dict[str, Any]:
        source = self._assert_transcript(transcript_path or self.latest_transcript())
        rows = load_jsonl(source)
        result = self._build(rows)
        report = evaluate_dirty(
            rows,
            dirty_budget_bytes=self.config.dirty_budget_bytes,
            rebuild_on_original_image_view=self.config.rebuild_on_original_image_view,
        )
        blocked_reason = ""
        if result.poison_score >= 2:
            blocked_reason = "possible poisoned recent context"
        elif result.overflow_blocked:
            blocked_reason = (
                f"carry would exceed carry_max_tokens by ~{result.overflow_tokens} tokens; "
                "carry_overflow=block keeps every turn and leaves the choice to a human "
                "(raise carry_max_tokens, or switch carry_overflow to drop_oldest)"
            )
        return {
            "ok": bool(result.events) and not blocked_reason,
            "source_path": str(source),
            "source_sha256": file_digest(source),
            "source_session_id": session_id_from_events(rows),
            "stats": result.stats(),
            "dirty": report,
            "blocked_reason": blocked_reason,
            "note": "read-only; no transcript or tmux state was changed",
        }

    def status(self, operation_id: str | None = None) -> dict[str, Any]:
        operation = self.state.load(operation_id) if operation_id else self.state.latest()
        return {"ok": True, "operation": operation}

    # ---------- write ----------

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
            if self.config.verify_workdir_matches_project and not self.config.workdir_matches_project():
                raise RebuildError(
                    "claude_workdir 与 project_dir 对不上："
                    f"workdir={self.config.claude_workdir} 应该对应目录 "
                    f"{self.config.expected_project_dirname}，实际配的是 {self.config.project_dir.name}。"
                    " 这样切换后 claude --resume 会去别的目录找 session，"
                    "报 No conversation found，而那时 pane 已经被杀了。"
                )
            source_digest = self._wait_stable(source)
            source_mtime = source.stat().st_mtime
            pane_pid = self._pane_identity()

            # 冻结优先：先把源复制成不可变快照，之后只读快照，绝不再碰那个
            # 正在被写的活文件。等稳定 + 事后校验 digest 也能保证安全，但那是
            # "事后发现、白跑一次"；先冻结是"根本不给竞态留窗口"。
            # 快照本来就要留（它就是这次 operation 的备份），只是提到读之前。
            operation_dir = self.config.state_dir / "artifacts" / operation_id
            operation_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            backup = operation_dir / source.name
            shutil.copy2(source, backup)
            os.chmod(backup, 0o600)
            frozen_digest = file_digest(backup)
            if frozen_digest != source_digest:
                raise RebuildError(
                    "source changed while it was being snapshotted; nothing was touched, retry"
                )

            source_events = load_jsonl(backup)
            old_session_id = session_id_from_events(source_events)
            if not old_session_id:
                raise RebuildError("source transcript has no sessionId")

            result = self._build(source_events)
            if result.poison_score >= 2:
                raise RebuildError("recent context looks poisoned; clean rebuild from durable memory is required")
            if result.overflow_blocked:
                raise RebuildError(
                    f"carry would exceed carry_max_tokens by ~{result.overflow_tokens} tokens and "
                    "carry_overflow=block refuses to drop turns; a human must raise the limit or choose"
                )
            if not result.events:
                raise RebuildError("no real user/assistant turns were found in the source transcript")

            snapshot = freeze_startup(source_events) if self.config.freeze_startup_snapshot else None
            startup_digest = sha256_text(event_text(snapshot)) if snapshot is not None else ""

            new_session_id = str(result.events[0]["sessionId"])
            candidate = self.config.project_dir / f"{new_session_id}.jsonl"
            if candidate.exists():
                raise RebuildError("candidate transcript already exists")

            # 先把候选路径落进耐久状态，之后任何一步失败都能把垃圾清干净。
            with self.state.lock():
                operation = self.state.load(operation_id)
                operation["candidate_path"] = str(candidate)
                self.state.save(operation)

            if snapshot is not None:
                atomic_write_text(
                    operation_dir / "startup_snapshot.txt",
                    event_text(snapshot),
                    0o600,
                )
            atomic_write_text(candidate, dump_jsonl(result.events), 0o600)
            verification = verify_candidate(candidate, result.manifest, new_session_id)
            if not verification["ok"]:
                raise RebuildError("candidate verification failed: " + "; ".join(verification["errors"]))

            dirty_report = evaluate_dirty(
                source_events,
                dirty_budget_bytes=self.config.dirty_budget_bytes,
                rebuild_on_original_image_view=self.config.rebuild_on_original_image_view,
            )

            with self.state.lock():
                operation = self.state.load(operation_id)
                operation.update(
                    {
                        "status": "verifying",
                        "source_sha256": source_digest,
                        "source_session_id": old_session_id,
                        "source_mtime": source_mtime,
                        "cas_pane_pid": pane_pid,
                        "new_session_id": new_session_id,
                        "candidate_path": str(candidate),
                        "candidate_sha256": file_digest(candidate),
                        "backup_path": str(backup),
                        "startup_snapshot_sha256": startup_digest,
                        "manifest": list(result.manifest),
                        "stats": result.stats(),
                        "dirty_at_prepare": dirty_report,
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
                self._discard_candidate(operation)
                self.state.save(operation)
            raise

    def _discard_candidate(self, operation: dict[str, Any]) -> None:
        """扔掉一个从未被切换过的候选文件。

        它的完整来源已经在 artifacts 冷仓里，留在 project_dir 只会变成垃圾。
        已经 respawn 过的候选不删，那是现场证据。
        """
        path = operation.get("candidate_path")
        if not path:
            return
        try:
            target = Path(str(path)).resolve()
            target.relative_to(self.config.project_dir)
        except (OSError, ValueError):
            return
        try:
            target.unlink(missing_ok=True)
            operation["candidate_discarded"] = True
        except OSError:
            pass

    def _assert_cas(self, operation: dict[str, Any]) -> None:
        """切换前核对身份：这个 pane 还是我准备时的那个 pane 吗。

        对不上就说明期间有别的合法机制换过 session，绝不覆盖第三方。
        """
        expected = str(operation.get("cas_pane_pid") or "")
        current = self._pane_identity()
        if expected and current and expected != current:
            raise RebuildError(
                f"session conflict: pane pid changed {expected} -> {current}; refusing to overwrite"
            )
        source_path = operation.get("source_path")
        source_mtime = operation.get("source_mtime")
        if source_path and isinstance(source_mtime, (int, float)):
            own = {Path(str(operation["candidate_path"])).resolve()} if operation.get("candidate_path") else set()
            newer = self._newer_transcripts(Path(source_path), float(source_mtime), ignore=own)
            if newer:
                raise RebuildError(
                    "session conflict: another transcript became active after prepare: " + ", ".join(newer)
                )

    def activate(self, operation_id: str) -> dict[str, Any]:
        with self.state.lock():
            operation = self.state.load(operation_id)
            if operation.get("status") != "verifying" or not operation.get("verification", {}).get("ok"):
                raise RebuildError("activation requires a verified candidate")
            source = self._assert_transcript(operation["source_path"])
            if file_digest(source) != operation["source_sha256"]:
                operation["status"] = "failed"
                operation["error"] = "source transcript changed after prepare; refusing activation"
                self._discard_candidate(operation)
                self.state.save(operation)
                raise RebuildError(operation["error"])
            candidate = self._assert_transcript(operation["candidate_path"])
            if file_digest(candidate) != operation["candidate_sha256"]:
                raise RebuildError("candidate transcript changed after verification")
            try:
                self._assert_cas(operation)
            except RebuildError as exc:
                operation["status"] = "failed"
                operation["error"] = str(exc)
                operation["session_conflict"] = True
                self._discard_candidate(operation)
                self.state.save(operation)
                raise
            operation["status"] = "activating"
            self.state.save(operation)

        # 这一段无论怎么炸，operation 都必须落进终态。
        # 卡在 activating 会让之后每一次 request 都被"已在进行中"挡死。
        try:
            result = self.tmux.respawn(operation["new_session_id"])
            switched = result.ok and self.tmux.wait_healthy()
            failure = "" if switched else (result.stderr or "new session failed tmux health check")
        except Exception as exc:  # noqa: BLE001 - 终态优先于精确分类
            switched, failure = False, f"tmux activation raised: {exc}"

        if not switched:
            try:
                rollback = self.tmux.respawn(operation["source_session_id"])
                rollback_healthy = rollback.ok and self.tmux.wait_healthy()
            except Exception:  # noqa: BLE001
                rollback_healthy = False
            with self.state.lock():
                operation = self.state.load(operation_id)
                operation["status"] = "rolled_back" if rollback_healthy else "failed"
                operation["error"] = failure
                operation["rollback_ok"] = rollback_healthy
                self.state.save(operation)
            raise RebuildError(failure)

        with self.state.lock():
            operation = self.state.load(operation_id)
            operation["status"] = "activated"
            operation["activated_at"] = utc_now()
            operation["tmux_command"] = result.command
            operation["cas_pane_pid_after"] = self._pane_identity()
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
            expected = str(operation.get("cas_pane_pid_after") or "")
            current = self._pane_identity()
            if expected and current and expected != current:
                operation["status"] = "failed"
                operation["error"] = (
                    f"session conflict: pane pid changed {expected} -> {current}; refusing rollback"
                )
                operation["session_conflict"] = True
                self.state.save(operation)
                raise RebuildError(operation["error"])
        try:
            result = self.tmux.respawn(operation["source_session_id"])
            healthy = result.ok and self.tmux.wait_healthy()
            failure = "" if healthy else (result.stderr or "rollback session failed tmux health check")
        except Exception as exc:  # noqa: BLE001 - 终态优先于精确分类
            healthy, failure = False, f"tmux rollback raised: {exc}"
        with self.state.lock():
            operation = self.state.load(operation_id)
            operation["status"] = "rolled_back" if healthy else "failed"
            operation["rollback_ok"] = healthy
            operation["rollback_at"] = utc_now()
            if not healthy:
                operation["error"] = failure
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
        # systemd 承载时 worker 必须逃出 service 的 cgroup，否则它自己发的
        # `systemctl restart` 会连自己一起杀掉，operation 永远卡在 activating。
        if self.config.activation == "systemd":
            import shutil as _shutil

            if _shutil.which("systemd-run"):
                argv = [
                    "systemd-run", "--scope", "--quiet", "--collect",
                    "--unit", f"ktr-worker-{active['operation_id']}",
                    "--property", "KillMode=process",
                ] + argv
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
