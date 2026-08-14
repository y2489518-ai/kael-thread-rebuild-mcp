from __future__ import annotations

from kael_thread_rebuild.transcript import event_text, select_turns, verify_candidate

from conftest import assistant, user


def test_selects_completed_real_turns_and_filters_internal_rows(tmp_path):
    tool_user = {
        "type": "user",
        "sessionId": "old-session",
        "message": {"content": [{"type": "tool_result", "content": "secret output"}]},
    }
    rows = [
        user("真实问题一"),
        assistant("真实回答一"),
        tool_user,
        user("<system-reminder>internal</system-reminder>"),
        assistant("internal reply"),
        user("记得我们的约定"),
        assistant("我会记得这个边界"),
        user("尚未回答"),
    ]
    result = select_turns(rows, target_tokens=5000, tail_turns=2, max_event_chars=1000)
    joined = "\n".join(event_text(event) for event in result.events)
    assert "真实问题一" in joined
    assert "约定" in joined
    assert "secret output" not in joined
    assert "system-reminder" not in joined
    assert "internal reply" not in joined
    assert "尚未回答" not in joined
    assert result.source_turns == 2


def test_rewrites_one_session_and_parent_chain():
    result = select_turns(
        [user("问题"), assistant("回答"), user("下一题"), assistant("下一答")],
        target_tokens=5000,
        tail_turns=2,
        max_event_chars=1000,
    )
    session_ids = {event["sessionId"] for event in result.events}
    assert len(session_ids) == 1
    parent = None
    for event in result.events:
        assert event["parentUuid"] == parent
        parent = event["uuid"]


def test_poison_recent_context_fails_closed_signal():
    rows = [user("AUP policy violation"), assistant("refusal loop"), user("毒上下文"), assistant("继续")]
    result = select_turns(rows, target_tokens=5000, tail_turns=2, max_event_chars=1000)
    assert result.poison_score >= 2
