from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .io import sha256_text


INJECTED_PREFIXES = (
    "<system-reminder",
    "<local-command",
    "<command-name",
    "<command-message",
    "<memory-retrieval",
    "<heartbeat",
    "<scheduled-task",
    "<task-notification",
)
MEMORY_RE = re.compile(
    r"(remember|don't forget|preference|likes?|dislikes?|relationship|boundary|nickname|identity|promise|continuity|memory|"
    r"记得|别忘|偏好|喜欢|讨厌|关系|边界|称呼|身份|承诺|以后|连续性|记忆|规矩|约定)",
    re.I,
)
STATE_RE = re.compile(
    r"(current task|next step|risk|done|todo|checkpoint|blocked|decision|"
    r"当前任务|下一步|风险|已完成|待办|检查点|阻塞|决定|确认|部署|修复)",
    re.I,
)
NOISE_RE = re.compile(
    r"(Traceback|Exception|Exit code|Chunk ID|Wall time|stdout|stderr|apply_patch|pytest|npm |pnpm |yarn |"
    r"curl |ssh |tmux |systemctl|journalctl|SELECT |INSERT |UPDATE |DELETE |CREATE TABLE|"
    r"/Users/|/root/|/opt/|\.py\b|\.sh\b|\.jsonl\b|\.sqlite\b|tool_result|tool_use|```|^\s*\{)",
    re.I | re.M,
)
POISON_RE = re.compile(
    r"(AUP|Acceptable Use|policy violation|policy blocked|refusal loop|毒上下文|中毒|拒绝循环)",
    re.I,
)


@dataclass(frozen=True)
class Turn:
    index: int
    user: dict[str, Any]
    assistants: tuple[dict[str, Any], ...]
    text: str
    score: int
    token_estimate: int


@dataclass(frozen=True)
class SelectionResult:
    events: tuple[dict[str, Any], ...]
    manifest: tuple[dict[str, Any], ...]
    source_turns: int
    selected_turns: int
    selected_high_signal: int
    selected_tail: int
    estimated_tokens: int
    poison_score: int

    def stats(self) -> dict[str, int]:
        return {
            "source_turns": self.source_turns,
            "selected_turns": self.selected_turns,
            "selected_high_signal": self.selected_high_signal,
            "selected_tail": self.selected_tail,
            "estimated_tokens": self.estimated_tokens,
            "poison_score": self.poison_score,
        }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


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


def is_real_user(event: dict[str, Any]) -> bool:
    if event.get("type") != "user" or event.get("isMeta") is True or event.get("isSidechain") is True:
        return False
    content = event.get("message", {}).get("content")
    if isinstance(content, list) and any(isinstance(item, dict) and item.get("type") == "tool_result" for item in content):
        return False
    text = event_text(event).lstrip().lower()
    return bool(text) and not text.startswith(INJECTED_PREFIXES)


def is_tool_result_user(event: dict[str, Any]) -> bool:
    if event.get("type") != "user":
        return False
    content = event.get("message", {}).get("content")
    return isinstance(content, list) and any(
        isinstance(item, dict) and item.get("type") == "tool_result" for item in content
    )


def is_text_assistant(event: dict[str, Any]) -> bool:
    return event.get("type") == "assistant" and not event.get("isMeta") and bool(event_text(event))


def _compact(text: str, max_chars: int) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half].rstrip() + "\n\n[中段已按续窗规则省略]\n\n" + text[-half:].lstrip()


def _sanitize(event: dict[str, Any], max_chars: int) -> dict[str, Any]:
    clean = copy.deepcopy(event)
    text = _compact(event_text(clean), max_chars)
    clean["type"] = str(event.get("type"))
    clean["isMeta"] = False
    clean["isSidechain"] = False
    content: Any = [{"type": "text", "text": text}] if clean["type"] == "assistant" else text
    clean["message"] = {"role": clean["type"], "content": content}
    for key in ("requestId", "toolUseResult", "isApiErrorMessage", "error", "durationMs", "usage", "costUSD"):
        clean.pop(key, None)
    return clean


def completed_turns(rows: Sequence[dict[str, Any]], max_event_chars: int) -> list[Turn]:
    turns: list[Turn] = []
    current_user: dict[str, Any] | None = None
    assistants: list[dict[str, Any]] = []

    def close() -> None:
        nonlocal current_user, assistants
        if current_user is None or not assistants:
            current_user, assistants = None, []
            return
        clean_user = _sanitize(current_user, max_event_chars)
        clean_assistants = tuple(_sanitize(event, max_event_chars) for event in assistants)
        text = "\n".join([event_text(clean_user), *(event_text(event) for event in clean_assistants)])
        memory_hits = len(MEMORY_RE.findall(text))
        state_hits = len(STATE_RE.findall(text))
        noise_hits = len(NOISE_RE.findall(text))
        score = memory_hits * 5 + state_hits * 3 - min(noise_hits, 8)
        tokens = max(1, sum(len(event_text(event)) for event in (clean_user, *clean_assistants)) // 3)
        turns.append(Turn(len(turns), clean_user, clean_assistants, text, score, tokens))
        current_user, assistants = None, []

    for row in rows:
        if is_real_user(row):
            close()
            current_user = row
        elif row.get("type") == "user" and not is_tool_result_user(row):
            # A system-shaped user item starts a separate internal turn. Close
            # the real turn, then ignore assistant text produced for the
            # injected item so it cannot be attributed to the human.
            close()
        elif current_user is not None and is_text_assistant(row):
            assistants.append(row)
    close()
    return turns


def select_turns(
    rows: Sequence[dict[str, Any]],
    *,
    target_tokens: int,
    tail_turns: int,
    max_event_chars: int,
) -> SelectionResult:
    turns = completed_turns(rows, max_event_chars)
    poison_score = len(POISON_RE.findall("\n".join(turn.text for turn in turns[-10:])))
    tail = turns[-tail_turns:]
    selected: dict[int, Turn] = {turn.index: turn for turn in tail}
    high_signal = sorted((turn for turn in turns if turn.score > 0), key=lambda item: (item.score, item.index), reverse=True)
    for turn in high_signal:
        selected.setdefault(turn.index, turn)

    ordered = sorted(selected.values(), key=lambda item: item.index)
    total = sum(turn.token_estimate for turn in ordered)
    protected_tail = {turn.index for turn in tail}
    removable = sorted(
        (turn for turn in ordered if turn.index not in protected_tail),
        key=lambda item: (item.score, -item.index),
    )
    remove_ids: set[int] = set()
    for turn in removable:
        if total <= target_tokens:
            break
        remove_ids.add(turn.index)
        total -= turn.token_estimate
    ordered = [turn for turn in ordered if turn.index not in remove_ids]

    source_events = [event for turn in ordered for event in (turn.user, *turn.assistants)]
    new_session_id = str(uuid.uuid4())
    rewritten: list[dict[str, Any]] = []
    manifest: list[dict[str, Any]] = []
    parent: str | None = None
    for position, event in enumerate(source_events):
        clean = copy.deepcopy(event)
        source_uuid = str(clean.get("uuid") or "")
        event_uuid = str(uuid.uuid4())
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
                "source_sha256": sha256_text(event_text(event)),
                "injected_sha256": sha256_text(event_text(clean)),
                "item_id": event_uuid,
            }
        )
    return SelectionResult(
        events=tuple(rewritten),
        manifest=tuple(manifest),
        source_turns=len(turns),
        selected_turns=len(ordered),
        selected_high_signal=sum(1 for turn in ordered if turn.score > 0),
        selected_tail=sum(1 for turn in ordered if turn.index in protected_tail),
        estimated_tokens=sum(turn.token_estimate for turn in ordered),
        poison_score=poison_score,
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
        if sha256_text(event_text(event)) != expected["injected_sha256"]:
            errors.append(f"event[{index}] content digest mismatch")
        parent = str(event.get("uuid") or "")
    return {"ok": not errors, "item_count": len(events), "errors": errors}
