from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any

from workflaskcode.config.models import ProviderConfig
from workflaskcode.errors import ErrorKind, ModelListingUnsupported, ProviderError
from workflaskcode.http import HttpClient, HttpResponse, TransportError, UrllibClient
from workflaskcode.providers.errors import (
    classify_body_error,
    classify_http_error,
    transport_error,
)
from workflaskcode.providers.types import (
    Capabilities,
    ChatResponse,
    Message,
    ModelInfo,
    ProviderUsageInfo,
    StreamEvent,
    ToolCall,
    ToolSpec,
    ValidationResult,
)

MAX_PAGES = 50

_PROBE_TOOL = ToolSpec(
    name="echo",
    description="Echo back a short string.",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
)


def parse_tool_arguments(raw: str | dict | None) -> tuple[dict[str, Any], str, str | None]:
    if isinstance(raw, dict):
        return raw, json.dumps(raw), None
    text = raw or ""
    if not text.strip():
        return {}, text, None
    try:
        value = json.loads(text)
    except ValueError as exc:
        return {}, text, f"tool arguments were not valid JSON: {exc}"
    if not isinstance(value, dict):
        return {}, text, "tool arguments must be a JSON object"
    return value, text, None


class Provider(ABC):
    protocol_supports_tools: bool = True

    def __init__(
        self,
        config: ProviderConfig,
        api_key: str | None,
        http: HttpClient | None = None,
    ) -> None:
        self.config = config
        self.name = config.name
        self._api_key = api_key
        self.http: HttpClient = http or UrllibClient()
        self._models_cache: dict[str, ModelInfo] = {}
        self._lock = threading.Lock()

    @property
    @abstractmethod
    def base_url(self) -> str: ...

    @abstractmethod
    def _auth_headers(self) -> dict[str, str]: ...

    @abstractmethod
    def list_models(self) -> list[ModelInfo]: ...

    @abstractmethod
    def validate_credentials(self) -> ValidationResult: ...

    @abstractmethod
    def generate_response(
        self,
        messages: list[Message],
        model: str,
        *,
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ChatResponse: ...

    @abstractmethod
    def stream_response(
        self,
        messages: list[Message],
        model: str,
        *,
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        cancel: threading.Event | None = None,
    ) -> Iterator[StreamEvent]: ...

    def classify_error(self, exc: Exception) -> ProviderError:
        if isinstance(exc, ProviderError):
            return exc
        if isinstance(exc, TransportError):
            return transport_error(exc, self.name)
        return ProviderError(
            f"unexpected {type(exc).__name__}: {exc}", kind=ErrorKind.UNKNOWN, provider=self.name
        )

    def get_usage_information(self) -> ProviderUsageInfo | None:
        return None

    def get_model_capabilities(self, model_id: str) -> Capabilities:
        info = self._models_cache.get(model_id)
        if info is not None:
            caps = info.capabilities()
            if caps.streaming is None:
                caps.streaming = True if self.protocol_supports_tools else None
            return caps
        return Capabilities(notes=["model not present in the fetched catalogue; capabilities unknown"])

    def validate_model(self, model_id: str) -> ValidationResult:
        try:
            resp = self.generate_response(
                [Message("user", "Reply with the single word: ok")],
                model_id,
                max_tokens=16,
                temperature=0,
            )
        except ProviderError as exc:
            return ValidationResult(False, str(exc), exc.kind.value)
        if not resp.content.strip() and not resp.tool_calls:
            return ValidationResult(False, "model returned an empty response", "malformed_response")
        return ValidationResult(True, f"responded ({resp.usage.output_tokens} output tokens)")

    def perform_tool_call(
        self,
        messages: list[Message],
        model: str,
        tools: list[ToolSpec],
        *,
        max_tokens: int | None = None,
    ) -> list[ToolCall]:
        resp = self.generate_response(messages, model, tools=tools, max_tokens=max_tokens)
        return resp.tool_calls

    def test_tool_calling(self, model_id: str) -> ValidationResult:
        try:
            calls = self.perform_tool_call(
                [Message("user", "Call the echo tool with text 'ping'. Do not answer in prose.")],
                model_id,
                [_PROBE_TOOL],
                max_tokens=64,
            )
        except ProviderError as exc:
            if exc.kind is ErrorKind.UNSUPPORTED_TOOLS:
                return ValidationResult(False, "model/endpoint rejected tool calling", exc.kind.value)
            return ValidationResult(False, str(exc), exc.kind.value, inconclusive=True)
        if calls and not calls[0].parse_error:
            return ValidationResult(True, "model emitted a well-formed tool call")
        return ValidationResult(False, "model did not emit a usable tool call", "unsupported_tools")

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(self.config.extra_headers)
        headers.update(self._auth_headers())
        if extra:
            headers.update(extra)
        return headers

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _send(self, method: str, path: str, body: Any = None, params: str = "") -> HttpResponse:
        try:
            return self.http.request(
                method,
                self._url(path) + params,
                headers=self._headers(),
                json_body=body,
                timeout=self.config.timeout_seconds,
            )
        except TransportError as exc:
            raise transport_error(exc, self.name) from exc

    def _json(self, method: str, path: str, body: Any = None, params: str = "") -> Any:
        resp = self._send(method, path, body, params)
        if not resp.ok:
            raise classify_http_error(resp.status, resp.headers, resp.body, provider=self.name)
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(
                f"{self.name} returned non-JSON content",
                kind=ErrorKind.MALFORMED_RESPONSE,
                status=resp.status,
                provider=self.name,
            ) from exc
        body_err = classify_body_error(data, provider=self.name)
        if body_err is not None:
            raise body_err
        return data

    def _open_stream(self, path: str, body: dict[str, Any]):
        try:
            stream = self.http.stream(
                "POST",
                self._url(path),
                headers=self._headers(),
                json_body=body,
                timeout=self.config.timeout_seconds,
            )
        except TransportError as exc:
            raise transport_error(exc, self.name) from exc
        if not 200 <= stream.status < 300:
            raise classify_http_error(
                stream.status, stream.headers, stream.error_body or b"", provider=self.name
            )
        return stream

    def remember_models(self, models: list[ModelInfo]) -> None:
        with self._lock:
            self._models_cache = {m.id: m for m in models}

    def _unsupported_listing(self) -> ModelListingUnsupported:
        return ModelListingUnsupported(
            f"{self.name} does not expose a model list",
            kind=ErrorKind.INVALID_REQUEST,
            provider=self.name,
            hint="enter the exact model id manually",
        )


def iter_sse(lines: Iterator[str]) -> Iterator[tuple[str | None, str]]:
    event: str | None = None
    data: list[str] = []
    for line in lines:
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = None, []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
    if data:
        yield event, "\n".join(data)
