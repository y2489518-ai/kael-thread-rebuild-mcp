from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

from kael_thread_rebuild.coordinator import RebuildCoordinator, RebuildError
from kael_thread_rebuild.tmux import CommandResult

from conftest import assistant, user, write_jsonl


@dataclass
class FakeTmux:
    healthy: bool = True

    def __post_init__(self):
        self.sessions: list[str] = []

    def available(self):
        return True

    def target_alive(self):
        return True

    def pane_command(self):
        return "claude"

    def respawn(self, session_id):
        self.sessions.append(session_id)
        return CommandResult(True, ["fake", session_id])

    def wait_healthy(self):
        return self.healthy


def test_request_requires_explicit_confirmation(configured):
    config, _ = configured
    coordinator = RebuildCoordinator(config, FakeTmux())
    with pytest.raises(RebuildError, match="REBUILD"):
        coordinator.request("test", "yes")


def test_pending_requests_coalesce(configured):
    config, _ = configured
    coordinator = RebuildCoordinator(config, FakeTmux())
    first = coordinator.request("one", "REBUILD")
    second = coordinator.request("two", "REBUILD")
    assert first["operation_id"] == second["operation_id"]
    assert second["reasons"] == ["one", "two"]


def test_prepare_verify_activate(configured):
    config, project = configured
    source = project / "old-session.jsonl"
    write_jsonl(source, [user("记得边界"), assistant("会记得"), user("当前任务"), assistant("下一步测试")])
    tmux = FakeTmux()
    coordinator = RebuildCoordinator(config, tmux)
    operation = coordinator.request("test", "REBUILD")
    prepared = coordinator.prepare(operation["operation_id"], source)
    assert prepared["status"] == "verifying"
    assert prepared["verification"]["ok"] is True
    assert source.exists()
    assert prepared["backup_path"]
    activated = coordinator.activate(operation["operation_id"])
    assert activated["status"] == "activated"
    assert tmux.sessions == [prepared["new_session_id"]]


def test_source_change_blocks_activation(configured):
    config, project = configured
    source = project / "old-session.jsonl"
    write_jsonl(source, [user("问题"), assistant("回答")])
    tmux = FakeTmux()
    coordinator = RebuildCoordinator(config, tmux)
    operation = coordinator.request("test", "REBUILD")
    prepared = coordinator.prepare(operation["operation_id"], source)
    with source.open("a", encoding="utf-8") as stream:
        stream.write("{}\n")
    with pytest.raises(RebuildError, match="source transcript changed"):
        coordinator.activate(operation["operation_id"])
    assert tmux.sessions == []


def test_failed_new_session_rolls_back_old(configured):
    config, project = configured
    source = project / "old-session.jsonl"
    write_jsonl(source, [user("问题"), assistant("回答")])

    class FailFirstTmux(FakeTmux):
        def wait_healthy(self):
            return len(self.sessions) > 1

    tmux = FailFirstTmux()
    coordinator = RebuildCoordinator(config, tmux)
    operation = coordinator.request("test", "REBUILD")
    prepared = coordinator.prepare(operation["operation_id"], source)
    with pytest.raises(RebuildError):
        coordinator.activate(operation["operation_id"])
    assert tmux.sessions == [prepared["new_session_id"], "old-session"]
    assert coordinator.status(operation["operation_id"])["operation"]["status"] == "rolled_back"


def test_stop_hook_schedules_only_one_worker(configured):
    config, project = configured
    source = project / "old-session.jsonl"
    write_jsonl(source, [user("问题"), assistant("回答")])
    coordinator = RebuildCoordinator(config, FakeTmux())
    operation = coordinator.request("test", "REBUILD")
    payload = {"transcript_path": str(source), "session_id": "old-session", "hook_event_name": "Stop"}
    with mock.patch("kael_thread_rebuild.coordinator.subprocess.Popen") as popen:
        first = coordinator.handle_stop_hook(payload, Path("/tmp/config.toml"))
        second = coordinator.handle_stop_hook(payload, Path("/tmp/config.toml"))
    assert first["scheduled"] is True
    assert second["scheduled"] is False
    assert popen.call_count == 1
    assert coordinator.status(operation["operation_id"])["operation"]["status"] == "scheduled"
