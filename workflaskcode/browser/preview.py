from __future__ import annotations

import shlex
import socket
import subprocess
import threading
import time
from pathlib import Path

from workflaskcode.errors import WorkFlaskError
from workflaskcode.security.permissions import filtered_env
from workflaskcode.tools.process import IS_WINDOWS, terminate_tree
from workflaskcode.utils.redact import redact


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


SERVERS: dict[str, DevServer] = {}


def infer_port(argv: list[str], default: int = 0) -> int:
    if default:
        return default
    for token in argv:
        if token.isdigit() and 1024 <= int(token) <= 65535:
            return int(token)
        if token.startswith("--port=") and token[7:].isdigit():
            return int(token[7:])
    joined = " ".join(argv)
    if "vite" in joined:
        return 5173
    if "flask" in joined:
        return 5000
    if "uvicorn" in joined or "http.server" in joined or "manage.py" in joined:
        return 8000
    return 3000


def stop_all() -> int:
    stopped = 0
    for server in list(SERVERS.values()):
        if server.running:
            server.stop()
            stopped += 1
    SERVERS.clear()
    return stopped
