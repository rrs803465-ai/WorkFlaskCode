from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from workflaskcode.ui.ansi import clip_ansi, visible_len
from workflaskcode.ui.console import Console

T = TypeVar("T")

KEY_UP, KEY_DOWN, KEY_PGUP, KEY_PGDN, KEY_ENTER, KEY_ESC, KEY_BACKSPACE, KEY_HOME, KEY_END = (
    "up",
    "down",
    "pgup",
    "pgdn",
    "enter",
    "esc",
    "backspace",
    "home",
    "end",
)


def filter_items(items: Sequence[T], query: str, text_of: Callable[[T], str]) -> list[T]:
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return list(items)
    scored: list[tuple[int, int, T]] = []
    for idx, item in enumerate(items):
        hay = text_of(item).lower()
        if all(t in hay for t in terms):
            rank = min(hay.find(t) for t in terms)
            scored.append((rank, idx, item))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [i for _, _, i in scored]


def interactive_ok() -> bool:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    if os.name == "nt":
        return True
    try:
        import termios  # noqa: F401
        import tty  # noqa: F401

        return True
    except ImportError:
        return False


def _decode_keys(data: bytes) -> list[str]:
    keys: list[str] = []
    text = data.decode("utf-8", errors="ignore")
    i = 0
    seqs = {
        "\x1b[A": KEY_UP,
        "\x1b[B": KEY_DOWN,
        "\x1bOA": KEY_UP,
        "\x1bOB": KEY_DOWN,
        "\x1b[5~": KEY_PGUP,
        "\x1b[6~": KEY_PGDN,
        "\x1b[H": KEY_HOME,
        "\x1b[F": KEY_END,
        "\x1b[1~": KEY_HOME,
        "\x1b[4~": KEY_END,
    }
    while i < len(text):
        matched = False
        for seq, name in seqs.items():
            if text.startswith(seq, i):
                keys.append(name)
                i += len(seq)
                matched = True
                break
        if matched:
            continue
        ch = text[i]
        i += 1
        if ch == "\x1b":
            keys.append(KEY_ESC)
        elif ch in ("\r", "\n"):
            keys.append(KEY_ENTER)
        elif ch in ("\x7f", "\b"):
            keys.append(KEY_BACKSPACE)
        elif ch == "\x03":
            keys.append(KEY_ESC)
        elif ch == "\x15":
            keys.append("clear")
        elif ch == "\x0e":
            keys.append(KEY_DOWN)
        elif ch == "\x10":
            keys.append(KEY_UP)
        elif ch.isprintable():
            keys.append(ch)
    return keys


def _read_keys_posix(fd: int) -> list[str]:
    import select

    data = os.read(fd, 64)
    if data == b"\x1b":
        ready, _, _ = select.select([fd], [], [], 0.03)
        if ready:
            data += os.read(fd, 64)
    return _decode_keys(data)


def _read_keys_windows() -> list[str]:
    import msvcrt

    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        code = msvcrt.getwch()
        return [
            {"H": KEY_UP, "P": KEY_DOWN, "I": KEY_PGUP, "Q": KEY_PGDN, "G": KEY_HOME, "O": KEY_END}.get(
                code, ""
            )
        ]
    return _decode_keys(ch.encode("utf-8"))


class PickerState:
    def __init__(self, items: Sequence[Any], text_of: Callable[[Any], str], page: int) -> None:
        self.all = list(items)
        self.text_of = text_of
        self.page = page
        self.query = ""
        self.view = list(items)
        self.cursor = 0
        self.top = 0

    def refilter(self) -> None:
        self.view = filter_items(self.all, self.query, self.text_of)
        self.cursor = 0
        self.top = 0

    def move(self, delta: int) -> None:
        if not self.view:
            return
        self.cursor = max(0, min(len(self.view) - 1, self.cursor + delta))
        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + self.page:
            self.top = self.cursor - self.page + 1

    def apply(self, key: str) -> str | None:
        if key == KEY_UP:
            self.move(-1)
        elif key == KEY_DOWN:
            self.move(1)
        elif key == KEY_PGUP:
            self.move(-self.page)
        elif key == KEY_PGDN:
            self.move(self.page)
        elif key == KEY_HOME:
            self.move(-len(self.view))
        elif key == KEY_END:
            self.move(len(self.view))
        elif key == KEY_BACKSPACE:
            self.query = self.query[:-1]
            self.refilter()
        elif key == "clear":
            self.query = ""
            self.refilter()
        elif key == KEY_ENTER:
            return "select"
        elif key == KEY_ESC:
            return "cancel"
        elif len(key) == 1 and key.isprintable():
            self.query += key
            self.refilter()
        return None

    def selected(self) -> Any | None:
        return self.view[self.cursor] if self.view else None


def render_picker(
    console: Console, state: PickerState, title: str, render: Callable[[Any], str]
) -> list[str]:
    p, c = console.p, console.r.c
    width = console.caps.width - 1
    head = (
        p.paint(title, "accent", bold=True)
        + p.paint(f"  {len(state.view)}/{len(state.all)}", "dim")
        + "   "
        + p.paint("filter ", "muted")
        + p.paint(state.query, "text", bold=True)
        + p.paint("▌" if console.caps.unicode else "_", "brand")
    )
    lines = [clip_ansi(head, width)]
    window = state.view[state.top : state.top + state.page]
    for offset, item in enumerate(window):
        idx = state.top + offset
        body = clip_ansi(render(item), width - 4)
        if idx == state.cursor:
            marker = p.paint(c["run"] + " ", "brand", bold=True)
            pad = " " * max(width - 2 - visible_len(body), 0)
            lines.append(
                marker + p.paint(body + pad, bg="sel_bg", bold=True) if console.caps.color else "> " + body
            )
        else:
            lines.append("  " + body)
    for _ in range(state.page - len(window)):
        lines.append("")
    hint = "↑↓ move   pgup/pgdn page   type to filter   enter select   esc cancel"
    if not console.caps.unicode:
        hint = "up/down move   type to filter   enter select   esc cancel"
    if not state.view:
        lines[1:2] = [p.paint("  no matches", "warn")]
    lines.append(p.paint(hint, "dim"))
    return lines


def pick(
    console: Console,
    items: Sequence[T],
    *,
    title: str,
    render: Callable[[T], str] | None = None,
    text_of: Callable[[T], str] | None = None,
    page: int | None = None,
    reader: Callable[[str], str | None] | None = None,
) -> T | None:
    render = render or str
    text_of = text_of or (lambda it: __import__("re").sub(r"\x1b\[[0-9;]*m", "", render(it)))
    if not items:
        return None
    if reader is None and interactive_ok() and console.caps.color >= 0:
        return _pick_interactive(console, items, title, render, text_of, page)
    return _pick_numbered(console, items, title, render, text_of, reader or (lambda q: input(q)))


def _pick_numbered(console, items, title, render, text_of, ask) -> Any:
    query = ""
    while True:
        view = filter_items(items, query, text_of)
        shown = view[:25]
        console.heading(title, f"{len(view)} of {len(items)}" + (f"  filter: {query}" if query else ""))
        for i, item in enumerate(shown, 1):
            console.line(f"  {console.p.paint(str(i).rjust(2), 'accent')}  {render(item)}")
        if len(view) > len(shown):
            console.dim(f"  ... {len(view) - len(shown)} more (type text to narrow the list)")
        answer = ask("number to select, text to filter, blank to cancel: ")
        if answer is None or not answer.strip():
            return None
        answer = answer.strip()
        if answer.isdigit() and 1 <= int(answer) <= len(shown):
            return shown[int(answer) - 1]
        query = answer


def _pick_interactive(console, items, title, render, text_of, page) -> Any:
    rows = console.caps.height
    page = page or max(5, min(14, rows - 6))
    state = PickerState(items, text_of, page)
    out = console.stream
    drawn = 0
    posix = os.name != "nt"
    old = None
    fd = sys.stdin.fileno()
    if posix:
        import termios
        import tty

        old = termios.tcgetattr(fd)
        tty.setcbreak(fd)

    def draw() -> None:
        nonlocal drawn
        lines = render_picker(console, state, title, render)
        buf = ""
        if drawn:
            buf += f"\x1b[{drawn}A"
        for ln in lines:
            buf += "\r\x1b[2K" + ln + "\n"
        drawn = len(lines)
        out.write(buf)
        out.flush()

    result = None
    try:
        out.write("\x1b[?25l")
        draw()
        while True:
            keys = _read_keys_posix(fd) if posix else _read_keys_windows()
            outcome = None
            for key in keys:
                outcome = state.apply(key) if key else None
                if outcome:
                    break
            if outcome == "select":
                result = state.selected()
                break
            if outcome == "cancel":
                break
            draw()
    finally:
        if old is not None:
            import termios

            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        wipe = (f"\x1b[{drawn}A" if drawn else "") + "".join("\r\x1b[2K\n" for _ in range(drawn))
        out.write(wipe + (f"\x1b[{drawn}A" if drawn else "") + "\x1b[?25h")
        out.flush()
    if result is not None:
        console.line(
            f"{console.p.paint(console.r.c['ok'], 'ok', bold=True)} {console.p.paint(title, 'muted')} "
            f"{console.p.paint(console.r.c['arrow'], 'dim')} {text_of(result).split('  ')[0].strip()}"
        )
    return result
