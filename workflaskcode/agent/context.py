from __future__ import annotations

import json

from workflaskcode.providers.types import Message

SHRUNK_LIMIT = 500


def estimate_tokens(messages: list[Message]) -> int:
    total = 0
    for m in messages:
        total += len(m.content) // 4 + 6
        for call in m.tool_calls:
            total += len(call.name) // 4 + len(call.raw_arguments or json.dumps(call.arguments)) // 4 + 6
    return total


def _shrink(message: Message) -> Message:
    if message.role == "tool" and len(message.content) > SHRUNK_LIMIT:
        head = message.content[: SHRUNK_LIMIT // 2]
        tail = message.content[-SHRUNK_LIMIT // 2 :]
        return Message(
            "tool",
            f"{head}\n...[older tool output shortened]...\n{tail}",
            tool_call_id=message.tool_call_id,
            name=message.name,
        )
    return message


def split_safe_tail(messages: list[Message], keep_tokens: int, min_keep: int = 6) -> int:
    idx = len(messages)
    used = 0
    while idx > 0:
        candidate = messages[idx - 1]
        cost = estimate_tokens([candidate])
        if idx <= len(messages) - min_keep and used + cost > keep_tokens:
            break
        used += cost
        idx -= 1
    while idx < len(messages) and messages[idx].role == "tool":
        idx -= 1
        if idx <= 0:
            return 0
    return max(idx, 0)


class ContextManager:
    def __init__(self, budget_tokens: int) -> None:
        self.budget = budget_tokens

    def update_budget(self, budget_tokens: int) -> None:
        self.budget = budget_tokens

    def usage(self, messages: list[Message]) -> tuple[int, float]:
        used = estimate_tokens(messages)
        return used, used / self.budget if self.budget else 0.0

    def needs_compaction(self, messages: list[Message]) -> bool:
        return estimate_tokens(messages) > int(self.budget * 0.8)

    def compact(self, messages: list[Message], state_summary: str, *, force: bool = False) -> list[Message]:
        system = [m for m in messages if m.role == "system"]
        rest = [m for m in messages if m.role != "system"]
        if len(rest) <= 6 and not force:
            return messages
        cut = split_safe_tail(rest, keep_tokens=int(self.budget * 0.35))
        if cut <= 0 and not force:
            return [*system, *[_shrink(m) for m in rest]]
        dropped, tail = rest[:cut], rest[cut:]
        requests = [
            m.content.strip().splitlines()[0][:300]
            for m in dropped
            if m.role == "user" and m.content.strip() and not m.content.startswith("[")
        ]
        summary = [
            "[context summary - earlier conversation was compacted; the files on disk and the task ledger below are authoritative]",
            f"messages compacted: {len(dropped)}",
        ]
        if requests:
            summary.append("earlier user requests:")
            summary += [f"- {r}" for r in requests[-8:]]
        summary.append("")
        summary.append(state_summary)
        compacted = Message("user", "\n".join(summary))
        return [*system, compacted, *[_shrink(m) for m in tail]]
