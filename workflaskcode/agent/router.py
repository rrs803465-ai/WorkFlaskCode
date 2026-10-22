from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from workflaskcode.config.models import ModelRef, Settings
from workflaskcode.errors import (
    AllModelsExhausted,
    Cancelled,
    ConfigError,
    CredentialError,
    ErrorKind,
    ProviderError,
)
from workflaskcode.providers.base import Provider
from workflaskcode.providers.registry import ProviderRegistry
from workflaskcode.providers.types import ChatResponse, Message, ToolSpec, Usage
from workflaskcode.ui.console import Console
from workflaskcode.usage.tracker import UsageTracker

log = logging.getLogger("workflaskcode.router")

MAX_HONOURED_RETRY_AFTER = 300.0


@dataclass
class RouteResult:
    response: ChatResponse
    ref: ModelRef
    attempts: int
    switched: bool = False
    notes: list[str] = field(default_factory=list)


class _Slots:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sems: dict[str, threading.Semaphore] = {}
        self._waiting: dict[str, int] = {}

    def sem(self, key: str, size: int) -> threading.Semaphore:
        with self._lock:
            if key not in self._sems:
                self._sems[key] = threading.Semaphore(max(size, 1))
            return self._sems[key]

    def acquire(
        self, key: str, size: int, cancel: threading.Event | None, on_wait: Callable[[], None] | None
    ) -> threading.Semaphore:
        sem = self.sem(key, size)
        notified = False
        while not sem.acquire(timeout=0.2):
            if cancel is not None and cancel.is_set():
                raise Cancelled("cancelled while waiting for a free provider slot")
            if not notified and on_wait:
                on_wait()
                notified = True
        return sem


class ModelRouter:
    def __init__(
        self,
        providers: ProviderRegistry,
        settings: Callable[[], Settings],
        tracker: UsageTracker,
        console: Console,
        *,
        sleep: Callable[[float, threading.Event | None], None] | None = None,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.providers = providers
        self.settings = settings
        self.tracker = tracker
        self.console = console
        self._sleep = sleep or self._default_sleep
        self._rng = rng
        self._slots = _Slots()
        self.exhausted_providers: dict[str, str] = {}
        self.last_route: RouteResult | None = None

    @staticmethod
    def _default_sleep(seconds: float, cancel: threading.Event | None) -> None:
        if cancel is not None:
            cancel.wait(seconds)
        else:
            time.sleep(seconds)

    def reset_exhaustion(self) -> None:
        self.exhausted_providers.clear()

    def chain(self, primary: ModelRef | None, *, include_fallbacks: bool = True) -> list[ModelRef]:
        s = self.settings()
        refs: list[ModelRef] = []
        if primary:
            refs.append(primary)
        if include_fallbacks:
            refs += [r for r in s.fallback_models if r not in refs]
        return refs

    def backoff(self, attempt: int, retry_after: float | None) -> float:
        policy = self.settings().retry
        delay = min(policy.max_delay_seconds, policy.base_delay_seconds * (2**attempt))
        delay *= 1 + self._rng() * policy.jitter
        if retry_after is not None:
            delay = max(delay, min(retry_after, MAX_HONOURED_RETRY_AFTER))
        return delay

    def _authorised(self, ref: ModelRef, primary: ModelRef | None, provider: Provider) -> str | None:
        info = provider._models_cache.get(ref.model_id)
        known_paid = info is not None and info.is_free is False
        if ref != primary and known_paid and not provider.config.allow_paid:
            return f"{ref} is a paid model and paid inference is not enabled for provider '{ref.provider}'"
        return None

    def _tool_support(self, provider: Provider, ref: ModelRef) -> bool | None:
        info = provider._models_cache.get(ref.model_id)
        return info.supports_tools if info else None

    def call(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None,
        primary: ModelRef | None,
        task_id: str | None = None,
        channel: str = "main",
        cancel: threading.Event | None = None,
        on_text: Callable[[str], None] | None = None,
        on_reset: Callable[[], None] | None = None,
        max_tokens: int | None = None,
        compact: Callable[[list[Message]], list[Message]] | None = None,
        allow_fallback: bool = True,
        temperature: float | None = None,
    ) -> RouteResult:
        s = self.settings()
        candidates = self.chain(primary, include_fallbacks=allow_fallback)
        if not candidates:
            raise ConfigError("no model is configured", hint="run setup or @changemodel")
        failures: list[str] = []
        skipped_provider: set[str] = set()
        compacted = False
        for index, ref in enumerate(candidates):
            if cancel is not None and cancel.is_set():
                raise Cancelled("cancelled")
            if ref.provider in skipped_provider or ref.provider in self.exhausted_providers:
                failures.append(
                    f"{ref}: skipped, provider '{ref.provider}' is unavailable ({self.exhausted_providers.get(ref.provider, 'earlier failure')})"
                )
                continue
            try:
                provider = self.providers.get(ref.provider)
            except (ConfigError, CredentialError) as exc:
                failures.append(f"{ref}: {exc}")
                continue
            if provider._api_key is None and provider.config.kind.value != "custom":
                failures.append(f"{ref}: no API key configured for provider '{ref.provider}'")
                skipped_provider.add(ref.provider)
                continue
            refusal = self._authorised(ref, primary, provider)
            if refusal:
                failures.append(refusal)
                continue
            if tools and self._tool_support(provider, ref) is False:
                failures.append(
                    f"{ref}: skipped, the catalogue says this model does not support tool calling"
                )
                continue
            self.tracker.check_budget(s.limits, task_id=task_id, provider=ref.provider, model=ref.model_id)
            attempt = 0
            working = list(messages)
            while attempt < s.retry.max_attempts:
                attempt += 1
                if cancel is not None and cancel.is_set():
                    raise Cancelled("cancelled")
                started = time.monotonic()
                gslot = pslot = None
                try:
                    gslot = self._slots.acquire(
                        "__global__", s.limits.max_parallel_requests, cancel, self._queued_notice(channel)
                    )
                    pslot = self._slots.acquire(
                        f"p:{ref.provider}",
                        provider.config.max_concurrency,
                        cancel,
                        self._queued_notice(channel),
                    )
                    response = self._invoke(
                        provider, ref, working, tools, on_text, on_reset, max_tokens, cancel, temperature
                    )
                except Cancelled:
                    raise
                except Exception as exc:
                    err = provider.classify_error(exc)
                    self.tracker.record(
                        ref.provider,
                        ref.model_id,
                        Usage(),
                        duration_ms=int((time.monotonic() - started) * 1000),
                        task_id=task_id,
                        error_kind=err.kind.value,
                        channel=channel,
                    )
                    if cancel is not None and cancel.is_set():
                        raise Cancelled("cancelled") from exc
                    log.warning(
                        "model call failed: %s kind=%s attempt=%d channel=%s detail=%s",
                        ref,
                        err.kind.value,
                        attempt,
                        channel,
                        str(err)[:300],
                    )
                    decision = self._decide(
                        err, attempt, s.retry.max_attempts, compact is not None and not compacted
                    )
                    if decision == "retry":
                        wait = self.backoff(attempt - 1, err.retry_after)
                        self.console.warn(
                            f"{ref}: {err.kind.value}; retrying in {wait:.1f}s (attempt {attempt}/{s.retry.max_attempts})"
                        )
                        self._sleep(wait, cancel)
                        continue
                    if decision == "compact" and compact is not None:
                        compacted = True
                        working = compact(working)
                        self.console.warn(f"{ref}: context too large; compacted history and retrying once")
                        attempt -= 1
                        continue
                    failures.append(f"{ref}: {err.kind.value} - {str(err)[:200]}")
                    log.info("giving up on %s: %s", ref, err.kind.value)
                    if err.kind is ErrorKind.INVALID_REQUEST:
                        skipped_provider.add(ref.provider)
                        if all(r.provider == ref.provider for r in candidates[index + 1 :]):
                            raise err
                    elif err.account_wide:
                        self.exhausted_providers[ref.provider] = err.kind.value
                        skipped_provider.add(ref.provider)
                    break
                finally:
                    if pslot is not None:
                        pslot.release()
                    if gslot is not None:
                        gslot.release()
                duration = int((time.monotonic() - started) * 1000)
                self.tracker.record(
                    ref.provider,
                    ref.model_id,
                    response.usage,
                    duration_ms=duration,
                    task_id=task_id,
                    channel=channel,
                )
                result = RouteResult(response, ref, attempt, switched=index > 0 or ref != primary)
                if result.switched:
                    self.console.warn(f"using fallback model {ref}")
                    result.notes.append("fallback")
                self.last_route = result
                return result
        raise AllModelsExhausted("every configured model failed", failures)

    def _queued_notice(self, channel: str) -> Callable[[], None]:
        return lambda: self.console.dim(f"  {channel}: waiting for a free provider slot (concurrency limit)")

    @staticmethod
    def _decide(err: ProviderError, attempt: int, max_attempts: int, can_compact: bool) -> str:
        if err.kind is ErrorKind.CONTEXT_OVERFLOW and can_compact:
            return "compact"
        if (
            err.kind is ErrorKind.RATE_LIMIT_MODEL
            and err.retry_after is not None
            and err.retry_after > MAX_HONOURED_RETRY_AFTER
        ):
            return "next"
        if err.transient and attempt < max_attempts:
            return "retry"
        return "next"

    def _invoke(
        self, provider, ref, messages, tools, on_text, on_reset, max_tokens, cancel, temperature
    ) -> ChatResponse:
        use_stream = on_text is not None and self.settings().ui.stream
        if not use_stream:
            return provider.generate_response(
                messages, ref.model_id, tools=tools, max_tokens=max_tokens, temperature=temperature
            )
        emitted = False
        final: ChatResponse | None = None
        try:
            for event in provider.stream_response(
                messages,
                ref.model_id,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
                cancel=cancel,
            ):
                if event.kind == "text":
                    emitted = True
                    on_text(event.text)
                elif event.kind == "done":
                    final = event.response
        except ProviderError:
            if emitted and on_reset:
                on_reset()
            raise
        if cancel is not None and cancel.is_set():
            raise Cancelled("cancelled")
        if final is None:
            raise ProviderError(
                "stream ended without a final message",
                kind=ErrorKind.MALFORMED_RESPONSE,
                provider=provider.name,
            )
        return final
