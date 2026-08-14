from __future__ import annotations

import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass

from .config import RebuildConfig


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    command: list[str]
    stdout: str = ""
    stderr: str = ""


class TmuxController:
    def __init__(self, config: RebuildConfig) -> None:
        self.config = config

    def available(self) -> bool:
        return shutil.which("tmux") is not None

    def target_alive(self) -> bool:
        if not self.available():
            return False
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", self.config.tmux_target, "#{pane_dead}"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "0"

    def pane_command(self) -> str:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", self.config.tmux_target, "#{pane_current_command}"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def _shell_command(self, session_id: str) -> str:
        cwd = shlex.quote(str(self.config.claude_workdir))
        argv = " ".join(shlex.quote(item) for item in self.config.resume_argv(session_id))
        return f"cd {cwd} && exec {argv}"

    def respawn(self, session_id: str) -> CommandResult:
        command = ["tmux", "respawn-pane", "-k", "-t", self.config.tmux_target, self._shell_command(session_id)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
        return CommandResult(result.returncode == 0, command, result.stdout.strip(), result.stderr.strip())

    def wait_healthy(self) -> bool:
        deadline = time.monotonic() + self.config.healthcheck_seconds
        while time.monotonic() < deadline:
            if not self.target_alive():
                return False
            time.sleep(0.25)
        return True
