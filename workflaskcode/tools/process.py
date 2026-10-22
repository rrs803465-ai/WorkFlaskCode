from __future__ import annotations

import atexit
import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from workflaskcode.security.permissions import filtered_env
from workflaskcode.utils.redact import redact

IS_WINDOWS = sys.platform.startswith("win")


@dataclass
class CommandResult:
    command: str
    cwd: str
    exit_code: int | None
    output: str
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False
    duration: float = 0.0
    pid: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled

    def render(self) -> str:
        head = f"$ {self.command}\n(cwd: {self.cwd})\n"
        if self.error:
            return head + f"ERROR: {self.error}"
        status = (
            "TIMED OUT"
            if self.timed_out
            else "CANCELLED"
            if self.cancelled
            else f"exit code {self.exit_code}"
        )
        body = self.output if self.output.strip() else "(no output)"
        trunc = "\n[output truncated]" if self.truncated else ""
        return f"{head}{status} after {self.duration:.1f}s\n{body}{trunc}"


@dataclass
class ProcessTracker:
    _procs: dict[int, subprocess.Popen] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs[proc.pid] = proc

    def remove(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.pop(proc.pid, None)

    def active(self) -> list[int]:
        with self._lock:
            return [pid for pid, p in self._procs.items() if p.poll() is None]

    def kill_all(self) -> int:
        with self._lock:
            procs = list(self._procs.values())
        killed = 0
        for proc in procs:
            if proc.poll() is None:
                terminate_tree(proc)
                killed += 1
        return killed


GLOBAL_TRACKER = ProcessTracker()
atexit.register(GLOBAL_TRACKER.kill_all)


def terminate_tree(proc: subprocess.Popen, grace: float = 2.0) -> None:
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(  # noqa: S603
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            deadline = time.monotonic() + grace
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        with contextlib.suppress(Exception):
            proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)


class _Capture:
    def __init__(self, limit: int) -> None:
        self.half = max(limit // 2, 500)
        self.head = bytearray()
        self.tail: deque[bytes] = deque()
        self.tail_size = 0
        self.dropped = 0

    def feed(self, chunk: bytes) -> None:
        if len(self.head) < self.half:
            take = self.half - len(self.head)
            self.head += chunk[:take]
            chunk = chunk[take:]
        if not chunk:
            return
        self.tail.append(chunk)
        self.tail_size += len(chunk)
        while self.tail_size - len(self.tail[0]) >= self.half:
            old = self.tail.popleft()
            self.tail_size -= len(old)
            self.dropped += len(old)

    def text(self) -> tuple[str, bool]:
        tail = b"".join(self.tail)
        if self.dropped:
            marker = f"\n...[{self.dropped} bytes omitted]...\n".encode()
            data = bytes(self.head) + marker + tail
        else:
            data = bytes(self.head) + tail
        return data.decode("utf-8", errors="replace"), bool(self.dropped)


def run_process(
    *,
    argv: list[str] | None = None,
    shell: str | None = None,
    cwd: Path,
    timeout: float = 120.0,
    max_output: int = 60_000,
    env: dict[str, str] | None = None,
    cancel: threading.Event | None = None,
    tracker: ProcessTracker | None = None,
    extra_env_keep: set[str] | None = None,
) -> CommandResult:
    if (argv is None) == (shell is None):
        raise ValueError("pass exactly one of argv / shell")
    tracker = tracker or GLOBAL_TRACKER
    display = shell if shell is not None else " ".join(argv or [])
    if shell is not None:
        real_argv = ["cmd", "/c", shell] if IS_WINDOWS else ["/bin/sh", "-c", shell]
    else:
        real_argv = list(argv or [])
    child_env = filtered_env(env, extra_env_keep)
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            real_argv,
            cwd=str(cwd),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
    except FileNotFoundError:
        return CommandResult(display, str(cwd), 127, "", error=f"command not found: {real_argv[0]}")
    except (PermissionError, OSError) as exc:
        return CommandResult(display, str(cwd), None, "", error=f"could not start: {exc}")

    tracker.add(proc)
    capture = _Capture(max_output)

    def pump() -> None:
        assert proc.stdout is not None
        try:
            for chunk in iter(lambda: proc.stdout.read1(8192), b""):
                capture.feed(chunk)
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    timed_out = cancelled = False
    try:
        while True:
            try:
                proc.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancel is not None and cancel.is_set():
                cancelled = True
                terminate_tree(proc)
                break
            if time.monotonic() - start > timeout:
                timed_out = True
                terminate_tree(proc)
                break
    finally:
        reader.join(timeout=3)
        tracker.remove(proc)
        for pipe in (proc.stdout,):
            with contextlib.suppress(Exception):
                if pipe:
                    pipe.close()
    text, truncated = capture.text()
    return CommandResult(
        command=display,
        cwd=str(cwd),
        exit_code=proc.returncode,
        output=redact(text),
        timed_out=timed_out,
        cancelled=cancelled,
        truncated=truncated,
        duration=time.monotonic() - start,
        pid=proc.pid,
    )
