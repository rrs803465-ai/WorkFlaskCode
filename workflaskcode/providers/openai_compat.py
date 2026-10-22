from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from workflaskcode.errors import ErrorKind, ModelListingUnsupported, ProviderError
from workflaskcode.http import TransportError
from workflaskcode.providers.base import MAX_PAGES, Provider, iter_sse, parse_tool_arguments
from workflaskcode.providers.errors import classify_body_error, transport_error
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

DEFAULT_OPENAI_BASE = "https://api.openai.com/v1"


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def messages_to_openai(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id or "", "content": m.content})
        elif m.role == "assistant" and m.tool_calls:
            out.append(
                {
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": [
                        {
                            "id": t.id,
                            "type": "function",
                            "function": {
                                "name": t.name,
                                "arguments": t.raw_arguments or json.dumps(t.arguments),
                            },
                        }
                        for t in m.tool_calls
                    ],
                }
            )
        else:
            out.append({"role": m.role, "content": m.content})
    return out


def tools_to_openai(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
        }
        for t in tools
    ]


def parse_usage(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    details = raw.get("prompt_tokens_details") or {}
    cost = raw.get("cost")
    return Usage(
        input_tokens=_int(raw.get("prompt_tokens")) or 0,
        output_tokens=_int(raw.get("completion_tokens")) or 0,
        cached_tokens=_int(details.get("cached_tokens")) or 0 if isinstance(details, dict) else 0,
        cost=float(cost) if isinstance(cost, (int, float)) else None,
    )


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return str(content)


class OpenAICompatProvider(Provider):
    @property
    def base_url(self) -> str:
        return (self.config.base_url or DEFAULT_OPENAI_BASE).rstrip("/")

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def _parse_model(self, raw: dict[str, Any]) -> ModelInfo | None:
        mid = raw.get("id")
        if not isinstance(mid, str) or not mid:
            return None
        ctx = (
            _int(raw.get("context_length"))
            or _int(raw.get("context_window"))
            or _int(raw.get("max_model_len"))
        )
        return ModelInfo(
            id=mid,
            name=str(raw.get("name") or mid),
            provider=self.name,
            description=raw.get("description") if isinstance(raw.get("description"), str) else None,
            context_length=ctx,
            routing=raw.get("owned_by") if isinstance(raw.get("owned_by"), str) else None,
        )

    def list_models(self) -> list[ModelInfo]:
        if not self.config.supports_model_listing:
            raise self._unsupported_listing()
        models: list[ModelInfo] = []
        params = ""
        for _ in range(MAX_PAGES):
            try:
                data = self._json("GET", "models", params=params)
            except ProviderError as exc:
                if exc.status in (404, 405, 501) and not models:
                    raise ModelListingUnsupported(
                        f"{self.name} has no model-list endpoint",
                        kind=ErrorKind.INVALID_REQUEST,
                        status=exc.status,
                        provider=self.name,
                        hint="enter the exact model id manually",
                    ) from exc
                raise
            items = data.get("data") if isinstance(data, dict) else data
            if not isinstance(items, list):
                raise ProviderError(
                    f"{self.name} returned an unexpected model-list shape",
                    kind=ErrorKind.MALFORMED_RESPONSE,
                    provider=self.name,
                )
            for raw in items:
                info = self._parse_model(raw) if isinstance(raw, dict) else None
                if info:
                    models.append(info)
            if isinstance(data, dict) and data.get("has_more") and data.get("last_id"):
                params = f"?after={data['last_id']}"
                continue
            break
        self.remember_models(models)
        return models

    def validate_credentials(self) -> ValidationResult:
        try:
            self.list_models()
        except ModelListingUnsupported:
            return ValidationResult(
                True,
                "endpoint has no model list; credentials can only be verified with a model test",
                inconclusive=True,
            )
        except ProviderError as exc:
            return ValidationResult(False, str(exc), exc.kind.value)
        return ValidationResult(True, "credentials accepted")

    def _body(
        self,
        messages: list[Message],
        model: str,
        tools: list[ToolSpec] | None,
        max_tokens: int | None,
        temperature: float | None,
        stream: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages_to_openai(messages),
            "stream": stream,
        }
        if tools:
            body["tools"] = tools_to_openai(tools)
            body["tool_choice"] = "auto"
        if max_tokens is not None:
            body[self.config.request_options.get("max_tokens_param", "max_tokens")] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        if stream and self.config.request_options.get("stream_usage", True):
            body["stream_options"] = {"include_usage": True}
        extra = self.config.request_options.get("body")
        if isinstance(extra, dict):
            body.update(extra)
        return body

    def generate_response(
        self, messages, model, *, tools=None, max_tokens=None, temperature=None
    ) -> ChatResponse:
        body = self._body(messages, model, tools, max_tokens, temperature, stream=False)
        data = self._json("POST", "chat/completions", body)
        return self._parse_completion(data)

    def _parse_completion(self, data: Any) -> ChatResponse:
        try:
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderError(
                f"{self.name} returned a response without choices",
                kind=ErrorKind.MALFORMED_RESPONSE,
                provider=self.name,
            ) from exc
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = (raw or {}).get("function") or {}
            args, text, err = parse_tool_arguments(fn.get("arguments"))
            calls.append(
                ToolCall(
                    id=str(raw.get("id") or f"call_{len(calls)}"),
                    name=str(fn.get("name") or ""),
                    arguments=args,
                    raw_arguments=text,
                    parse_error=err,
                )
            )
        content = _content_text(message.get("content"))
        finish = choice.get("finish_reason")
        if (finish == "content_filter" or message.get("refusal")) and not content and not calls:
            raise ProviderError(
                f"{self.name} refused or filtered the response",
                kind=ErrorKind.SAFETY_REFUSAL,
                provider=self.name,
            )
        return ChatResponse(
            content=content,
            tool_calls=calls,
            usage=parse_usage(data.get("usage")),
            finish_reason=finish,
            model=data.get("model"),
        )

    def stream_response(
        self, messages, model, *, tools=None, max_tokens=None, temperature=None, cancel=None
    ) -> Iterator[StreamEvent]:
        body = self._body(messages, model, tools, max_tokens, temperature, stream=True)
        stream = self._open_stream("chat/completions", body)
        text_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage = Usage()
        finish: str | None = None
        resp_model: str | None = None
        try:
            for _event, payload in iter_sse(stream.iter_lines()):
                if cancel is not None and cancel.is_set():
                    stream.close()
                    break
                if payload.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError as exc:
                    raise ProviderError(
                        f"{self.name} sent a malformed stream chunk",
                        kind=ErrorKind.MALFORMED_RESPONSE,
                        provider=self.name,
                    ) from exc
                err = classify_body_error(chunk, provider=self.name)
                if err is not None:
                    raise err
                resp_model = chunk.get("model") or resp_model
                if chunk.get("usage"):
                    usage = parse_usage(chunk["usage"])
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    piece = _content_text(delta.get("content"))
                    if piece:
                        text_parts.append(piece)
                        yield StreamEvent("text", piece)
                    for tc in delta.get("tool_calls") or []:
                        idx = int(tc.get("index", 0))
                        slot = calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
        except TransportError as exc:
            raise transport_error(exc, self.name) from exc
        finally:
            stream.close()
        tool_calls = []
        for idx in sorted(calls):
            slot = calls[idx]
            args, text, err = parse_tool_arguments(slot["args"])
            tool_calls.append(ToolCall(slot["id"] or f"call_{idx}", slot["name"], args, text, err))
        final = ChatResponse("".join(text_parts), tool_calls, usage, finish, resp_model)
        if finish == "content_filter" and not final.content and not tool_calls:
            raise ProviderError(
                f"{self.name} filtered the response",
                kind=ErrorKind.SAFETY_REFUSAL,
                provider=self.name,
            )
        yield StreamEvent("done", response=final)
