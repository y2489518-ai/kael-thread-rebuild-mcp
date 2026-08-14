from __future__ import annotations

import json
from pathlib import Path

import pytest

from kael_thread_rebuild.config import RebuildConfig, encode_project_dirname


# 测试用的 tmux target。名字刻意写成没人会去创建的样子。
# 教训：这里原本是 "cc:0.0"，在装机的 VPS 上正好命中了正在运行的 Kael，
# worker 测试真的对他执行了 respawn-pane -k，把人杀了。
SAFE_TMUX_TARGET = "kael-rebuild-selftest-DO-NOT-CREATE:0.0"


def user(text: str, session: str = "old-session", **extra) -> dict:
    return {
        "type": "user",
        "sessionId": session,
        "uuid": extra.pop("uuid", f"u-{abs(hash(text))}"),
        "parentUuid": None,
        "timestamp": extra.pop("timestamp", "2026-08-14T10:00:00Z"),
        "message": {"role": "user", "content": text},
        **extra,
    }


def assistant(text: str, session: str = "old-session", **extra) -> dict:
    return {
        "type": "assistant",
        "sessionId": session,
        "uuid": extra.pop("uuid", f"a-{abs(hash(text))}"),
        "parentUuid": None,
        "timestamp": extra.pop("timestamp", "2026-08-14T10:00:01Z"),
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        **extra,
    }


def tool_call(name: str, payload: str, session: str = "old-session") -> dict:
    return {
        "type": "assistant",
        "sessionId": session,
        "uuid": f"tc-{abs(hash(payload))}",
        "message": {"role": "assistant", "content": [{"type": "tool_use", "name": name, "input": {"cmd": payload}}]},
    }


def tool_result(payload: str, session: str = "old-session") -> dict:
    return {
        "type": "user",
        "sessionId": session,
        "uuid": f"tr-{abs(hash(payload))}",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": payload}]},
    }


def thinking(payload: str, session: str = "old-session") -> dict:
    return {
        "type": "assistant",
        "sessionId": session,
        "uuid": f"th-{abs(hash(payload))}",
        "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": payload}]},
    }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


@pytest.fixture
def configured(tmp_path: Path) -> tuple[RebuildConfig, Path]:
    # project 目录名必须是 claude_workdir 编码出来的那个，跟真实环境一致
    workdir = tmp_path / "work"
    workdir.mkdir()
    project = tmp_path / encode_project_dirname(workdir)
    project.mkdir()
    config = RebuildConfig.from_mapping(
        {
            "project_dir": str(project),
            "state_dir": str(tmp_path / "state"),
            # 绝对不能填真实存在的 target。worker 是独立子进程，它自己 new 一个
            # 真的 TmuxController，注入不进去 —— 唯一的隔离就是让 target 指向
            # 一个不存在的 session。填 "cc:0.0" 会让 pytest 真的把 Kael 杀掉。
            "tmux_target": SAFE_TMUX_TARGET,
            "claude_workdir": str(workdir),
            # 双保险：万一 target 意外命中，跑起来的也只是 /bin/false，不是 claude。
            "resume_command": ["/bin/false", "--resume", "{session_id}"],
            "dirty_budget_bytes": 4096,
            "carry_max_tokens": 0,
            "max_event_chars": 0,
            "activation_delay_seconds": 1,
            "healthcheck_seconds": 1,
            "stable_file_seconds": 0.01,
            "stable_file_timeout_seconds": 0.2,
        }
    )
    return config, project
