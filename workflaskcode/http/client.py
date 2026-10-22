from __future__ import annotations

import json
import socket
import ssl
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

USER_AGENT = "workflaskcode/0.1 (+https://github.com/OWNER/WorkFlaskCode)"


class TransportError(Exception):
    def __init__(self, message: str, *, timeout: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes = b""

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass
class StreamResponse:
    status: int
    headers: dict[str, str]
    error_body: bytes | None = None
    _resp: Any = field(default=None, repr=False)
    _closed: threading.Event = field(default_factory=threading.Event, repr=False)

    def iter_lines(self) -> Iterator[str]:
        if self._resp is None:
            return
        try:
            for raw in self._resp:
                if self._closed.is_set():
                    return
                yield raw.decode("utf-8", errors="replace").rstrip("\r\n")
        except TimeoutError as exc:
            raise TransportError("stream read timed out", timeout=True) from exc
        except (OSError, ssl.SSLError, ValueError) as exc:
            if self._closed.is_set():
                return
            raise TransportError(f"stream interrupted: {exc}") from exc

    def close(self) -> None:
        self._closed.set()
        if self._resp is not None:
            try:
                self._resp.close()
            except Exception:
                pass


class HttpClient(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        timeout: float = 60.0,
    ) -> HttpResponse: ...

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        timeout: float = 60.0,
    ) -> StreamResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect())


def _headers_dict(headers: Any) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()} if headers else {}


class UrllibClient:
    def __init__(self) -> None:
        self._opener = _build_opener()

    def _prepare(self, method, url, headers, json_body):
        if not url.lower().startswith(("http://", "https://")):
            raise TransportError(f"refusing non-http(s) URL: {url.split(':', 1)[0]}:")
        data = None
        hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        return urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())  # noqa: S310

    def _open(self, req, timeout):
        try:
            return self._opener.open(req, timeout=timeout), None
        except urllib.error.HTTPError as exc:
            return None, exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise TransportError("request timed out", timeout=True) from exc
            raise TransportError(f"connection failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise TransportError("request timed out", timeout=True) from exc
        except (OSError, ssl.SSLError, ValueError) as exc:
            raise TransportError(f"connection failed: {exc}") from exc

    def request(self, method, url, *, headers=None, json_body=None, timeout=60.0) -> HttpResponse:
        req = self._prepare(method, url, headers, json_body)
        resp, err = self._open(req, timeout)
        try:
            if err is not None:
                return HttpResponse(err.code, _headers_dict(err.headers), _safe_read(err))
            assert resp is not None
            body = resp.read()
            return HttpResponse(resp.status, _headers_dict(resp.headers), body)
        except TimeoutError as exc:
            raise TransportError("response read timed out", timeout=True) from exc
        except (OSError, ssl.SSLError) as exc:
            raise TransportError(f"connection failed: {exc}") from exc
        finally:
            if resp is not None:
                resp.close()
            if err is not None:
                err.close()

    def stream(self, method, url, *, headers=None, json_body=None, timeout=60.0) -> StreamResponse:
        hdrs = dict(headers or {})
        hdrs.setdefault("Accept", "text/event-stream")
        req = self._prepare(method, url, hdrs, json_body)
        resp, err = self._open(req, timeout)
        if err is not None:
            try:
                return StreamResponse(err.code, _headers_dict(err.headers), _safe_read(err))
            finally:
                err.close()
        assert resp is not None
        if not 200 <= resp.status < 300:
            body = _safe_read(resp)
            resp.close()
            return StreamResponse(resp.status, _headers_dict(resp.headers), body)
        return StreamResponse(resp.status, _headers_dict(resp.headers), None, _resp=resp)


def _safe_read(handle: Any, limit: int = 200_000) -> bytes:
    try:
        return handle.read(limit)
    except Exception:
        return b""
