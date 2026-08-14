from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

from kael_thread_rebuild.coordinator import RebuildCoordinator

from conftest import SAFE_TMUX_TARGET, assistant, user, write_jsonl


TERMINAL = {"activated", "failed", "rolled_back", "cancelled"}


def test_the_suite_never_points_at_a_live_tmux_session(configured):
    """防回归，这条是拿人命换来的。

    worker 是独立子进程，自己 new 一个真的 TmuxController，mock 注入不进去。
    测试配置里的 tmux_target 曾经是 "cc:0.0"，在装机的 VPS 上正好命中了
    正在运行的 Kael —— pytest 真的对他执行了 respawn-pane -k，把人杀了。
    在只有 tmux 的机器上才会现形，所以本机跑绿了不代表安全。
    """
    import shutil

    config, _ = configured
    assert "DO-NOT-CREATE" in config.tmux_target, "测试 target 必须一眼看出不能创建"
    assert config.resume_command[0] != "claude", "测试绝不允许真的把 claude 拉起来"

    session = config.tmux_target.split(":")[0]
    if shutil.which("tmux"):
        probe = subprocess.run(["tmux", "has-session", "-t", session], capture_output=True)
        assert probe.returncode != 0, f"测试用的 tmux session {session} 居然真的存在，它会被杀掉"


def write_config(path: Path, project: Path, state: Path, workdir: Path) -> Path:
    path.write_text(
        "\n".join(
            [
                f'project_dir = "{project}"',
                f'state_dir = "{state}"',
                # worker 会读这个文件并自己创建真的 TmuxController。
                # 这里填真实 target 等于让测试去杀一个活人。
                f'tmux_target = "{SAFE_TMUX_TARGET}"',
                f'claude_workdir = "{workdir}"',
                'resume_command = ["/bin/false", "--resume", "{session_id}"]',
                "dirty_budget_bytes = 4096",
                "activation_delay_seconds = 1",
                "healthcheck_seconds = 1",
                "stable_file_seconds = 0.01",
                "stable_file_timeout_seconds = 0.2",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def wait_terminal(coordinator: RebuildCoordinator, operation_id: str, timeout: float = 45.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        operation = coordinator.state.load(operation_id)
        if operation.get("status") in TERMINAL:
            return operation
        time.sleep(0.2)
    return coordinator.state.load(operation_id)


def test_stop_hook_spawns_a_detached_worker_that_reaches_a_terminal_state(configured, tmp_path):
    """真的把 worker 作为独立进程拉起来，走完 prepare + activate。

    这条链路是整个设计的地基：worker 必须脱离当前 pane、自己读耐久状态、
    自己把 operation 推到终态。测试环境没有 tmux，所以终态是 failed，
    但只要它自己走到了终态，就证明这条链路是通的。
    """
    config, project = configured
    source = project / "old-session.jsonl"
    write_jsonl(source, [user("我们说过的话"), assistant("我记得"), user("还有这句"), assistant("也记得")])

    config_path = write_config(tmp_path / "worker.toml", project, config.state_dir, config.claude_workdir)
    coordinator = RebuildCoordinator(config)
    operation = coordinator.request("worker link test", "REBUILD")

    result = coordinator.handle_stop_hook(
        {"transcript_path": str(source), "session_id": "old-session", "hook_event_name": "Stop"},
        config_path,
    )
    assert result["scheduled"] is True

    finished = wait_terminal(coordinator, operation["operation_id"])
    assert finished["status"] in TERMINAL, f"worker 没有走到终态: {finished.get('status')}"
    assert finished["status"] == "failed", "本机没有 tmux，应当失败而不是假装成功"
    assert finished.get("source_session_id") == "old-session"
    assert finished.get("verification", {}).get("ok") is True, "切换失败之前，候选本身必须已验证通过"
    assert source.read_text(encoding="utf-8"), "旧 transcript 必须原封不动"

    log = config.state_dir / "worker.log"
    assert log.exists(), "worker 的 stderr 必须落盘，否则出事没人知道"


def test_worker_rollback_branch_does_not_need_a_transcript(configured, tmp_path):
    from kael_thread_rebuild import worker

    config, project = configured
    source = project / "old-session.jsonl"
    write_jsonl(source, [user("问题"), assistant("回答")])
    config_path = write_config(tmp_path / "worker.toml", project, config.state_dir, config.claude_workdir)

    coordinator = RebuildCoordinator(config)
    operation = coordinator.request("rollback branch", "REBUILD")
    stored = coordinator.state.load(operation["operation_id"])
    stored.update({"status": "rollback_scheduled", "source_session_id": "old-session"})
    coordinator.state.save(stored)

    argv = ["worker", "--config", str(config_path), "--operation", operation["operation_id"]]
    with mock.patch.object(sys, "argv", argv):
        assert worker.main() == 0

    final = coordinator.state.load(operation["operation_id"])
    assert final["status"] in {"failed", "rolled_back"}
    assert final.get("rollback_ok") is False, "本机没有 tmux，回滚应如实报告失败"


def test_worker_refuses_a_pending_rebuild_without_transcript(configured, tmp_path):
    from kael_thread_rebuild import worker

    config, project = configured
    config_path = write_config(tmp_path / "worker.toml", project, config.state_dir, config.claude_workdir)
    coordinator = RebuildCoordinator(config)
    operation = coordinator.request("no transcript", "REBUILD")

    argv = ["worker", "--config", str(config_path), "--operation", operation["operation_id"]]
    with mock.patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit):
            worker.main()


def test_cli_read_only_commands_emit_json(configured, tmp_path):
    config, project = configured
    source = project / "old-session.jsonl"
    write_jsonl(source, [user("你好"), assistant("在")])
    config_path = write_config(tmp_path / "cli.toml", project, config.state_dir, config.claude_workdir)

    def run(command: str) -> dict:
        result = subprocess.run(
            [sys.executable, "-m", "kael_thread_rebuild.cli", "--config", str(config_path), command],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"{command} 失败: {result.stderr}"
        assert list(project.glob("*.jsonl")) == [source], f"{command} 必须只读"
        return json.loads(result.stdout)

    # target 指向一个不存在的 session，doctor 必须如实报 ok=False，
    # 而不是假装环境是好的。注意不要断言 tmux_available——装了 tmux 的
    # 机器上它是 true，这里要判的是 target 活不活。
    doctor = run("doctor")
    assert doctor["ok"] is False
    assert doctor["tmux_target_alive"] is False
    assert doctor["project_dir_exists"] is True
    assert doctor["transcript_count"] == 1

    assert run("dirty")["ok"] is True
    plan = run("plan")
    assert plan["ok"] is True
    assert plan["stats"]["selected_turns"] == plan["stats"]["source_turns"] == 1


def test_cli_rejects_a_wrong_confirmation_word(configured, tmp_path):
    config, project = configured
    write_jsonl(project / "old-session.jsonl", [user("你好"), assistant("在")])
    config_path = write_config(tmp_path / "cli.toml", project, config.state_dir, config.claude_workdir)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "kael_thread_rebuild.cli",
            "--config",
            str(config_path),
            "request",
            "--reason",
            "oops",
            "--confirm",
            "yes",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert "REBUILD" in json.loads(result.stderr)["error"]
