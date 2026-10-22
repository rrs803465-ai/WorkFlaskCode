from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from workflaskcode.errors import ErrorKind, ProviderError
from workflaskcode.http import TransportError
from workflaskcode.providers.base import MAX_PAGES, Provider, iter_sse, parse_tool_arguments
from workflaskcode.providers.errors import classify_http_error, transport_error
from workflaskcode.providers.types import (
    ChatResponse,
    Message,
    ModelInfo,
    StreamEvent,
    ToolCall,
    ToolSpec,
    Usage,
    ValidationResult,
)

ANTHROPIC_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096


def messages_to_anthropic(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        if m.role == "system":
            if m.content:
                system_parts.append(m.content)
        elif m.role == "user":
            push("user", [{"type": "text", "text": m.content or "(empty)"}])
        elif m.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for t in m.tool_calls:
                blocks.append({"type": "tool_use", "id": t.id, "name": t.name, "input": t.arguments or {}})
            push("assistant", blocks or [{"type": "text", "text": "(empty)"}])
        elif m.role == "tool":
            push(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id or "",
                        "content": m.content,
                    }
                ],
            )
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(start)"}]})
    return "\n\n".join(system_parts), out


def tools_to_anthropic(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]


class AnthropicProvider(Provider):
    @property
    def base_url(self) -> str:
        return (self.config.base_url or ANTHROPIC_BASE).rstrip("/")

    def _auth_headers(self) -> dict[str, str]:
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        return headers

    def list_models(self) -> list[ModelInfo]:
        models: list[ModelInfo] = []
        after: str | None = None
        for _ in range(MAX_PAGES):
            params = "?limit=100" + (f"&after_id={after}" if after else "")
            data = self._json("GET", "models", params=params)
            items = data.get("data") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise ProviderError(
                    "Anthropic returned an unexpected model-list shape",
                    kind=ErrorKind.MALFORMED_RESPONSE,
                    provider=self.name,
                )
            for raw in items:
                if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                    continue
                caps = raw.get("capabilities") if isinstance(raw.get("capabilities"), dict) else {}
                models.append(
                    ModelInfo(
                        id=raw["id"],
                        name=str(raw.get("display_name") or raw["id"]),
                        provider=self.name,
                        context_length=raw.get("max_input_tokens")
                        if isinstance(raw.get("max_input_tokens"), int)
                        else None,
                        max_output_tokens=raw.get("max_tokens")
                        if isinstance(raw.get("max_tokens"), int)
                        else None,
                        supports_vision=_cap(caps, "image_input"),
                        supports_tools=_cap(caps, "tool_use") if "tool_use" in caps else None,
                        created=str(raw["created_at"])[:10] if raw.get("created_at") else None,
                        routing="anthropic",
                    )
                )
            if isinstance(data, dict) and data.get("has_more") and data.get("last_id"):
                after = data["last_id"]
                continue
            break
        self.remember_models(models)
        return models

    def validate_credentials(self) -> ValidationResult:
        try:
            self._json("GET", "models", params="?limit=1")
        except ProviderError as exc:
            return ValidationResult(False, str(exc), exc.kind.value)
        return ValidationResult(True, "credentials accepted")

    def _body(self, messages, model, tools, max_tokens, temperature, stream) -> dict[str, Any]:
        system, converted = messages_to_anthropic(messages)
        body: dict[str, Any] = {
            "model": model,
            "messages": converted,
            "max_tokens": max_tokens or self.config.request_options.get("max_tokens", DEFAULT_MAX_TOKENS),
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools_to_anthropic(tools)
        if temperature is not None:
            body["temperature"] = temperature
        if stream:
            body["stream"] = True
        extra = self.config.request_options.get("body")
        if isinstance(extra, dict):
            body.update(extra)
        return body

    def generate_response(
        self, messages, model, *, tools=None, max_tokens=None, temperature=None
    ) -> ChatResponse:
        data = self._json(
            "POST", "messages", self._body(messages, model, tools, max_tokens, temperature, False)
        )
        return self._parse_message(data)

    @staticmethod
    def _usage(raw: Any) -> Usage:
        if not isinstance(raw, dict):
            return Usage()
        return Usage(
            input_tokens=int(raw.get("input_tokens") or 0),
            output_tokens=int(raw.get("output_tokens") or 0),
            cached_tokens=int(raw.get("cache_read_input_tokens") or 0),
        )

    def _parse_message(self, data: Any) -> ChatResponse:
        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            raise ProviderError(
                "Anthropic returned a response without content blocks",
                kind=ErrorKind.MALFORMED_RESPONSE,
                provider=self.name,
            )
        text: list[str] = []
        calls: list[ToolCall] = []
        for block in data["content"]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_use":
                args = block.get("input")
                if isinstance(args, dict):
                    calls.append(
                        ToolCall(str(block.get("id")), str(block.get("name")), args, json.dumps(args))
                    )
                else:
                    calls.append(
                        ToolCall(
                            str(block.get("id")),
                            str(block.get("name")),
                            {},
                            "",
                            "tool input was not an object",
                        )
                    )
        stop = data.get("stop_reason")
        content = "".join(text)
        if stop == "refusal" and not content and not calls:
            raise ProviderError(
                "Anthropic declined to answer", kind=ErrorKind.SAFETY_REFUSAL, provider=self.name
            )
        return ChatResponse(content, calls, self._usage(data.get("usage")), stop, data.get("model"))

    def stream_response(
        self, messages, model, *, tools=None, max_tokens=None, temperature=None, cancel=None
    ) -> Iterator[StreamEvent]:
        body = self._body(messages, model, tools, max_tokens, temperature, True)
        stream = self._open_stream("messages", body)
        text: list[str] = []
        blocks: dict[int, dict[str, Any]] = {}
        usage = Usage()
        stop: str | None = None
        resp_model: str | None = None
        try:
            for _event, payload in iter_sse(stream.iter_lines()):
                if cancel is not None and cancel.is_set():
                    stream.close()
                    break
                try:
                    evt = json.loads(payload)
                except ValueError as exc:
                    raise ProviderError(
                        "Anthropic sent a malformed stream event",
                        kind=ErrorKind.MALFORMED_RESPONSE,
                        provider=self.name,
                    ) from exc
                etype = evt.get("type")
                if etype == "error":
                    raise classify_http_error(_status_for(evt), {}, json.dumps(evt), provider=self.name)
                if etype == "message_start":
                    msg = evt.get("message") or {}
                    resp_model = msg.get("model")
                    usage = self._usage(msg.get("usage"))
                elif etype == "content_block_start":
                    block = evt.get("content_block") or {}
                    blocks[int(evt.get("index", 0))] = {
                        "type": block.get("type"),
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "json": "",
                    }
                elif etype == "content_block_delta":
                    delta = evt.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        piece = str(delta.get("text") or "")
                        text.append(piece)
                        yield StreamEvent("text", piece)
                    elif delta.get("type") == "input_json_delta":
                        slot = blocks.setdefault(
                            int(evt.get("index", 0)), {"type": "tool_use", "id": "", "name": "", "json": ""}
                        )
                        slot["json"] += str(delta.get("partial_json") or "")
                elif etype == "message_delta":
                    stop = (evt.get("delta") or {}).get("stop_reason") or stop
                    out = (evt.get("usage") or {}).get("output_tokens")
                    if out is not None:
                        usage.output_tokens = int(out)
                elif etype == "message_stop":
                    break
        except TransportError as exc:
            raise transport_error(exc, self.name) from exc
        finally:
            stream.close()
        calls = []
        for idx in sorted(blocks):
            slot = blocks[idx]
            if slot["type"] != "tool_use":
                continue
            args, raw, err = parse_tool_arguments(slot["json"])
            calls.append(ToolCall(slot["id"] or f"toolu_{idx}", slot["name"], args, raw, err))
        yield StreamEvent("done", response=ChatResponse("".join(text), calls, usage, stop, resp_model))


def _cap(caps: dict[str, Any], key: str) -> bool | None:
    entry = caps.get(key)
    if isinstance(entry, dict) and isinstance(entry.get("supported"), bool):
        return entry["supported"]
    if isinstance(entry, bool):
        return entry
    return None


def _status_for(evt: dict[str, Any]) -> int:
    etype = str((evt.get("error") or {}).get("type") or "")
    return {
        "authentication_error": 401,
        "permission_error": 403,
        "not_found_error": 404,
        "request_too_large": 413,
        "rate_limit_error": 429,
        "overloaded_error": 529,
        "api_error": 500,
        "billing_error": 402,
    }.get(etype, 400)
