from __future__ import annotations

import shlex
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from workflaskcode.errors import WorkFlaskError
from workflaskcode.security.permissions import filtered_env
from workflaskcode.tools.process import IS_WINDOWS, terminate_tree
from workflaskcode.utils.redact import redact

INSTALL_HINT = "pip install playwright && python3 -m playwright install chromium"


class BrowserUnavailable(WorkFlaskError):
    pass


def playwright_status() -> tuple[bool, str]:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, f"Playwright is not installed. Enable it with: {INSTALL_HINT}"
    return True, "playwright package importable (browser binaries are checked when a page is opened)"


def port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class DevServer:
    def __init__(
        self,
        root: Path,
        command: list[str],
        host: str = "127.0.0.1",
        port: int = 5173,
        startup_timeout: float = 30.0,
    ) -> None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise WorkFlaskError(
                f"refusing to bind a development server to '{host}'",
                hint="use 127.0.0.1; do not expose dev servers publicly",
            )
        self.root = root
        self.command = command
        self.host = host
        self.port = port
        self.startup_timeout = startup_timeout
        self.proc: subprocess.Popen | None = None
        self.log: list[str] = []
        self._reader: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, cancel: threading.Event | None = None) -> str:
        if self.running:
            return self.url
        if port_open(self.host, self.port):
            raise WorkFlaskError(
                f"port {self.port} on {self.host} is already in use",
                hint="stop the other process or change browser.port",
            )
        env = filtered_env({"HOST": self.host, "PORT": str(self.port)}, {"HOST", "PORT"})
        kwargs: dict = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(
                self.command,
                cwd=self.root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                **kwargs,
            )
        except OSError as exc:
            raise WorkFlaskError(f"could not start '{shlex.join(self.command)}': {exc}") from exc
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                self.stop()
                raise WorkFlaskError("cancelled while waiting for the dev server")
            if self.proc.poll() is not None:
                tail = "\n".join(self.log[-15:])
                self.stop()
                raise WorkFlaskError(
                    f"dev server exited with code {self.proc.returncode} before becoming ready\n{tail}"
                )
            if port_open(self.host, self.port):
                return self.url
            time.sleep(0.25)
        tail = "\n".join(self.log[-15:])
        self.stop()
        raise WorkFlaskError(
            f"dev server did not open {self.host}:{self.port} within {self.startup_timeout:.0f}s\n{tail}"
        )

    def _pump(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            self.log.append(redact(line.rstrip()))
            if len(self.log) > 400:
                del self.log[:200]

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            terminate_tree(self.proc)
        self.proc = None


@dataclass
class BrowserReport:
    url: str
    title: str | None = None
    status: int | None = None
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    found: dict[str, bool] = field(default_factory=dict)
    screenshot: str | None = None
    failed_requests: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            (self.status or 0) < 400
            and not self.console_errors
            and not self.page_errors
            and all(self.found.values())
        )

    def render(self) -> str:
        lines = [f"url: {self.url}", f"http status: {self.status}", f"title: {self.title!r}"]
        for selector, present in self.found.items():
            lines.append(f"selector {selector}: {'found' if present else 'MISSING'}")
        lines += (
            [f"console error: {e}" for e in self.console_errors]
            + [f"page error: {e}" for e in self.page_errors]
            + [f"failed request: {e}" for e in self.failed_requests]
        )
        if self.screenshot:
            lines.append(f"screenshot: {self.screenshot}")
        lines.append("result: " + ("PASS" if self.ok else "FAIL"))
        return "\n".join(lines)


def browser_check(
    url: str, *, selectors: list[str] | None = None, screenshot: Path | None = None, timeout: float = 20.0
) -> BrowserReport:
    ok, message = playwright_status()
    if not ok:
        raise BrowserUnavailable(message)
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    report = BrowserReport(url)
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PlaywrightError as exc:
                raise BrowserUnavailable(
                    f"Chromium could not be launched ({str(exc).splitlines()[0]}). Install browsers with: python3 -m playwright install chromium"
                ) from exc
            try:
                page = browser.new_page()
                page.on(
                    "console", lambda m: report.console_errors.append(m.text) if m.type == "error" else None
                )
                page.on("pageerror", lambda e: report.page_errors.append(str(e)))
                page.on("requestfailed", lambda r: report.failed_requests.append(f"{r.method} {r.url}"))
                response = page.goto(url, timeout=timeout * 1000, wait_until="load")
                report.status = response.status if response else None
                report.title = page.title()
                for selector in selectors or []:
                    report.found[selector] = page.query_selector(selector) is not None
                if screenshot is not None:
                    screenshot.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(screenshot), full_page=True)
                    report.screenshot = str(screenshot)
            finally:
                browser.close()
    except PlaywrightError as exc:
        raise WorkFlaskError(f"browser check failed: {str(exc).splitlines()[0]}") from exc
    return report
