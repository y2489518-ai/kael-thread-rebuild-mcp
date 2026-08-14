from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from .config import DEFAULT_CONFIG_ENV, RebuildConfig
from .coordinator import RebuildCoordinator, RebuildError


def build_server(config: RebuildConfig) -> MCPServer:
    coordinator = RebuildCoordinator(config)
    server = MCPServer(
        "kael-thread-rebuild",
        instructions=(
            "Kael 的 Claude Code 续窗维护器。先调用 thread_rebuild_doctor 和 "
            "thread_rebuild_plan。只有用户明确确认后才能调用 request；request 不会立即杀死当前会话，"
            "它等待当前 turn 的 Stop hook，再由独立 worker 切换 tmux cc。"
        ),
    )

    @server.tool()
    def thread_rebuild_doctor() -> dict[str, Any]:
        """只读检查 transcript 目录、Claude、tmux cc 和未完成 operation。"""
        return coordinator.doctor()

    @server.tool()
    def thread_rebuild_plan(transcript_path: str = "") -> dict[str, Any]:
        """只读预演续窗；不写 transcript，不切 tmux。通常留空使用配置目录中最新 transcript。"""
        return coordinator.plan(transcript_path or None)

    @server.tool()
    def thread_rebuild_request(reason: str, confirmation: str) -> dict[str, Any]:
        """请求在本轮回答完成后安全续窗。confirmation 必须精确为 REBUILD。"""
        operation = coordinator.request(reason, confirmation)
        return {
            "ok": True,
            "operation": operation,
            "next": "先正常回复用户；Stop hook 会在本轮结束后准备、验证并切换 tmux cc。",
        }

    @server.tool()
    def thread_rebuild_status(operation_id: str = "") -> dict[str, Any]:
        """查看指定或最近一次续窗 operation 的耐久状态。"""
        return coordinator.status(operation_id or None)

    @server.tool()
    def thread_rebuild_cancel(operation_id: str, confirmation: str) -> dict[str, Any]:
        """取消尚未执行的续窗。confirmation 必须精确为 CANCEL。"""
        return {"ok": True, "operation": coordinator.cancel(operation_id, confirmation)}

    @server.tool()
    def thread_rebuild_rollback_request(operation_id: str, confirmation: str) -> dict[str, Any]:
        """请求在本轮结束后回到旧 Claude session。confirmation 必须精确为 ROLLBACK。"""
        return {
            "ok": True,
            "operation": coordinator.request_rollback(operation_id, confirmation),
            "next": "先正常回复用户；Stop hook 会在本轮结束后切回旧 session。",
        }

    return server


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", type=Path, default=os.environ.get(DEFAULT_CONFIG_ENV, "config.toml"))
    args, _ = parser.parse_known_args()
    server = build_server(RebuildConfig.load(args.config))
    server.run()


if __name__ == "__main__":
    main()

