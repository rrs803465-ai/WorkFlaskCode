from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    parse_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
            "raw_arguments": self.raw_arguments,
            "parse_error": self.parse_error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ToolCall:
        return cls(
            id=d["id"],
            name=d["name"],
            arguments=d.get("arguments") or {},
            raw_arguments=d.get("raw_arguments", ""),
            parse_error=d.get("parse_error"),
        )


@dataclass
class Message:
    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": [t.to_dict() for t in self.tool_calls],
            "tool_call_id": self.tool_call_id,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Message:
        return cls(
            role=d["role"],
            content=d.get("content") or "",
            tool_calls=[ToolCall.from_dict(t) for t in d.get("tool_calls") or []],
            tool_call_id=d.get("tool_call_id"),
            name=d.get("name"),
        )


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost: float | None = None
    estimated: bool = False

    def __add__(self, other: Usage) -> Usage:
        cost = None
        if self.cost is not None or other.cost is not None:
            cost = (self.cost or 0.0) + (other.cost or 0.0)
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cached_tokens + other.cached_tokens,
            cost,
            self.estimated or other.estimated,
        )


@dataclass
class ChatResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    model: str | None = None

    def as_message(self) -> Message:
        return Message(role="assistant", content=self.content, tool_calls=list(self.tool_calls))


@dataclass
class StreamEvent:
    kind: str
    text: str = ""
    response: ChatResponse | None = None


@dataclass
class Capabilities:
    tools: bool | None = None
    streaming: bool | None = None
    vision: bool | None = None
    context_length: int | None = None
    max_output_tokens: int | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class ModelInfo:
    id: str
    name: str
    provider: str
    description: str | None = None
    context_length: int | None = None
    prompt_price: float | None = None
    completion_price: float | None = None
    is_free: bool | None = None
    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    supports_vision: bool | None = None
    max_output_tokens: int | None = None
    routing: str | None = None
    created: str | None = None

    def capabilities(self) -> Capabilities:
        return Capabilities(
            tools=self.supports_tools,
            streaming=self.supports_streaming,
            vision=self.supports_vision,
            context_length=self.context_length,
            max_output_tokens=self.max_output_tokens,
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ModelInfo:
        allowed = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in allowed})

    def price_label(self) -> str:
        if self.is_free:
            return "free"
        if self.prompt_price is None and self.completion_price is None:
            return "price unknown"
        pin = "?" if self.prompt_price is None else f"${self.prompt_price * 1e6:.2f}"
        pout = "?" if self.completion_price is None else f"${self.completion_price * 1e6:.2f}"
        return f"{pin}/{pout} per 1M tok (in/out)"


@dataclass
class ValidationResult:
    ok: bool
    detail: str = ""
    kind: str | None = None
    inconclusive: bool = False


@dataclass
class ProviderUsageInfo:
    label: str | None = None
    used: float | None = None
    limit: float | None = None
    remaining: float | None = None
    is_free_tier: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)
