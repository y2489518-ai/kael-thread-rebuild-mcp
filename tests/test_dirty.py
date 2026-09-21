from __future__ import annotations

from kael_thread_rebuild.dirty import evaluate, measure

from conftest import assistant, thinking, tool_call, tool_result, user


def test_ledger_counts_runtime_load_not_conversation():
    rows = [
        user("短问题"),
        thinking("很长的内部推理" * 200),
        tool_call("Bash", "cat /root/big.log " + "x" * 2000),
        tool_result("y" * 5000),
        assistant("短回答"),
    ]
    ledger = measure(rows)
    assert ledger.conversation_turns == 1
    assert ledger.conversation_bytes < 200
    assert ledger.total_bytes > 10_000
    assert set(ledger.categories) >= {"thinking", "tool_use", "tool_result"}
    assert ledger.as_dict()["noise_ratio"] > 0.9


def test_injected_blocks_count_as_runtime_load_not_as_her_words():
    rows = [user("<system-reminder>" + "z" * 3000 + "</system-reminder>\n真话"), assistant("回答")]
    ledger = measure(rows)
    assert ledger.categories.get("injected_block", 0) > 3000
    # 只有真话和回答算对话，注入块一个字节都不算
    assert ledger.conversation_bytes == len("真话回答".encode("utf-8"))


def test_meta_skill_injection_counts_as_runtime_load():
    """skill 正文能一条顶几十万字符，漏算会让脏预算严重低估。"""
    rows = [
        user("Base directory for this skill: /tmp/x\n" + "指令正文" * 5000, isMeta=True),
        user("真话"),
        assistant("回答"),
    ]
    ledger = measure(rows)
    assert ledger.categories.get("meta_injection", 0) > 50_000
    assert ledger.conversation_bytes == len("真话回答".encode("utf-8"))
    assert ledger.conversation_turns == 1


def test_budget_triggers_rebuild():
    clean = evaluate([user("你好"), assistant("在")], dirty_budget_bytes=4096)
    assert clean["should_rebuild"] is False
    assert clean["reasons"] == []

    noisy = evaluate(
        [user("你好"), tool_result("x" * 9000), assistant("在")],
        dirty_budget_bytes=4096,
    )
    assert noisy["should_rebuild"] is True
    assert "dirty budget reached" in noisy["reasons"][0]


def test_original_image_view_triggers_rebuild():
    rows = [
        user("看看这张图"),
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "image", "source": {"data": "b" * 500}}]},
        },
        assistant("看到了"),
    ]
    report = evaluate(rows, dirty_budget_bytes=10_000_000)
    assert report["original_image_view_count"] == 1
    assert report["should_rebuild"] is True
    assert "original image view" in report["reasons"][0]

    ignored = evaluate(rows, dirty_budget_bytes=10_000_000, rebuild_on_original_image_view=False)
    assert ignored["should_rebuild"] is False


def test_sidechain_bytes_are_runtime_load():
    rows = [user("主线"), assistant("回答"), user("子线" * 500, isSidechain=True)]
    ledger = measure(rows)
    assert ledger.categories.get("sidechain", 0) > 1000
    assert ledger.conversation_turns == 1
