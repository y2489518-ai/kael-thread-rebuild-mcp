from __future__ import annotations

import os
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_ENV = "KAEL_REBUILD_CONFIG"


@dataclass(frozen=True)
class RebuildConfig:
    project_dir: Path
    state_dir: Path
    tmux_target: str = "cc:0.0"
    claude_workdir: Path = Path("/root")
    resume_command: tuple[str, ...] = ("claude", "--resume", "{session_id}")
    target_tokens: int = 50_000
    tail_turns: int = 14
    max_event_chars: int = 3_600
    activation_delay_seconds: float = 3.0
    healthcheck_seconds: float = 5.0
    stable_file_seconds: float = 1.0
    stable_file_timeout_seconds: float = 10.0

    @classmethod
    def load(cls, path: str | Path | None = None) -> "RebuildConfig":
        selected = Path(path or os.environ.get(DEFAULT_CONFIG_ENV, "config.toml")).expanduser()
        with selected.open("rb") as stream:
            raw = tomllib.load(stream)
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "RebuildConfig":
        resume = raw.get("resume_command", ["claude", "--resume", "{session_id}"])
        if isinstance(resume, str):
            resume = shlex.split(resume)
        if not isinstance(resume, list) or not all(isinstance(item, str) for item in resume):
            raise ValueError("resume_command must be a string or list of strings")
        if "{session_id}" not in resume:
            raise ValueError("resume_command must contain a standalone {session_id} argument")

        config = cls(
            project_dir=Path(str(raw["project_dir"])).expanduser().resolve(),
            state_dir=Path(str(raw.get("state_dir", "~/.local/state/kael-thread-rebuild"))).expanduser().resolve(),
            tmux_target=str(raw.get("tmux_target", "cc:0.0")).strip(),
            claude_workdir=Path(str(raw.get("claude_workdir", "/root"))).expanduser().resolve(),
            resume_command=tuple(resume),
            target_tokens=int(raw.get("target_tokens", 50_000)),
            tail_turns=int(raw.get("tail_turns", 14)),
            max_event_chars=int(raw.get("max_event_chars", 3_600)),
            activation_delay_seconds=float(raw.get("activation_delay_seconds", 3.0)),
            healthcheck_seconds=float(raw.get("healthcheck_seconds", 5.0)),
            stable_file_seconds=float(raw.get("stable_file_seconds", 1.0)),
            stable_file_timeout_seconds=float(raw.get("stable_file_timeout_seconds", 10.0)),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.tmux_target or any(char in self.tmux_target for char in "\n\r\0"):
            raise ValueError("invalid tmux_target")
        if self.target_tokens < 5_000:
            raise ValueError("target_tokens must be at least 5000")
        if not 2 <= self.tail_turns <= 100:
            raise ValueError("tail_turns must be between 2 and 100")
        if self.max_event_chars < 500:
            raise ValueError("max_event_chars must be at least 500")
        if self.activation_delay_seconds < 1:
            raise ValueError("activation_delay_seconds must be at least 1")

    def resume_argv(self, session_id: str) -> list[str]:
        if not session_id or any(char not in "0123456789abcdef-" for char in session_id.lower()):
            raise ValueError("invalid Claude session id")
        return [session_id if item == "{session_id}" else item for item in self.resume_command]

