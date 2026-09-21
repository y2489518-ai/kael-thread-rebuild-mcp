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
    """所有 tmux 调用都不向上抛异常。

    这里抛一个 FileNotFoundError 或 TimeoutExpired，operation 就会永久卡在
    activating —— 之后每一次 request 都被"已在进行中"挡死，人得手工去删状态
    文件才能恢复。宁可返回失败让上层走回滚路径。
    """

    def __init__(self, config: RebuildConfig) -> None:
        self.config = config

    def available(self) -> bool:
        return shutil.which("tmux") is not None

    def _run(self, command: list[str], timeout: float) -> CommandResult:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        except FileNotFoundError:
            return CommandResult(False, command, "", "tmux binary not found")
        except subprocess.TimeoutExpired:
            return CommandResult(False, command, "", f"tmux command timed out after {timeout}s")
        except OSError as exc:
            return CommandResult(False, command, "", f"tmux command failed: {exc}")
        return CommandResult(result.returncode == 0, command, result.stdout.strip(), result.stderr.strip())

    def _display(self, spec: str) -> str:
        result = self._run(["tmux", "display-message", "-p", "-t", self.config.tmux_target, spec], 3)
        return result.stdout if result.ok else ""

    def target_alive(self) -> bool:
        return self._display("#{pane_dead}") == "0"

    def pane_command(self) -> str:
        return self._display("#{pane_current_command}")

    def pane_pid(self) -> str:
        """pane 里当前 shell 的 pid。

        这是 tmux 环境下的 session 身份凭据：prepare 时记下，激活前再核一次。
        对不上就说明这个 pane 已经被别人换过，绝不覆盖。
        """
        return self._display("#{pane_pid}")

    def _shell_command(self, session_id: str) -> str:
        cwd = shlex.quote(str(self.config.claude_workdir))
        argv = " ".join(shlex.quote(item) for item in self.config.resume_argv(session_id))
        return f"cd {cwd} && exec {argv}"

    def respawn(self, session_id: str) -> CommandResult:
        return self._run(
            ["tmux", "respawn-pane", "-k", "-t", self.config.tmux_target, self._shell_command(session_id)],
            10,
        )

    def wait_healthy(self) -> bool:
        deadline = time.monotonic() + self.config.healthcheck_seconds
        while time.monotonic() < deadline:
            if not self.target_alive():
                return False
            time.sleep(0.25)
        return True
