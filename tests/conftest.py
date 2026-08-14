from __future__ import annotations

import json
from pathlib import Path

import pytest

from kael_thread_rebuild.config import RebuildConfig


def user(text: str, session: str = "old-session", **extra) -> dict:
    return {
        "type": "user",
        "sessionId": session,
        "uuid": extra.pop("uuid", f"u-{abs(hash(text))}"),
        "parentUuid": None,
        "message": {"role": "user", "content": text},
        **extra,
    }


def assistant(text: str, session: str = "old-session", **extra) -> dict:
    return {
        "type": "assistant",
        "sessionId": session,
        "uuid": extra.pop("uuid", f"a-{abs(hash(text))}"),
        "parentUuid": None,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        **extra,
    }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


@pytest.fixture
def configured(tmp_path: Path) -> tuple[RebuildConfig, Path]:
    project = tmp_path / "project"
    project.mkdir()
    config = RebuildConfig.from_mapping(
        {
            "project_dir": str(project),
            "state_dir": str(tmp_path / "state"),
            "tmux_target": "cc:0.0",
            "claude_workdir": str(tmp_path),
            "resume_command": ["claude", "--resume", "{session_id}"],
            "target_tokens": 5000,
            "tail_turns": 2,
            "max_event_chars": 1000,
            "activation_delay_seconds": 1,
            "healthcheck_seconds": 1,
            "stable_file_seconds": 0.01,
            "stable_file_timeout_seconds": 0.2,
        }
    )
    return config, project

