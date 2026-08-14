from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .transcript import (
    event_text,
    human_text,
    is_real_user,
    is_text_assistant,
    strip_injected_blocks,
)


IMAGE_TYPES = {"image"}


def _blob(value: Any) -> int:
    """粗略估算一个内部项占多少字节。"""
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, (list, dict)):
        try:
            import json

            return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            return 0
    return 0


@dataclass
class DirtyLedger:
    """只计运行负担，不计聊天语义。

    体积该由"清得多勤"来控，不由"删哪段回忆"来控，所以这里统计的全是
    工具回包、thinking、图片、注入块这类不需要长期占住上下文的东西。
    对话本身单独计在 conversation_bytes，用来对照噪音占比。
    """

    total_bytes: int = 0
    categories: dict[str, int] = field(default_factory=dict)
    preview_image_count: int = 0
    original_image_view_count: int = 0
    conversation_bytes: int = 0
    conversation_turns: int = 0

    def add(self, category: str, size: int) -> None:
        if size <= 0:
            return
        self.total_bytes += size
        self.categories[category] = self.categories.get(category, 0) + size

    def as_dict(self) -> dict[str, Any]:
        total = self.total_bytes + self.conversation_bytes
        return {
            "total_bytes": self.total_bytes,
            "categories": dict(sorted(self.categories.items(), key=lambda item: -item[1])),
            "preview_image_count": self.preview_image_count,
            "original_image_view_count": self.original_image_view_count,
            "conversation_bytes": self.conversation_bytes,
            "conversation_turns": self.conversation_turns,
            "noise_ratio": round(self.total_bytes / total, 4) if total else 0.0,
        }


def measure(rows: Sequence[dict[str, Any]]) -> DirtyLedger:
    ledger = DirtyLedger()
    open_turn = False
    for row in rows:
        kind = row.get("type")
        content = row.get("message", {}).get("content") if isinstance(row.get("message"), dict) else None

        if row.get("isSidechain"):
            ledger.add("sidechain", _blob(content))
            continue

        if kind == "user":
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type")
                    if item_type == "tool_result":
                        ledger.add("tool_result", _blob(item.get("content")))
                    elif item_type in IMAGE_TYPES:
                        ledger.preview_image_count += 1
                        ledger.add("image", _blob(item.get("source")))
            raw = event_text(row)
            if raw:
                stripped = strip_injected_blocks(raw)
                injected = len(raw.encode("utf-8")) - len(stripped.encode("utf-8"))
                ledger.add("injected_block", injected)
                if is_real_user(row):
                    ledger.conversation_bytes += len(human_text(row).encode("utf-8"))
                    if not open_turn:
                        ledger.conversation_turns += 1
                        open_turn = True
                elif not stripped:
                    pass  # 纯注入项，字节已计入 injected_block
            if row.get("toolUseResult") is not None:
                ledger.add("tool_result", _blob(row.get("toolUseResult")))

        elif kind == "assistant":
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type")
                    if item_type == "tool_use":
                        ledger.add("tool_use", _blob(item.get("input")))
                    elif item_type in {"thinking", "redacted_thinking"}:
                        ledger.add("thinking", _blob(item.get("thinking") or item.get("data")))
                    elif item_type in IMAGE_TYPES:
                        ledger.original_image_view_count += 1
                        ledger.add("image", _blob(item.get("source")))
            if is_text_assistant(row):
                ledger.conversation_bytes += len(event_text(row).encode("utf-8"))
                open_turn = False

        elif kind in {"system", "progress", "summary"}:
            ledger.add(str(kind), _blob(content) or _blob(row))

    return ledger


def evaluate(
    rows: Sequence[dict[str, Any]],
    *,
    dirty_budget_bytes: int,
    rebuild_on_original_image_view: bool = True,
) -> dict[str, Any]:
    """算出脏预算，并给出该不该重建的机械判断。"""
    ledger = measure(rows)
    reasons: list[str] = []
    if dirty_budget_bytes > 0 and ledger.total_bytes >= dirty_budget_bytes:
        reasons.append(
            f"dirty budget reached: {ledger.total_bytes} >= {dirty_budget_bytes} bytes"
        )
    if rebuild_on_original_image_view and ledger.original_image_view_count:
        reasons.append(
            f"original image view: {ledger.original_image_view_count}"
        )
    payload = ledger.as_dict()
    payload["dirty_budget_bytes"] = dirty_budget_bytes
    payload["should_rebuild"] = bool(reasons)
    payload["reasons"] = reasons
    return payload
