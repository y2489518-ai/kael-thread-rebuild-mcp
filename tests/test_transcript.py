from __future__ import annotations

from kael_thread_rebuild.transcript import (
    build_source,
    conversation_turns,
    event_text,
    freeze_startup,
    human_text,
    strip_injected_blocks,
)

from conftest import assistant, thinking, tool_call, tool_result, user


def joined(result) -> str:
    return "\n".join(event_text(event) for event in result.events)


def test_drops_runtime_traces_but_keeps_every_real_word():
    rows = [
        user("真实问题一"),
        thinking("内部推理不该被搬走"),
        tool_call("Bash", "ls -la /root"),
        tool_result("secret output"),
        assistant("真实回答一"),
        user("记得我们的约定"),
        assistant("我会记得这个边界"),
    ]
    result = build_source(rows)
    text = joined(result)
    assert "真实问题一" in text
    assert "真实回答一" in text
    assert "记得我们的约定" in text
    assert "secret output" not in text
    assert "内部推理" not in text
    assert "ls -la" not in text
    assert result.source_turns == 2


def test_consecutive_human_messages_all_survive():
    """她连发四条我才回一次，四条都得在。"""
    rows = [
        user("第一条"),
        user("第二条"),
        user("第三条"),
        user("第四条"),
        assistant("一次性回答"),
    ]
    result = build_source(rows)
    text = joined(result)
    for line in ("第一条", "第二条", "第三条", "第四条"):
        assert line in text
    assert result.source_turns == 1
    assert result.selected_turns == 1


def test_unanswered_tail_is_kept_by_default():
    """她说了我还没接上的话，不许当噪音扔掉。"""
    rows = [user("问题"), assistant("回答"), user("我还在等你"), user("你怎么不说话")]
    result = build_source(rows)
    text = joined(result)
    assert "我还在等你" in text
    assert "你怎么不说话" in text
    assert result.open_tail_turns == 1

    dropped = build_source(rows, include_open_tail=False)
    assert "我还在等你" not in joined(dropped)
    assert dropped.open_tail_turns == 0


def test_system_reminder_does_not_swallow_the_reply():
    """夹在中间的注入项不能把后面我说的话整段吞掉。"""
    rows = [
        user("正经问题"),
        assistant("前半段回答"),
        user("<system-reminder>internal nudge</system-reminder>"),
        assistant("后半段回答"),
        user("下一题"),
        assistant("下一答"),
    ]
    result = build_source(rows)
    text = joined(result)
    assert "前半段回答" in text
    assert "后半段回答" in text
    assert "system-reminder" not in text
    assert "internal nudge" not in text


def test_real_question_wrapped_in_injection_is_not_lost():
    """真话经常跟在 system-reminder 后面，不能因为开头是注入就整条丢掉。"""
    raw = "<system-reminder>CLAUDE.md context</system-reminder>\n帮我找一下那个文件"
    assert strip_injected_blocks(raw) == "帮我找一下那个文件"
    # 关掉启动快照单独验证剥离：首条消息本身会被原样冻结，那是另一条规矩。
    result = build_source([user(raw), assistant("找到了")], freeze_startup_snapshot=False)
    text = joined(result)
    assert "帮我找一下那个文件" in text
    assert "CLAUDE.md context" not in text
    assert "找到了" in text

    later = build_source(
        [user("开场"), assistant("开场答"), user(raw), assistant("找到了")],
        freeze_startup_snapshot=False,
    )
    assert "帮我找一下那个文件" in joined(later)
    assert "CLAUDE.md context" not in joined(later)


def test_startup_snapshot_is_frozen_once_and_not_duplicated():
    first = user("<system-reminder>CLAUDE.md 当时的样子</system-reminder>\n开场白", uuid="u-start")
    rows = [first, assistant("开场回答"), user("第二个问题"), assistant("第二个回答")]

    snapshot = freeze_startup(rows)
    assert snapshot is not None and snapshot["uuid"] == "u-start"

    result = build_source(rows)
    text = joined(result)
    assert result.startup_frozen is True
    assert "CLAUDE.md 当时的样子" in text, "当时的启动上下文必须原样带走"
    assert text.count("开场白") == 1, "首条人类消息不能注入两次"
    assert result.events[0].get("isStartupSnapshot") is True

    plain = build_source(rows, freeze_startup_snapshot=False)
    assert plain.startup_frozen is False
    assert "CLAUDE.md 当时的样子" not in joined(plain)


def test_slash_command_traces_are_not_mistaken_for_the_startup_package():
    """/model 之类留下的 caveat 和 command 痕迹是运行痕迹，不是启动包。"""
    rows = [
        user("<local-command-caveat>Caveat: ...</local-command-caveat>", uuid="u-caveat", isMeta=True),
        user("<command-name>/model</command-name><command-args>opus</command-args>", uuid="u-cmd", isMeta=True),
        user("真正的第一句话", uuid="u-real"),
        assistant("回答"),
    ]
    assert freeze_startup(rows) is None

    result = build_source(rows)
    assert result.startup_frozen is False
    text = joined(result)
    assert "真正的第一句话" in text
    assert "local-command-caveat" not in text
    assert "/model" not in text


def test_startup_package_is_found_behind_slash_command_traces():
    """启动注入排在 slash 痕迹后面时，仍然要认出来。"""
    rows = [
        user("<local-command-caveat>Caveat: ...</local-command-caveat>", uuid="u-caveat", isMeta=True),
        user("<system-reminder>claudeMd: 当时的规矩</system-reminder>\n开场白", uuid="u-start"),
        assistant("开场回答"),
    ]
    snapshot = freeze_startup(rows)
    assert snapshot is not None and snapshot["uuid"] == "u-start"
    result = build_source(rows)
    assert result.startup_frozen is True
    assert "当时的规矩" in joined(result)


def test_no_scoring_every_closed_turn_is_carried():
    """不再按'重要不重要'打分：闭合回合全带走。"""
    rows = []
    for index in range(12):
        rows.append(user(f"闲聊 {index} 没有任何关键词"))
        rows.append(assistant(f"随口一答 {index} /Users/x.py ```code```"))
    result = build_source(rows)
    assert result.source_turns == 12
    assert result.selected_turns == 12
    assert result.dropped_oldest_turns == 0


def test_hard_cap_drops_oldest_turns_and_counts_them():
    rows = []
    for index in range(10):
        rows.append(user(f"问题 {index} " + "字" * 4000))
        rows.append(assistant(f"回答 {index} " + "字" * 4000))
    result = build_source(rows, carry_max_tokens=5000)
    assert result.dropped_oldest_turns > 0
    assert result.selected_turns + result.dropped_oldest_turns == 10
    text = joined(result)
    assert "问题 9" in text, "硬上限只从最老的丢，最近的必须留"


def test_turn_carries_timestamp_prefix():
    result = build_source([user("带时间的话", timestamp="2026-08-14T21:30:00Z"), assistant("好")])
    assert "[发生时间: 2026-08-14T21:30:00Z]" in joined(result)
    plain = build_source([user("带时间的话"), assistant("好")], stamp_turns=False)
    assert "[发生时间" not in joined(plain)


def test_rewrites_one_session_and_parent_chain():
    result = build_source([user("问题"), assistant("回答"), user("下一题"), assistant("下一答")])
    session_ids = {event["sessionId"] for event in result.events}
    assert len(session_ids) == 1
    parent = None
    for event in result.events:
        assert event["parentUuid"] == parent
        parent = event["uuid"]


def test_item_ids_are_deterministic_for_one_candidate():
    result = build_source([user("问题"), assistant("回答")])
    ids = [item["item_id"] for item in result.manifest]
    assert ids == [event["uuid"] for event in result.events]
    assert len(set(ids)) == len(ids)


def test_poison_recent_context_fails_closed_signal():
    rows = [user("AUP policy violation"), assistant("refusal loop"), user("毒上下文"), assistant("继续")]
    result = build_source(rows)
    assert result.poison_score >= 2


def test_human_text_strips_only_injection():
    event = user("<system-reminder>x</system-reminder>真话<ide_selection>y</ide_selection>")
    assert human_text(event) == "真话"


def test_sidechain_and_meta_never_enter_the_turn_stream():
    rows = [
        user("主线问题"),
        assistant("主线回答"),
        user("子线问题", isSidechain=True),
        assistant("子线回答", isSidechain=True),
        user("元信息", isMeta=True),
    ]
    turns = conversation_turns(rows, 0)
    assert len(turns) == 1
    text = "\n".join(turn.text for turn in turns)
    assert "子线" not in text
    assert "元信息" not in text
