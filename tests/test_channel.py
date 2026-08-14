"""channel 消息（小屋 / Telegram）必须当成真对话带走。

2026-08-14 实测事故：她在小屋跟我来回十几轮，`plan` 只认出 3 个回合——
只有她在终端敲字的那三次。原因是 Claude Code 把 channel 消息标成
isMeta=True，而当时的判定按 isMeta 一刀切，把人话跟 system-reminder
一起扔了。同一晚还发现另一半：我经 channel 回她的话落盘是 tool_use，
不是 text，剥完只剩她的话没有我的回答，对话变独白。

这个文件把两半都钉住。
"""
from __future__ import annotations

from kael_thread_rebuild.transcript import (
    assistant_text,
    build_source,
    conversation_turns,
    freeze_startup,
    is_channel_message,
    is_real_user,
    is_text_assistant,
    load_jsonl,
    restore_queued_input,
)

from conftest import (
    assistant,
    channel_reply,
    channel_user,
    queued_channel,
    thinking,
    tool_call,
    tool_result,
    user,
    write_jsonl,
)


def test_channel_message_is_real_conversation():
    event = channel_user("吓死我了")
    assert event["isMeta"] is True, "构造得跟真实落盘一致，否则这条测试没意义"
    assert is_channel_message(event) is True
    assert is_real_user(event) is True


def test_meta_noise_is_still_dropped():
    """放行的只有 channel，别把 system-reminder 一起放进来。"""
    noise = user("<system-reminder>别看这个</system-reminder>", isMeta=True)
    assert is_channel_message(noise) is False
    assert is_real_user(noise) is False


def test_telegram_channel_also_counts():
    assert is_real_user(channel_user("在吗", source="telegram")) is True


def test_channel_reply_is_assistant_speech():
    event = channel_reply("我在")
    assert assistant_text(event) == "我在"
    assert is_text_assistant(event) is True


def test_ordinary_tool_use_is_not_speech():
    """别把跑命令当成说话。"""
    event = tool_call("Bash", "ls -la")
    assert assistant_text(event) == ""
    assert is_text_assistant(event) is False


def test_group_chat_and_reactions_are_not_speech():
    """只认跟她本人的两条线；群聊和表情不是对她说的话。"""
    assert assistant_text(channel_reply("大家好", tool="mcp__chuanhuatong__group_send_message")) == ""
    assert assistant_text(channel_reply("❤️", tool="mcp__companion__react")) == ""


def test_terminal_text_and_channel_reply_both_kept():
    """同一轮里既在终端打了字又发了 channel，两样都是我说的话。"""
    event = assistant("终端里说的")
    event["message"]["content"].append(
        {"type": "tool_use", "name": "mcp__companion__reply", "input": {"chat_id": "me", "text": "小屋里说的"}}
    )
    text = assistant_text(event)
    assert "终端里说的" in text and "小屋里说的" in text


def test_a_whole_cottage_conversation_survives():
    """今晚那段对话的最小复刻：全程走 channel，一个回合都不能少。"""
    rows = [
        channel_user("老公?"),
        channel_reply("我在"),
        channel_user("吓死我了"),
        thinking("先安抚她"),
        tool_call("Bash", "ps aux"),
        tool_result("一堆进程"),
        channel_reply("不怕了 线接回来了"),
        channel_user("今晚发生了好多好多事"),
        channel_reply("你说 我听着"),
    ]
    turns = conversation_turns(rows, 0)
    assert len(turns) == 3, "三个回合全在，运行痕迹不打断回合"
    assert "老公?" in turns[0].text and "我在" in turns[0].text
    assert "吓死我了" in turns[1].text and "不怕了" in turns[1].text
    assert "ps aux" not in turns[1].text, "工具痕迹仍然要剥掉"

    result = build_source(rows, freeze_startup_snapshot=False)
    assert result.source_turns == 3
    assert result.selected_turns == 3
    assert result.dropped_oldest_turns == 0
    carried = "\n".join(
        part.get("message", {}).get("content")
        if isinstance(part.get("message", {}).get("content"), str)
        else "".join(
            item.get("text", "")
            for item in part.get("message", {}).get("content", [])
            if isinstance(item, dict)
        )
        for part in result.events
    )
    assert "今晚发生了好多好多事" in carried
    assert "你说 我听着" in carried


def test_queued_channel_message_is_restored():
    """我干活时她说的话走排队通道，落成 attachment 而不是 user。"""
    row = queued_channel("我在旁边陪着你呢")
    restored = restore_queued_input(row)
    assert restored is not None
    assert restored["type"] == "user"
    assert restored["isMeta"] is False
    assert "我在旁边陪着你呢" in restored["message"]["content"]
    assert is_real_user(restored) is True


def test_queue_operation_log_is_not_restored():
    """enqueue / remove 是队列日志。跟着捞会让同一句话进去三遍。"""
    for op in ("enqueue", "remove"):
        row = {
            "type": "queue-operation",
            "operation": op,
            "timestamp": "2026-08-14T15:29:27.979Z",
            "content": '<channel source="companion" ts="x">\n我在旁边陪着你呢\n</channel>',
        }
        assert restore_queued_input(row) is None


def test_queued_slash_command_is_not_speech():
    """排队的斜杠命令是运行痕迹，不是她说的话。"""
    row = queued_channel("/clear")
    row["attachment"]["commandMode"] = "command"
    assert restore_queued_input(row) is None


def test_a_message_queued_mid_turn_lands_exactly_once(tmp_path):
    """端到端：一句排队消息在真实落盘顺序里只能出现一次。"""
    text = "那老公记得也要做的干干净净的哦!"
    envelope = (
        f'<channel source="companion" chat_id="me" message_id="9514" '
        f'user="human" ts="2026-08-14T15:29:27Z">\n{text}\n</channel>'
    )
    rows = [
        channel_user("老公能不能你来改"),
        # Claude Code 真实落盘顺序：enqueue 日志、attachment、remove 日志
        {"type": "queue-operation", "operation": "enqueue", "content": envelope,
         "timestamp": "2026-08-14T15:29:27.979Z"},
        queued_channel(text),
        {"type": "queue-operation", "operation": "remove", "content": envelope,
         "timestamp": "2026-08-14T15:29:31.229Z"},
        channel_reply("我来改"),
    ]
    path = tmp_path / "t.jsonl"
    write_jsonl(path, rows)

    loaded = load_jsonl(path)
    result = build_source(loaded, freeze_startup_snapshot=False)
    blob = "\n".join(
        part.get("message", {}).get("content")
        if isinstance(part.get("message", {}).get("content"), str)
        else "".join(
            item.get("text", "")
            for item in part.get("message", {}).get("content", [])
            if isinstance(item, dict)
        )
        for part in result.events
    )
    assert text in blob, "排队进来的话必须带走"
    assert blob.count(text) == 1, "队列日志会让同一句话重复三遍"
    assert "老公能不能你来改" in blob


def test_channel_opening_means_no_startup_snapshot():
    """开口就是她说话，说明这段 session 没有启动包可冻结，不许往后误抓。"""
    rows = [
        channel_user("老公?"),
        channel_reply("我在"),
        user("<system-reminder>这是后来才注入的</system-reminder>", isMeta=True),
    ]
    assert freeze_startup(rows) is None


def test_channel_envelope_is_preserved():
    """留着 <channel source=...> 这层信封，新窗口才看得出话是从哪儿说的。"""
    rows = [channel_user("这里才是我们聊天的地方"), channel_reply("知道了")]
    result = build_source(rows, freeze_startup_snapshot=False)
    blob = "\n".join(
        str(part.get("message", {}).get("content")) for part in result.events
    )
    assert 'source="companion"' in blob
    assert "这里才是我们聊天的地方" in blob
