from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .io import sha256_text


ITEM_NAMESPACE = uuid.UUID("6f0f8f6a-6f1e-4b1a-9a3f-2f5d1c8b7e40")

# 运行时注入进 user 事件里的内部块。它们是运行痕迹，不是人说的话。
INJECTED_TAGS = (
    "system-reminder",
    "local-command-caveat",
    "local-command-stdout",
    "local-command-stderr",
    "command-name",
    "command-message",
    "command-args",
    "memory-retrieval",
    "heartbeat",
    "scheduled-task",
    "task-notification",
    "ide_selection",
    "ide-selection",
)
_PAIRED_BLOCKS = tuple(
    re.compile(rf"<{tag}\b[^>]*>.*?</{tag}>", re.S | re.I) for tag in INJECTED_TAGS
)
_LONE_TAGS = tuple(
    re.compile(rf"</?{tag}\b[^>]*/?>", re.I) for tag in INJECTED_TAGS
)

# channel 消息的外壳。<channel> 刻意不进 INJECTED_TAGS——它不是运行痕迹，
# 是她从小屋/TG 说话的信封，留着新窗口才看得出这句话是从哪儿说的。
_CHANNEL_RE = re.compile(r'^\s*<channel\b[^>]*\bsource="', re.I)

# "对外说话"的工具。它们的 text 参数就是助手说出口的话。
# 只认跟她本人的两条线（小屋 / TG）；群聊、表情、附件不算发言。
_REPLY_TOOL_RE = re.compile(r"^mcp__[\w.-]*(?:companion|telegram)[\w.-]*__reply$", re.I)

# 保留这条：0812 那次 session 被 safety 注入毒掉，只能整段清零。
# 它不判断"经历重不重要"，只判断"这段上下文还能不能用"。
POISON_RE = re.compile(
    r"(AUP|Acceptable Use|policy violation|policy blocked|refusal loop|毒上下文|中毒|拒绝循环)",
    re.I,
)


@dataclass(frozen=True)
class Turn:
    """一个真实回合：连续的若干条人类消息 + 随后的助手最终回复。

    允许多条 user，是因为人可以连发好几条才等到一次回复；把它们拆成
    多个"没有回复的回合"再丢掉，等于把她说过的话删了。
    """

    index: int
    users: tuple[dict[str, Any], ...]
    assistants: tuple[dict[str, Any], ...]
    token_estimate: int

    @property
    def closed(self) -> bool:
        return bool(self.assistants)

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return (*self.users, *self.assistants)

    @property
    def text(self) -> str:
        return "\n".join(event_text(event) for event in self.events)


@dataclass(frozen=True)
class SelectionResult:
    events: tuple[dict[str, Any], ...]
    manifest: tuple[dict[str, Any], ...]
    source_turns: int
    selected_turns: int
    open_tail_turns: int
    dropped_oldest_turns: int
    estimated_tokens: int
    poison_score: int
    startup_frozen: bool

    def stats(self) -> dict[str, int | bool]:
        return {
            "source_turns": self.source_turns,
            "selected_turns": self.selected_turns,
            "open_tail_turns": self.open_tail_turns,
            "dropped_oldest_turns": self.dropped_oldest_turns,
            "estimated_tokens": self.estimated_tokens,
            "poison_score": self.poison_score,
            "startup_frozen": self.startup_frozen,
        }


def restore_queued_input(row: dict[str, Any]) -> dict[str, Any] | None:
    """把"排队进来的输入"还原成标准 user 事件。

    她在我干活时说的话不走正常 user 通道：Claude Code 先把它排队，落盘成
    type=attachment / attachment.type=queued_command，原文在 attachment.prompt
    里。不还原它，这些话在重建时会被整条丢掉——0814 实测，她那晚在小屋说的
    大半句子都是这么没的。

    只认 attachment 这一条：同一句话还会在 type=queue-operation 里出现两次
    （enqueue 一次、remove 一次），那两条是队列日志，跟着捞会让一句话进去三遍。

    只认 commandMode == "prompt"：排队的斜杠命令（/clear 之类）是运行痕迹，
    不是人说的话。
    """
    if row.get("type") != "attachment":
        return None
    attachment = row.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
        return None
    if attachment.get("commandMode") != "prompt":
        return None
    prompt = attachment.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    return {
        "type": "user",
        "uuid": row.get("uuid"),
        "parentUuid": row.get("parentUuid"),
        "sessionId": row.get("sessionId") or row.get("session_id"),
        "timestamp": row.get("timestamp") or attachment.get("timestamp"),
        # 已经确认是人打出来的字，不再让下游按 isMeta 判死。
        "isMeta": False,
        "isSidechain": False,
        "message": {"role": "user", "content": prompt},
    }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 transcript。

    读的时候顺手把排队进来的输入还原成 user 事件（见 restore_queued_input）。
    这是一对一替换，行数和顺序都不变，下游的计数、切分、去重都不受影响。
    """
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(value, dict):
                rows.append(restore_queued_input(value) or value)
    return rows


def strip_injected_blocks(text: str) -> str:
    """剥掉运行时注入块，留下真正被人打出来的字。

    真实 user 事件经常以 <system-reminder> 开头，真话在后面。只看开头就
    判断整条是不是注入，会把人说的话一起丢掉。
    """
    for pattern in _PAIRED_BLOCKS:
        text = pattern.sub("", text)
    for pattern in _LONE_TAGS:
        text = pattern.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(part for part in parts if part.strip()).strip()


def event_text(event: dict[str, Any]) -> str:
    message = event.get("message")
    return _text_content(message.get("content")) if isinstance(message, dict) else ""


def human_text(event: dict[str, Any]) -> str:
    return strip_injected_blocks(event_text(event))


def has_tool_result(event: dict[str, Any]) -> bool:
    content = event.get("message", {}).get("content")
    return isinstance(content, list) and any(
        isinstance(item, dict) and item.get("type") == "tool_result" for item in content
    )


def is_channel_message(event: dict[str, Any]) -> bool:
    """channel 消息（小屋 / Telegram）是她说的话，不是运行痕迹。

    Claude Code 把它们落成 type=user 且 **isMeta=True**，跟 system-reminder
    共用同一个标记。按 isMeta 一刀切就会把人话和噪音一起扔掉——0814 实测：
    她在小屋来回十几轮，切完只剩 3 个回合（终端里敲的那三次）。
    """
    message = event.get("message")
    if not isinstance(message, dict):
        return False
    return bool(_CHANNEL_RE.match(_text_content(message.get("content"))))


def _reply_texts(content: Any) -> list[str]:
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_use":
            continue
        if not _REPLY_TOOL_RE.match(str(item.get("name") or "")):
            continue
        payload = item.get("input")
        text = payload.get("text") if isinstance(payload, dict) else None
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return texts


def assistant_text(event: dict[str, Any]) -> str:
    """助手这侧真正说出口的话：终端里打的字 + 通过 channel 发出去的话。

    经 channel 说的话落在 transcript 里是 tool_use，不是 text。不还原它，
    重建出来就只剩她的话没有我的回答，对话变独白。
    """
    message = event.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    parts = [_text_content(content), *_reply_texts(content)]
    return "\n".join(part for part in parts if part.strip()).strip()


def is_real_user(event: dict[str, Any]) -> bool:
    if event.get("type") != "user" or event.get("isSidechain") is True:
        return False
    if event.get("isMeta") is True and not is_channel_message(event):
        return False
    if has_tool_result(event):
        return False
    return bool(human_text(event))


def is_text_assistant(event: dict[str, Any]) -> bool:
    if event.get("type") != "assistant" or event.get("isMeta") or event.get("isSidechain"):
        return False
    return bool(assistant_text(event))


def event_timestamp(event: dict[str, Any]) -> str:
    value = event.get("timestamp")
    return str(value).strip() if isinstance(value, str) else ""


def _compact(text: str, max_chars: int) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half].rstrip() + "\n\n[中段已按续窗规则省略]\n\n" + text[-half:].lstrip()


def _sanitize(event: dict[str, Any], max_chars: int, *, timestamp_prefix: bool = False) -> dict[str, Any]:
    clean = copy.deepcopy(event)
    kind = str(event.get("type"))
    raw = human_text(clean) if kind == "user" else assistant_text(clean)
    text = _compact(raw, max_chars)
    stamp = event_timestamp(event)
    if timestamp_prefix and stamp:
        text = f"[发生时间: {stamp}]\n{text}"
    clean["type"] = kind
    clean["isMeta"] = False
    clean["isSidechain"] = False
    content: Any = [{"type": "text", "text": text}] if kind == "assistant" else text
    clean["message"] = {"role": kind, "content": content}
    for key in ("requestId", "toolUseResult", "isApiErrorMessage", "error", "durationMs", "usage", "costUSD"):
        clean.pop(key, None)
    return clean


def conversation_turns(rows: Sequence[dict[str, Any]], max_event_chars: int, *, stamp_turns: bool = True) -> list[Turn]:
    """把原始事件归并成真实回合。

    - 工具结果、注入项、纯 thinking 的助手项：跳过，但**不打断**当前回合，
      否则夹在中间的 system-reminder 会把后面我说的话整段吞掉。
    - 连续多条人类消息：并进同一个回合。
    - 未闭合回合只可能出现在末尾（她说了我还没回），交由调用方决定去留。
    """
    turns: list[Turn] = []
    users: list[dict[str, Any]] = []
    assistants: list[dict[str, Any]] = []

    def close() -> None:
        nonlocal users, assistants
        if users:
            index = len(turns)
            clean_users = tuple(
                _sanitize(event, max_event_chars, timestamp_prefix=stamp_turns and position == 0)
                for position, event in enumerate(users)
            )
            clean_assistants = tuple(_sanitize(event, max_event_chars) for event in assistants)
            tokens = max(
                1,
                sum(len(event_text(event)) for event in (*clean_users, *clean_assistants)) // 3,
            )
            turns.append(Turn(index=index, users=clean_users, assistants=clean_assistants, token_estimate=tokens))
        users, assistants = [], []

    for row in rows:
        if is_real_user(row):
            if assistants:
                close()
            users.append(row)
        elif is_text_assistant(row) and users:
            assistants.append(row)
    close()
    return turns


def freeze_startup(rows: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """冻结当时真正用过的启动上下文。

    Claude Code 把 CLAUDE.md、记忆索引、环境说明注入在 <system-reminder> 里，
    所以只认这个容器。`/model` 之类 slash 命令留下的 caveat 和 command 痕迹
    虽然也是注入块，但它们是运行痕迹不是启动包，抓错了等于把一条无关的系统
    提示钉在新窗口最前面。

    走到第一条真话仍没见到启动注入，就说明这段 session 没有可冻结的启动包，
    返回 None，绝不拿别的东西凑数。
    """
    for row in rows:
        if row.get("type") != "user" or row.get("isSidechain") or has_tool_result(row):
            continue
        raw = event_text(row)
        if not raw:
            continue
        if "<system-reminder" in raw.lower():
            return copy.deepcopy(row)
        # channel 消息虽然带 isMeta，但它是真话：走到它就说明这段 session
        # 开口就是她在说话，没有启动包可冻结，不能继续往后翻去误抓。
        if row.get("isMeta") and not is_channel_message(row):
            continue
        if is_real_user(row):
            return None
    return None


def _startup_item(snapshot: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """把冻结快照做成一条 synthetic 启动项，避免首轮内容注入两次。"""
    raw = event_text(snapshot)
    body = _compact(raw, max_chars)
    stamp = event_timestamp(snapshot)
    header = "[续窗启动快照" + (f" · 冻结于 {stamp}" if stamp else "") + "]"
    clean = copy.deepcopy(snapshot)
    clean["type"] = "user"
    clean["isMeta"] = False
    clean["isSidechain"] = False
    clean["isStartupSnapshot"] = True
    clean["message"] = {"role": "user", "content": f"{header}\n{body}"}
    for key in ("requestId", "toolUseResult", "isApiErrorMessage", "error", "durationMs", "usage", "costUSD"):
        clean.pop(key, None)
    return clean


def build_source(
    rows: Sequence[dict[str, Any]],
    *,
    max_event_chars: int = 0,
    carry_max_tokens: int = 0,
    include_open_tail: bool = True,
    freeze_startup_snapshot: bool = True,
    stamp_turns: bool = True,
) -> SelectionResult:
    """构造新 thread 的注入源。

    这里刻意**不判断哪段经历重要**：闭合的真实对话全部带走，只把运行痕迹
    留在旧 transcript 里。体积由 dirty ledger 的触发时机来控，不由内容打分来控。
    `carry_max_tokens` 只是防炸的硬上限，超了从最老的整轮开始丢，并如实计数。
    """
    turns = conversation_turns(rows, max_event_chars, stamp_turns=stamp_turns)
    poison_score = len(POISON_RE.findall("\n".join(turn.text for turn in turns[-10:])))

    snapshot = freeze_startup(rows) if freeze_startup_snapshot else None
    selected = [turn for turn in turns if turn.closed or (include_open_tail and turn.index == len(turns) - 1)]

    dropped = 0
    if carry_max_tokens > 0:
        total = sum(turn.token_estimate for turn in selected)
        while len(selected) > 1 and total > carry_max_tokens:
            total -= selected[0].token_estimate
            selected.pop(0)
            dropped += 1

    startup_events: list[dict[str, Any]] = []
    if snapshot is not None:
        startup_events.append(_startup_item(snapshot, max_event_chars))
        snapshot_uuid = str(snapshot.get("uuid") or "")
        if snapshot_uuid:
            # 4.3：启动快照已含首轮人类消息，不再重复注入原件。
            selected = [
                Turn(
                    index=turn.index,
                    users=tuple(
                        event for event in turn.users if str(event.get("uuid") or "") != snapshot_uuid
                    ),
                    assistants=turn.assistants,
                    token_estimate=turn.token_estimate,
                )
                for turn in selected
            ]
            selected = [turn for turn in selected if turn.users or turn.assistants]

    source_events = [*startup_events, *(event for turn in selected for event in turn.events)]
    new_session_id = str(uuid.uuid4())
    rewritten: list[dict[str, Any]] = []
    manifest: list[dict[str, Any]] = []
    parent: str | None = None
    for position, event in enumerate(source_events):
        clean = copy.deepcopy(event)
        source_uuid = str(clean.get("uuid") or "")
        event_uuid = str(uuid.uuid5(ITEM_NAMESPACE, f"{new_session_id}:{position}:{source_uuid}"))
        clean["sessionId"] = new_session_id
        clean["uuid"] = event_uuid
        clean["parentUuid"] = parent
        parent = event_uuid
        rewritten.append(clean)
        manifest.append(
            {
                "position": position,
                "role": clean["type"],
                "source_uuid": source_uuid,
                "source_timestamp": event_timestamp(event),
                "source_sha256": sha256_text(event_text(event)),
                "injected_sha256": sha256_text(event_text(clean)),
                "item_id": event_uuid,
            }
        )
    return SelectionResult(
        events=tuple(rewritten),
        manifest=tuple(manifest),
        source_turns=len(turns),
        selected_turns=len(selected),
        open_tail_turns=sum(1 for turn in selected if not turn.closed),
        dropped_oldest_turns=dropped,
        estimated_tokens=sum(turn.token_estimate for turn in selected),
        poison_score=poison_score,
        startup_frozen=snapshot is not None,
    )


def session_id_from_events(events: Iterable[dict[str, Any]]) -> str:
    for event in reversed(list(events)):
        value = str(event.get("sessionId") or "").strip()
        if value:
            return value
    return ""


def dump_jsonl(events: Iterable[dict[str, Any]]) -> str:
    return "".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n" for event in events)


def verify_candidate(path: Path, manifest: Sequence[dict[str, Any]], expected_session_id: str) -> dict[str, Any]:
    events = load_jsonl(path)
    errors: list[str] = []
    if len(events) != len(manifest):
        errors.append(f"item_count expected={len(manifest)} actual={len(events)}")
    parent: str | None = None
    for index, (event, expected) in enumerate(zip(events, manifest)):
        if event.get("sessionId") != expected_session_id:
            errors.append(f"event[{index}] sessionId mismatch")
        if event.get("uuid") != expected["item_id"]:
            errors.append(f"event[{index}] item_id mismatch")
        if event.get("parentUuid") != parent:
            errors.append(f"event[{index}] parentUuid mismatch")
        if event.get("type") not in {"user", "assistant"}:
            errors.append(f"event[{index}] forbidden type={event.get('type')}")
        if has_tool_result(event):
            errors.append(f"event[{index}] contains tool_result")
        if sha256_text(event_text(event)) != expected["injected_sha256"]:
            errors.append(f"event[{index}] content digest mismatch")
        parent = str(event.get("uuid") or "")
    return {"ok": not errors, "item_count": len(events), "errors": errors}
