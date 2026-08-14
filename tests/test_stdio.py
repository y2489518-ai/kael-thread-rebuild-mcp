from __future__ import annotations

import asyncio
import json
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
                "dirty_budget_bytes = 4096",
                "carry_max_tokens = 0",
                "activation_delay_seconds = 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    from conftest import assistant, user, write_jsonl

    source = project / "old-session.jsonl"
    write_jsonl(source, [user("我们说过的话"), assistant("我记得")])

    async def exercise() -> set[str]:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "kael_thread_rebuild.mcp_server", "--config", str(config_path)],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()

                # VPS 上的 Kael 只能从这个口进来，所以真的调一遍只读工具
                for name in ("thread_rebuild_doctor", "thread_rebuild_dirty", "thread_rebuild_plan"):
                    called = await session.call_tool(name, {})
                    assert called.is_error is not True, f"{name} 调用失败"
                    payload = json.loads(called.content[0].text)
                    assert isinstance(payload, dict) and payload, f"{name} 返回不可用"
                    if name == "thread_rebuild_plan":
                        assert payload["stats"]["selected_turns"] == payload["stats"]["source_turns"] == 1
                    if name == "thread_rebuild_dirty":
                        assert "noise_ratio" in payload

                # 写操作必须挡住错误确认词
                refused = await session.call_tool(
                    "thread_rebuild_request", {"reason": "oops", "confirmation": "yes"}
                )
                assert refused.is_error is True

                assert list(project.glob("*.jsonl")) == [source], "只读工具不许留下任何文件"
                return {tool.name for tool in result.tools}

    assert asyncio.run(exercise()) == {
        "thread_rebuild_doctor",
        "thread_rebuild_dirty",
        "thread_rebuild_plan",
        "thread_rebuild_request",
        "thread_rebuild_status",
        "thread_rebuild_cancel",
        "thread_rebuild_rollback_request",
    }
