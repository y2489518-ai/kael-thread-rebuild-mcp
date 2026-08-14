from __future__ import annotations

import json
from pathlib import Path

import pytest

from kael_thread_rebuild.config import RebuildConfig, encode_project_dirname


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
            "tmux_target": "cc:0.0",
            "claude_workdir": str(workdir),
            "resume_command": ["claude", "--resume", "{session_id}"],
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
