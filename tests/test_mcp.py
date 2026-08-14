from __future__ import annotations

import asyncio

from kael_thread_rebuild.mcp_server import build_server


def test_mcp_exposes_only_expected_tools(configured):
    config, _ = configured
    server = build_server(config)
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == {
        "thread_rebuild_doctor",
        "thread_rebuild_plan",
        "thread_rebuild_request",
        "thread_rebuild_status",
        "thread_rebuild_cancel",
        "thread_rebuild_rollback_request",
    }
