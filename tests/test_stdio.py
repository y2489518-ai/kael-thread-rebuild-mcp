from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_real_stdio_handshake_lists_tools(configured, tmp_path: Path):
    config, project = configured
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                f'project_dir = "{project}"',
                f'state_dir = "{config.state_dir}"',
                'tmux_target = "cc:0.0"',
                f'claude_workdir = "{tmp_path}"',
                'resume_command = ["claude", "--resume", "{session_id}"]',
                "target_tokens = 5000",
                "tail_turns = 2",
                "activation_delay_seconds = 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    async def exercise() -> set[str]:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "kael_thread_rebuild.mcp_server", "--config", str(config_path)],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return {tool.name for tool in result.tools}

    assert asyncio.run(exercise()) == {
        "thread_rebuild_doctor",
        "thread_rebuild_plan",
        "thread_rebuild_request",
        "thread_rebuild_status",
        "thread_rebuild_cancel",
        "thread_rebuild_rollback_request",
    }
