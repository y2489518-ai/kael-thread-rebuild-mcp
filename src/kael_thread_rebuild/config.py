from __future__ import annotations

import os
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_ENV = "KAEL_REBUILD_CONFIG"
DEFAULT_DIRTY_BUDGET = 512 * 1024


@dataclass(frozen=True)
class RebuildConfig:
    project_dir: Path
    state_dir: Path
    tmux_target: str = "cc:0.0"
    claude_workdir: Path = Path("/root")
    resume_command: tuple[str, ...] = ("claude", "--resume", "{session_id}")
    # 脏预算：只统计工具回包、thinking、图片、注入块这类运行负担。
    dirty_budget_bytes: int = DEFAULT_DIRTY_BUDGET
    rebuild_on_original_image_view: bool = True
    # 防炸硬上限，0 表示不限。正常路径不该靠它裁剪对话。
    carry_max_tokens: int = 0
    # 单条消息截断上限，0 表示不截断。
    max_event_chars: int = 0
    include_open_tail: bool = True
    freeze_startup_snapshot: bool = True
    stamp_turns: bool = True
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
            dirty_budget_bytes=int(raw.get("dirty_budget_bytes", DEFAULT_DIRTY_BUDGET)),
            rebuild_on_original_image_view=bool(raw.get("rebuild_on_original_image_view", True)),
            carry_max_tokens=int(raw.get("carry_max_tokens", 0)),
            max_event_chars=int(raw.get("max_event_chars", 0)),
            include_open_tail=bool(raw.get("include_open_tail", True)),
            freeze_startup_snapshot=bool(raw.get("freeze_startup_snapshot", True)),
            stamp_turns=bool(raw.get("stamp_turns", True)),
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
        if self.dirty_budget_bytes < 0:
            raise ValueError("dirty_budget_bytes must not be negative")
        if self.carry_max_tokens < 0:
            raise ValueError("carry_max_tokens must not be negative")
        if 0 < self.carry_max_tokens < 5_000:
            raise ValueError("carry_max_tokens must be 0 (unlimited) or at least 5000")
        if self.max_event_chars < 0:
            raise ValueError("max_event_chars must not be negative")
        if 0 < self.max_event_chars < 500:
            raise ValueError("max_event_chars must be 0 (no truncation) or at least 500")
        if self.activation_delay_seconds < 1:
            raise ValueError("activation_delay_seconds must be at least 1")

    def resume_argv(self, session_id: str) -> list[str]:
        if not session_id or any(char not in "0123456789abcdef-" for char in session_id.lower()):
            raise ValueError("invalid Claude session id")
        return [session_id if item == "{session_id}" else item for item in self.resume_command]
