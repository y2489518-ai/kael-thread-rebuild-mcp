from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

from kael_thread_rebuild.coordinator import RebuildCoordinator, RebuildError
from kael_thread_rebuild.tmux import CommandResult

from conftest import assistant, thinking, tool_call, tool_result, user, write_jsonl


@dataclass
class FakeTmux:
    healthy: bool = True
    pid: str = "4242"

    def __post_init__(self):
        self.sessions: list[str] = []

    def available(self):
        return True

    def target_alive(self):
        return True

    def pane_command(self):
        return "claude"

    def pane_pid(self):
        return self.pid

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
    assert prepared["cas_pane_pid"] == "4242"
    assert prepared["stats"]["selected_turns"] == 2
    assert source.exists()
    assert prepared["backup_path"]
    activated = coordinator.activate(operation["operation_id"])
    assert activated["status"] == "activated"
    assert tmux.sessions == [prepared["new_session_id"]]


def test_candidate_keeps_conversation_and_drops_runtime_traces(configured):
    config, project = configured
    source = project / "old-session.jsonl"
    write_jsonl(
        source,
        [
            user("我们说过的话"),
            thinking("内部推理"),
            tool_call("Bash", "rm -rf /tmp/x"),
            tool_result("命令输出"),
            assistant("我记得"),
            user("连发一"),
            user("连发二"),
            assistant("一起回"),
        ],
    )
    coordinator = RebuildCoordinator(config, FakeTmux())
    operation = coordinator.request("test", "REBUILD")
    prepared = coordinator.prepare(operation["operation_id"], source)
    candidate = Path(prepared["candidate_path"]).read_text(encoding="utf-8")
    assert "我们说过的话" in candidate
    assert "连发一" in candidate and "连发二" in candidate
    assert "内部推理" not in candidate
    assert "命令输出" not in candidate
    assert "rm -rf" not in candidate


def test_source_change_blocks_activation(configured):
    config, project = configured
    source = project / "old-session.jsonl"
    write_jsonl(source, [user("问题"), assistant("回答")])
    tmux = FakeTmux()
    coordinator = RebuildCoordinator(config, tmux)
    operation = coordinator.request("test", "REBUILD")
    coordinator.prepare(operation["operation_id"], source)
    with source.open("a", encoding="utf-8") as stream:
        stream.write("{}\n")
    with pytest.raises(RebuildError, match="source transcript changed"):
        coordinator.activate(operation["operation_id"])
    assert tmux.sessions == []


def test_pane_change_blocks_activation_cas(configured):
    """准备时的 pane 已经不是现在这个了，说明有人换过 session,绝不覆盖。"""
    config, project = configured
    source = project / "old-session.jsonl"
    write_jsonl(source, [user("问题"), assistant("回答")])
    tmux = FakeTmux()
    coordinator = RebuildCoordinator(config, tmux)
    operation = coordinator.request("test", "REBUILD")
    coordinator.prepare(operation["operation_id"], source)
    tmux.pid = "9999"
    with pytest.raises(RebuildError, match="session conflict"):
        coordinator.activate(operation["operation_id"])
    assert tmux.sessions == []
    stored = coordinator.status(operation["operation_id"])["operation"]
    assert stored["status"] == "failed"
    assert stored["session_conflict"] is True


def test_own_candidate_is_never_mistaken_for_a_third_party(configured):
    """候选文件天生比 source 新，不排除的话每次续窗都会自判冲突，永远激活不了。"""
    config, project = configured
    source = project / "old-session.jsonl"
    write_jsonl(source, [user("问题"), assistant("回答")])
    # 让 source 看起来是好几秒前写完的，复现真实 Stop hook 的时序
    stale = time.time() - 30
    os.utime(source, (stale, stale))

    tmux = FakeTmux()
    coordinator = RebuildCoordinator(config, tmux)
    operation = coordinator.request("test", "REBUILD")
    prepared = coordinator.prepare(operation["operation_id"], source)
    assert Path(prepared["candidate_path"]).stat().st_mtime > stale + 1.0

    activated = coordinator.activate(operation["operation_id"])
    assert activated["status"] == "activated"
    assert tmux.sessions == [prepared["new_session_id"]]


def test_orphan_candidate_is_removed_and_never_looks_like_the_latest_session(configured):
    config, project = configured
    source = project / "old-session.jsonl"
    write_jsonl(source, [user("问题"), assistant("回答")])
    tmux = FakeTmux()
    coordinator = RebuildCoordinator(config, tmux)
    operation = coordinator.request("test", "REBUILD")
    prepared = coordinator.prepare(operation["operation_id"], source)
    candidate = Path(prepared["candidate_path"])
    assert candidate.exists()

    tmux.pid = "9999"
    with pytest.raises(RebuildError, match="session conflict"):
        coordinator.activate(operation["operation_id"])

    assert not candidate.exists(), "从未切换过的候选必须清掉，不能留成垃圾"
    assert coordinator.latest_transcript().resolve() == source.resolve()


def test_third_party_transcript_blocks_activation(configured):
    """期间冒出另一段活跃 transcript,也算第三方改过 session。"""
    config, project = configured
    source = project / "old-session.jsonl"
    write_jsonl(source, [user("问题"), assistant("回答")])
    coordinator = RebuildCoordinator(config, FakeTmux())
    operation = coordinator.request("test", "REBUILD")
    prepared = coordinator.prepare(operation["operation_id"], source)

    other = project / "another-session.jsonl"
    write_jsonl(other, [user("别处开的新会话"), assistant("在")])
    future = time.time() + 60
    os.utime(other, (future, future))

    with pytest.raises(RebuildError, match="another transcript became active"):
        coordinator.activate(operation["operation_id"])
    assert coordinator.status(operation["operation_id"])["operation"]["session_conflict"] is True
    assert not Path(prepared["candidate_path"]).exists()
    assert source.exists(), "旧 transcript 永远不动"


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


def test_rollback_refuses_when_pane_changed(configured):
    config, project = configured
    source = project / "old-session.jsonl"
    write_jsonl(source, [user("问题"), assistant("回答")])
    tmux = FakeTmux()
    coordinator = RebuildCoordinator(config, tmux)
    operation = coordinator.request("test", "REBUILD")
    coordinator.prepare(operation["operation_id"], source)
    coordinator.activate(operation["operation_id"])
    coordinator.request_rollback(operation["operation_id"], "ROLLBACK")
    tmux.pid = "1000"
    with pytest.raises(RebuildError, match="session conflict"):
        coordinator.perform_rollback(operation["operation_id"])
    assert tmux.sessions == [coordinator.status(operation["operation_id"])["operation"]["new_session_id"]]


def test_tmux_explosion_still_lands_in_a_terminal_state(configured):
    """tmux 抛异常时不能把 operation 卡在 activating，否则之后每次 request 都被挡死。"""
    config, project = configured
    source = project / "old-session.jsonl"
    write_jsonl(source, [user("问题"), assistant("回答")])

    class ExplodingTmux(FakeTmux):
        def respawn(self, session_id):
            raise FileNotFoundError("tmux")

    coordinator = RebuildCoordinator(config, ExplodingTmux())
    operation = coordinator.request("test", "REBUILD")
    coordinator.prepare(operation["operation_id"], source)
    with pytest.raises(RebuildError, match="tmux activation raised"):
        coordinator.activate(operation["operation_id"])

    stored = coordinator.status(operation["operation_id"])["operation"]
    assert stored["status"] == "failed"
    assert stored["rollback_ok"] is False
    # 终态之后必须还能重新登记，不需要人工去删状态文件
    fresh = coordinator.request("retry", "REBUILD")
    assert fresh["operation_id"] != operation["operation_id"]


def test_missing_tmux_binary_reports_failure_instead_of_raising(configured):
    from kael_thread_rebuild.tmux import TmuxController

    config, _ = configured
    controller = TmuxController(config)
    with mock.patch("kael_thread_rebuild.tmux.subprocess.run", side_effect=FileNotFoundError("tmux")):
        result = controller.respawn("11111111-1111-1111-1111-111111111111")
        assert result.ok is False
        assert "not found" in result.stderr
        assert controller.pane_pid() == ""
        assert controller.target_alive() is False


def test_mismatched_workdir_is_refused_before_anything_is_written(configured, tmp_path):
    """cwd 和 project_dir 对不上时，切换后 resume 必然找不到 session,
    而那时 pane 已经被杀。必须在写任何东西之前就拒绝。"""
    from kael_thread_rebuild.config import RebuildConfig, encode_project_dirname

    assert encode_project_dirname(Path("/root")) == "-root"
    assert encode_project_dirname(Path("/")) == "-"

    config, project = configured
    source = project / "old-session.jsonl"
    write_jsonl(source, [user("问题"), assistant("回答")])

    # project_dir 的目录名跟 claude_workdir 编不出来
    bad = RebuildConfig.from_mapping(
        {
            "project_dir": str(project),
            "state_dir": str(config.state_dir),
            "claude_workdir": "/root",
            "resume_command": ["claude", "--resume", "{session_id}"],
            "activation_delay_seconds": 1,
            "stable_file_seconds": 0.01,
            "stable_file_timeout_seconds": 0.2,
        }
    )
    assert bad.workdir_matches_project() is False

    coordinator = RebuildCoordinator(bad, FakeTmux())
    assert coordinator.doctor()["workdir_matches_project"] is False
    assert coordinator.doctor()["expected_project_dirname"] == "-root"

    operation = coordinator.request("mismatch", "REBUILD")
    with pytest.raises(RebuildError, match="对不上"):
        coordinator.prepare(operation["operation_id"], source)
    assert list(project.glob("*.jsonl")) == [source], "拒绝之前不许留下任何候选"


def test_matching_workdir_passes_the_check(tmp_path):
    from kael_thread_rebuild.config import RebuildConfig

    project = tmp_path / "-root"
    project.mkdir()
    config = RebuildConfig.from_mapping(
        {
            "project_dir": str(project),
            "state_dir": str(tmp_path / "state"),
            "claude_workdir": "/root",
            "resume_command": ["claude", "--resume", "{session_id}"],
            "activation_delay_seconds": 1,
        }
    )
    assert config.workdir_matches_project() is True


def test_dirty_report_is_read_only(configured):
    config, project = configured
    source = project / "old-session.jsonl"
    write_jsonl(source, [user("你好"), tool_result("x" * 9000), assistant("在")])
    coordinator = RebuildCoordinator(config, FakeTmux())
    report = coordinator.dirty(source)
    assert report["should_rebuild"] is True
    assert report["categories"]["tool_result"] > 8000
    assert report["conversation_turns"] == 1
    assert coordinator.state.latest() is None


def test_plan_reports_dirty_and_changes_nothing(configured):
    config, project = configured
    source = project / "old-session.jsonl"
    write_jsonl(source, [user("问题"), assistant("回答")])
    before = source.read_text(encoding="utf-8")
    coordinator = RebuildCoordinator(config, FakeTmux())
    plan = coordinator.plan(source)
    assert plan["ok"] is True
    assert plan["stats"]["selected_turns"] == 1
    assert "noise_ratio" in plan["dirty"]
    assert source.read_text(encoding="utf-8") == before
    assert list(project.glob("*.jsonl")) == [source]


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
