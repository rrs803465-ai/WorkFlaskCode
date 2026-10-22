from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from workflaskcode.ui.ansi import THEMES, Painter, clip_ansi, detect_caps, visible_len
from workflaskcode.ui.render import MarkdownStream, Renderer


class Spinner:
    def __init__(self, console: Console, text: str) -> None:
        self.console = console
        self.text = text
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start = 0.0
        self.active = False

    def update(self, text: str) -> None:
        self.text = text

    def _frame(self, i: int) -> str:
        frames = self.console.r.c["spin"]
        c = self.console
        elapsed = time.monotonic() - self._start
        spin = c.p.paint(frames[i % len(frames)], "brand", bold=True)
        return f"{spin} {c.p.paint(self.text, 'text')} {c.p.paint(f'{elapsed:4.1f}s', 'dim')}"

    def _run(self) -> None:
        i = 0
        while not self._stop.is_set():
            with self.console.lock:
                if self.console._spinner is self:
                    line = clip_ansi(self._frame(i), self.console.caps.width - 1)
                    self.console.stream.write("\r\x1b[2K" + line)
                    self.console.stream.flush()
            i += 1
            self._stop.wait(0.08)

    def __enter__(self) -> Spinner:
        c = self.console
        self._start = time.monotonic()
        if not c.caps.tty or c.caps.color == 0 or c.managed_prompt:
            c.line(c.p.paint(f"{c.r.c['ell']} {self.text}", "dim"))
            return self
        self.active = True
        with c.lock:
            c._spinner = self
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        if not self.active:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        with self.console.lock:
            self.console._spinner = None
            self.console.stream.write("\r\x1b[2K")
            self.console.stream.flush()
        self.active = False


class StreamWriter:
    def __init__(self, console: Console, source: str, label: str) -> None:
        self.console = console
        gutter, token = (
            (console.r.c["g_side"], "side") if source == "side" else (console.r.c["g_main"], "agent")
        )
        self.md = MarkdownStream(console.r, gutter, token)
        self.source = source
        self.token = token
        self.gutter = gutter
        self.label = label
        self.started = False
        self.text: list[str] = []

    def _header(self) -> None:
        c = self.console
        name = "btw" if self.source == "side" else "agent"
        head = c.p.paint(self.gutter, self.token) + " " + c.p.paint(name, self.token, bold=True)
        if self.label:
            head += c.p.paint(f"  {c.r.c['dot']}  {self.label}", "dim")
        c.line(head)
        self.started = True

    def write(self, chunk: str) -> None:
        if not chunk:
            return
        self.text.append(chunk)
        lines = self.md.feed(chunk)
        if lines:
            if not self.started:
                self._header()
            self.console.lines(lines)

    def reset(self) -> None:
        lines = self.md.flush()
        if lines:
            if not self.started:
                self._header()
            self.console.lines(lines)
        if self.started:
            self.console.dim("  response interrupted; restarting on retry")
        self.md = MarkdownStream(self.console.r, self.gutter, self.token)
        self.text = []
        self.started = False

    def close(self) -> str:
        lines = self.md.flush()
        if lines:
            if not self.started:
                self._header()
            self.console.lines(lines)
        return "".join(self.text)


class Console:
    def __init__(
        self,
        stream=None,
        *,
        theme: str = "auto",
        plain: bool = False,
        verbose: bool = False,
        env: dict[str, str] | None = None,
    ) -> None:
        self.stream = stream or sys.stdout
        self.caps = detect_caps(self.stream, env, force_plain=plain)
        name = theme if theme in THEMES else "dark"
        if theme == "plain":
            self.caps = type(self.caps)(
                0, self.caps.unicode, self.caps.width, self.caps.height, self.caps.tty
            )
        self.p = Painter(self.caps, THEMES[name])
        self.r = Renderer(self.p, self.caps)
        self.verbose = verbose
        self.lock = threading.RLock()
        self._spinner: Spinner | None = None
        self._prompt: str | None = None
        self._buffer_getter: Callable[[], str] = lambda: ""
        self.managed_prompt = False
        self.transcript: list[str] = []

    def set_theme(self, name: str) -> None:
        if name == "plain":
            self.caps = type(self.caps)(
                0, self.caps.unicode, self.caps.width, self.caps.height, self.caps.tty
            )
        else:
            self.caps = detect_caps(self.stream)
        self.p = Painter(self.caps, THEMES.get(name, THEMES["dark"]))
        self.r = Renderer(self.p, self.caps)

    def bind_prompt(self, text: str | None, buffer_getter: Callable[[], str] | None = None) -> None:
        with self.lock:
            self._prompt = text
            if buffer_getter:
                self._buffer_getter = buffer_getter

    def write(self, text: str) -> None:
        with self.lock:
            from workflaskcode.ui.ansi import strip_ansi

            self.transcript.append(strip_ansi(text))
            if len(self.transcript) > 4000:
                del self.transcript[:1000]
            clear = ""
            redraw = ""
            if self.caps.tty and not self.managed_prompt:
                if self._spinner is not None:
                    clear = "\r\x1b[2K"
                elif self._prompt is not None:
                    clear = "\r\x1b[2K"
                    redraw = self._prompt + self._buffer_getter()
            try:
                self.stream.write(clear + text + redraw)
                self.stream.flush()
            except (BrokenPipeError, ValueError):
                pass
            except UnicodeEncodeError:
                self.stream.write(clear + text.encode("ascii", "replace").decode("ascii") + redraw)
                self.stream.flush()

    def line(self, text: str = "") -> None:
        self.write(text + "\n")

    def lines(self, lines: Iterable[str]) -> None:
        self.write("".join(ln + "\n" for ln in lines))

    def clear(self) -> None:
        if self.caps.tty:
            self.write("\x1b[2J\x1b[H")

    def spinner(self, text: str) -> Spinner:
        return Spinner(self, text)

    def stream_writer(self, source: str = "main", label: str = "") -> StreamWriter:
        return StreamWriter(self, source, label)

    def markdown(self, text: str, source: str = "main", label: str = "") -> None:
        writer = self.stream_writer(source, label)
        writer.write(text if text.endswith("\n") else text + "\n")
        writer.close()

    def info(self, msg: str) -> None:
        self.line(f"{self.p.paint(self.r.c['dot'], 'info')} {msg}")

    def ok(self, msg: str) -> None:
        self.line(f"{self.p.paint(self.r.c['ok'], 'ok', bold=True)} {msg}")

    def warn(self, msg: str) -> None:
        self.line(f"{self.p.paint(self.r.c['warn'], 'warn', bold=True)} {self.p.paint(msg, 'warn')}")

    def dim(self, msg: str) -> None:
        self.line(self.p.paint(msg, "dim"))

    def error(self, msg: str, hint: str | None = None) -> None:
        body = [self.p.paint(msg, "err", bold=True)]
        if hint:
            body.append(self.p.paint(f"{self.r.c['arrow']} {hint}", "muted"))
        self.lines(self.r.box(body, title="error", token="err", title_token="err"))

    def heading(self, title: str, subtitle: str | None = None) -> None:
        text = self.p.paint(title, "accent", bold=True)
        if subtitle:
            text += self.p.paint(f"  {subtitle}", "dim")
        self.line(text)

    def panel(self, title: str, body: Sequence[str], token: str = "border") -> None:
        self.lines(self.r.box(body, title=title, token=token))

    def kv(self, pairs: Sequence[tuple[str, str]], title: str | None = None) -> None:
        body = self.r.kv(pairs)
        if title:
            self.lines(self.r.box(body, title=title))
        else:
            self.lines(body)

    def table(self, headers: Sequence[str], rows: Sequence[Sequence[str]], **kw: Any) -> None:
        self.lines(self.r.table(headers, rows, **kw))

    def rule(self, label: str | None = None) -> None:
        self.line(self.r.rule(label))

    def diff(self, text: str, max_lines: int = 400) -> None:
        if not text.strip():
            self.dim("no changes")
            return
        self.lines(self.r.diff(text, max_lines=max_lines))

    def tool(
        self, name: str, summary: str, *, status: str = "ok", ms: float | None = None, note: str = ""
    ) -> None:
        icons = {
            "ok": (self.r.c["ok"], "ok"),
            "err": (self.r.c["err"], "err"),
            "run": (self.r.c["run"], "info"),
            "skip": (self.r.c["warn"], "warn"),
        }
        icon, token = icons.get(status, icons["ok"])
        head = f"  {self.p.paint(icon, token, bold=True)} {self.p.paint(name, 'tool', bold=True)}"
        tail = ""
        if summary:
            tail += " " + self.p.paint(clip_ansi(summary, max(self.caps.width - 30, 20)), "muted")
        if ms is not None and status != "run":
            tail += " " + self.p.paint(f"{ms:.0f}ms" if ms < 1000 else f"{ms / 1000:.1f}s", "dim")
        if note:
            tail += " " + self.p.paint(f"{self.r.c['dot']} {note}", "dim")
        self.line(head + tail)

    def file_event(self, op: str, path: str, added: int = 0, removed: int = 0) -> None:
        sym, token = {
            "create": ("+", "add"),
            "modify": ("~", "warn"),
            "delete": ("-", "del"),
            "move": (self.r.c["arrow"], "info"),
        }.get(op, ("?", "muted"))
        counts = ""
        if added or removed:
            counts = " " + self.p.paint(f"+{added}", "add") + " " + self.p.paint(f"-{removed}", "del")
        self.line(f"    {self.p.paint(sym, token, bold=True)} {self.p.paint(path, 'text')}{counts}")

    def system(self, msg: str) -> None:
        self.line(self.p.paint(f"{self.r.c['star']} {msg}", "dim"))

    def task_list(self, tasks: Sequence[Any]) -> None:
        marks = {
            "pending": ("○", "muted"),
            "in_progress": ("◐", "brand"),
            "completed": ("●", "ok"),
            "failed": ("✗", "err"),
            "blocked": ("◼", "warn"),
            "cancelled": ("⊘", "dim"),
        }
        asc = {
            "pending": "o",
            "in_progress": "~",
            "completed": "x",
            "failed": "!",
            "blocked": "#",
            "cancelled": "-",
        }
        if not tasks:
            self.dim("no tasks yet")
            return
        done = sum(1 for t in tasks if t.status.value == "completed")
        self.line(
            f"{self.p.paint('tasks', 'accent', bold=True)} {self.r.bar(done, len(tasks), 14)} "
            f"{self.p.paint(f'{done}/{len(tasks)}', 'muted')}"
        )
        for t in tasks:
            sym, token = marks[t.status.value]
            if not self.caps.unicode:
                sym = asc[t.status.value]
            extra = ""
            if t.status.value == "completed" and not t.verified:
                extra = self.p.paint("  (unverified)", "warn")
            elif t.status.value in ("failed", "blocked") and t.failure:
                extra = self.p.paint(f"  {t.failure[:70]}", "dim")
            desc = self.p.paint(t.description, "text" if t.status.value != "completed" else "muted")
            self.line(
                f"  {self.p.paint(sym, token, bold=True)} {self.p.paint(str(t.ordinal) + '.', 'dim')} {desc}{extra}"
            )

    def status_line(self, info: dict[str, Any]) -> str:
        p, c = self.p, self.r.c
        sep = p.paint(f" {c['sep']} ", "border")
        parts: list[tuple[int, str]] = []
        mode = info.get("mode", "coding")
        parts.append((0, p.pill("CHAT" if mode == "chat" else "CODE", "side" if mode == "chat" else "brand")))
        if info.get("working"):
            parts.append((1, p.paint(f"{c['run']} working {info['working']}", "brand", bold=True)))
        if info.get("pending_approval"):
            parts.append((1, p.paint("approval needed", "warn", bold=True)))
        project = info.get("project")
        if project:
            seg = p.paint(project, "text", bold=True)
            if info.get("branch"):
                seg += p.paint(f" {c['branch']} {info['branch']}", "muted")
                if info.get("dirty"):
                    seg += p.paint(f" {c['dirty']}{info['dirty']}", "warn")
            parts.append((2, seg))
        if info.get("model"):
            parts.append((3, p.paint(str(info["model"]), "accent")))
        total = info.get("tasks_total", 0)
        if total:
            done = info.get("tasks_done", 0)
            parts.append((4, self.r.bar(done, total, 8) + p.paint(f" {done}/{total}", "muted")))
        if info.get("side_chats"):
            parts.append((4, p.paint(f"btw x{info['side_chats']}", "side")))
        if info.get("permissions"):
            tone = {
                "read_only": "warn",
                "approve_all": "info",
                "approve_risky": "muted",
                "restricted_autonomous": "err",
            }.get(info["permissions"], "muted")
            parts.append((5, p.paint(info["permissions"].replace("_", " "), tone)))
        if info.get("cost") is not None:
            parts.append((6, p.paint(f"${info['cost']:.4f}", "dim")))
        limit = self.caps.width - 1
        while len(parts) > 1 and visible_len(sep.join(t for _, t in parts)) > limit:
            drop = max(range(len(parts)), key=lambda i: parts[i][0])
            parts.pop(drop)
        return clip_ansi(sep.join(t for _, t in parts), limit)

    def prompt_text(self, *, approving: bool = False, readline_safe: bool = False) -> str:
        c = self.r.c
        if approving:
            core = (
                self.p.paint("approve", "warn", bold=True)
                + self.p.paint(" [y/n/a] ", "dim")
                + self.p.paint(c["prompt"], "warn", bold=True)
            )
        else:
            core = self.p.paint(c["prompt"], "brand", bold=True)
        text = core + " "
        if readline_safe and self.caps.color:
            import re

            text = re.sub(r"(\x1b\[[0-9;?]*[A-Za-z])", "\001\\1\002", text)
        return text

    def approval_panel(self, request: Any) -> list[str]:
        risk_name = getattr(request.risk, "name", str(request.risk)).lower()
        token = {
            "read": "ok",
            "project": "info",
            "modify": "warn",
            "network": "warn",
            "destructive": "err",
            "forbidden": "err",
        }.get(risk_name, "warn")
        body = [self.p.paint(request.summary, "text", bold=True)]
        if request.detail:
            body += [self.p.paint(ln, "muted") for ln in self.r.wrap(request.detail, self.r.width - 6)]
        if request.cwd:
            body.append(self.p.paint("cwd  ", "dim") + request.cwd)
        for effect in request.effects:
            body.append(self.p.paint(f"{self.r.c['warn']} {effect}", token))
        body.append("")
        body.append(
            self.p.paint("y", "ok", bold=True)
            + " approve once   "
            + self.p.paint("a", "info", bold=True)
            + " approve & remember (session)   "
            + self.p.paint("n", "err", bold=True)
            + " deny"
        )
        return self.r.box(
            body, title=f"approval needed {self.r.c['dot']} {risk_name}", token=token, title_token=token
        )

    def show_banner(self, version: str, pairs: Sequence[tuple[str, str]], notes: Sequence[str] = ()) -> None:
        self.line()
        self.lines(self.r.banner(version))
        self.line()
        self.lines(self.r.box(self.r.kv(pairs), title="session"))
        for note in notes:
            self.line(f"{self.p.paint(self.r.c['ok'], 'ok')} {self.p.paint(note, 'muted')}")
        self.line(
            self.p.paint(
                "type @help for commands  ·  @btw <question> asks without interrupting work  ·  @exit quits",
                "dim",
            )
        )
        self.line()
