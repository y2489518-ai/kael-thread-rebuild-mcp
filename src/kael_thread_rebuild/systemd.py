from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from .config import RebuildConfig
from .tmux import CommandResult


class SystemdController:
    """TmuxController 的 systemd 版：pane == 一个 systemd service。

    咱家（Darcy/琢）的 cc 不跑在 tmux 里，而是 systemd 直接拉起
    `zhuo-claude`，pty 由 `script -qfc` 提供。所以「换窗」在这里的语义是：
      1. 把目标 session_id 写进指针文件（zhuo-claude 启动时优先读它）
      2. `systemctl restart <unit>`
    身份凭据（对应 tmux 的 pane_pid）用 MainPID：prepare 时记下，激活前
    再核一次，中间被别人重启过就拒绝覆盖。

    和 TmuxController 一样：所有调用都不向上抛异常，失败一律返回
    CommandResult(ok=False)，让上层走回滚路径，别把 operation 卡死在
    activating。
    """

    def __init__(self, config: RebuildConfig) -> None:
        self.config = config
        self.unit = config.systemd_unit
        self.pointer = Path(config.resume_pointer_path)

    # ---------- 基础 ----------

    def available(self) -> bool:
        return shutil.which("systemctl") is not None

    def _run(self, command: list[str], timeout: float) -> CommandResult:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        except FileNotFoundError:
            return CommandResult(False, command, "", "systemctl binary not found")
        except subprocess.TimeoutExpired:
            return CommandResult(False, command, "", f"systemctl command timed out after {timeout}s")
        except OSError as exc:
            return CommandResult(False, command, "", f"systemctl command failed: {exc}")
        return CommandResult(result.returncode == 0, command, result.stdout.strip(), result.stderr.strip())

    def _show(self, prop: str) -> str:
        result = self._run(["systemctl", "show", "-p", prop, "--value", self.unit], 5)
        return result.stdout if result.ok else ""

    # ---------- 与 TmuxController 同名接口 ----------

    def target_alive(self) -> bool:
        return self._run(["systemctl", "is-active", "--quiet", self.unit], 5).ok

    def pane_pid(self) -> str:
        pid = self._show("MainPID")
        return "" if pid in ("", "0") else pid

    def pane_command(self) -> str:
        pid = self.pane_pid()
        if not pid:
            return ""
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return ""
        return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()

    def _write_pointer(self, session_id: str) -> CommandResult:
        command = ["write-pointer", str(self.pointer), session_id]
        try:
            self.pointer.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.pointer.with_suffix(self.pointer.suffix + ".tmp")
            tmp.write_text(session_id + "\n", encoding="utf-8")
            os.replace(tmp, self.pointer)
        except OSError as exc:
            return CommandResult(False, command, "", f"cannot write resume pointer: {exc}")
        return CommandResult(True, command)

    def respawn(self, session_id: str) -> CommandResult:
        """写指针 → restart。restart 是同步的，会等 ExecStop（archive_core）跑完。"""
        # 先用 resume_argv 校验 session_id 合法（和 tmux 版同一道闸）。
        try:
            self.config.resume_argv(session_id)
        except ValueError as exc:
            return CommandResult(False, ["respawn", session_id], "", str(exc))
        written = self._write_pointer(session_id)
        if not written.ok:
            return written
        result = self._run(["systemctl", "restart", self.unit], self.config.systemd_restart_timeout_seconds)
        if not result.ok:
            return result
        # 起来之后再确认新进程的命令行里确实带着这个 session_id，
        # 否则说明 zhuo-claude 没读指针（或读了别的），这不算成功。
        deadline = time.monotonic() + self.config.healthcheck_seconds
        while time.monotonic() < deadline:
            if session_id in self.pane_command():
                return CommandResult(True, result.command, result.stdout, result.stderr)
            time.sleep(0.25)
        return CommandResult(
            False,
            result.command,
            result.stdout,
            f"service restarted but main process cmdline does not contain {session_id}: {self.pane_command()!r}",
        )

    def wait_healthy(self) -> bool:
        deadline = time.monotonic() + self.config.healthcheck_seconds
        while time.monotonic() < deadline:
            if not self.target_alive():
                return False
            time.sleep(0.25)
        return True


class PointerController(SystemdController):
    """`claude -p` 循环家的激活方式：只写指针，不杀任何进程。

    有的家没有常驻 claude——runner 循环调 `claude -p --resume <id>`，每次
    调用都是一次性的进程。这种架构里"换窗"不需要 respawn 任何东西：把洗
    好的新 session_id 原子写进指针文件，runner 下一圈自己带上它就完成了。
    worker 的触发也不走 Stop hook，由 runner 在两次调用之间跑
    `kael-thread-rebuild hook-stop`（喂同样的 JSON）即可。

    身份凭据（对应 pane_pid 的 CAS）用指针文件当时的内容：prepare 记下、
    activate 前再核，中间被别人改过指针就拒绝——和 tmux/systemd 版同一个
    语义：证明"现场还是我准备时那个现场"。activate 与 runner 的下一次
    调用之间天然存在缝，靠已有的"活跃 transcript 冲突"检查兜底。
    """

    def target_alive(self) -> bool:
        # 没有常驻进程可查活；指针目录可写就算现场存在。
        return self.pointer.parent.exists() or not self.pointer.is_absolute()

    def pane_pid(self) -> str:
        try:
            return self.pointer.read_text(encoding="utf-8").strip() or "pointer:empty"
        except OSError:
            return "pointer:absent"

    def pane_command(self) -> str:
        return f"pointer:{self.pointer}"

    def respawn(self, session_id: str) -> CommandResult:
        try:
            self.config.resume_argv(session_id)
        except ValueError as exc:
            return CommandResult(False, ["respawn", session_id], "", str(exc))
        return self._write_pointer(session_id)

    def wait_healthy(self) -> bool:
        try:
            return bool(self.pointer.read_text(encoding="utf-8").strip())
        except OSError:
            return False


def make_controller(config: RebuildConfig):
    """按 config.activation 选实现。默认 tmux，保持上游行为不变。"""
    if config.activation == "systemd":
        return SystemdController(config)
    if config.activation == "pointer":
        return PointerController(config)
    from .tmux import TmuxController

    return TmuxController(config)
