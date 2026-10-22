from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
RESET = "\x1b[0m"


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def visible_len(text: str) -> int:
    return sum(char_width(c) for c in strip_ansi(text))


def clip_ansi(text: str, width: int, ellipsis: str = "…") -> str:
    if width <= 0:
        return ""
    if visible_len(text) <= width:
        return text
    budget = width - char_width(ellipsis) if ellipsis else width
    out: list[str] = []
    used = 0
    pos = 0
    has_ansi = False
    while pos < len(text):
        m = ANSI_RE.match(text, pos)
        if m:
            out.append(m.group(0))
            has_ansi = True
            pos = m.end()
            continue
        ch = text[pos]
        w = char_width(ch)
        if used + w > budget:
            break
        out.append(ch)
        used += w
        pos += 1
    out.append(ellipsis)
    if has_ansi:
        out.append(RESET)
    return "".join(out)


def pad_ansi(text: str, width: int, align: str = "left") -> str:
    gap = width - visible_len(text)
    if gap <= 0:
        return text
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def wrap_plain(text: str, width: int) -> list[str]:
    width = max(width, 8)
    out: list[str] = []
    for raw in text.split("\n"):
        if not raw.strip():
            out.append("")
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        prefix = " " * indent
        line = prefix
        for word in raw.split():
            sep = "" if line.strip() == "" else " "
            if visible_len(line) + len(sep) + visible_len(word) <= width:
                line += sep + word
            else:
                if line.strip():
                    out.append(line)
                while visible_len(word) > width:
                    out.append(prefix + word[: width - indent])
                    word = word[width - indent :]
                line = prefix + word
        out.append(line)
    return out


@dataclass(frozen=True)
class Caps:
    color: int
    unicode: bool
    width: int
    height: int
    tty: bool

    @property
    def plain(self) -> bool:
        return self.color == 0


def enable_windows_vt() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def detect_caps(stream=None, env=None, *, force_plain: bool = False) -> Caps:
    env = os.environ if env is None else env
    stream = stream or sys.stdout
    tty = bool(getattr(stream, "isatty", lambda: False)())
    color = 0
    if not force_plain and "NO_COLOR" not in env and env.get("TERM") != "dumb":
        if tty or env.get("FORCE_COLOR"):
            if os.name == "nt":
                color = 3 if enable_windows_vt() and (env.get("WT_SESSION") or env.get("COLORTERM")) else 2
                if not enable_windows_vt():
                    color = 0
            else:
                ct = env.get("COLORTERM", "").lower()
                term = env.get("TERM", "")
                if ct in ("truecolor", "24bit"):
                    color = 3
                elif "256" in term or env.get("FORCE_COLOR") == "2":
                    color = 2
                elif env.get("FORCE_COLOR") == "3":
                    color = 3
                else:
                    color = 1
    enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
    uni = enc in ("utf8", "utf16", "utf32") and not env.get("WFC_ASCII")
    if env.get("FORCE_COLOR") in ("1", "2", "3") and not getattr(stream, "encoding", None):
        uni = not env.get("WFC_ASCII")
    size = shutil.get_terminal_size((100, 30))
    cols = int(env.get("COLUMNS", 0) or 0) or size.columns
    return Caps(color, uni, max(cols, 40), max(size.lines, 10), tty)


THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "brand": "#8B5CF6",
        "brand2": "#22D3EE",
        "accent": "#22D3EE",
        "ok": "#34D399",
        "warn": "#FBBF24",
        "err": "#F87171",
        "info": "#60A5FA",
        "text": "#E5E7EB",
        "muted": "#9CA3AF",
        "dim": "#6B7280",
        "border": "#4B5563",
        "agent": "#A78BFA",
        "side": "#2DD4BF",
        "user": "#F9A8D4",
        "tool": "#94A3B8",
        "add": "#34D399",
        "del": "#F87171",
        "hunk": "#60A5FA",
        "pill_fg": "#0B0B12",
        "sel_bg": "#312E81",
        "code_bg": "#1F2937",
        "syn_keyword": "#C792EA",
        "syn_string": "#C3E88D",
        "syn_number": "#F78C6C",
        "syn_comment": "#637777",
        "syn_func": "#82AAFF",
    },
    "light": {
        "brand": "#6D28D9",
        "brand2": "#0891B2",
        "accent": "#0E7490",
        "ok": "#047857",
        "warn": "#B45309",
        "err": "#B91C1C",
        "info": "#1D4ED8",
        "text": "#111827",
        "muted": "#4B5563",
        "dim": "#6B7280",
        "border": "#9CA3AF",
        "agent": "#6D28D9",
        "side": "#0F766E",
        "user": "#BE185D",
        "tool": "#475569",
        "add": "#047857",
        "del": "#B91C1C",
        "hunk": "#1D4ED8",
        "pill_fg": "#FFFFFF",
        "sel_bg": "#DDD6FE",
        "code_bg": "#F3F4F6",
        "syn_keyword": "#7C3AED",
        "syn_string": "#047857",
        "syn_number": "#C2410C",
        "syn_comment": "#6B7280",
        "syn_func": "#1D4ED8",
    },
}
THEMES["mono"] = {k: "#FFFFFF" for k in THEMES["dark"]}


def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def rgb_to_256(r: int, g: int, b: int) -> int:
    if r == g == b:
        if r < 8:
            return 16
        if r > 248:
            return 231
        return round((r - 8) / 247 * 24) + 232
    return 16 + 36 * round(r / 255 * 5) + 6 * round(g / 255 * 5) + round(b / 255 * 5)


def rgb_to_16(r: int, g: int, b: int, background: bool = False) -> int:
    idx = (1 if r > 127 else 0) | (2 if g > 127 else 0) | (4 if b > 127 else 0)
    bright = max(r, g, b) > 200 and idx != 0
    base = (100 if bright else 40) if background else (90 if bright else 30)
    return base + idx


def lerp_color(c1: str, c2: str, t: float) -> str:
    a, b = hex_rgb(c1), hex_rgb(c2)
    mixed = tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return f"#{mixed[0]:02X}{mixed[1]:02X}{mixed[2]:02X}"


class Painter:
    def __init__(self, caps: Caps, theme: dict[str, str] | None = None) -> None:
        self.caps = caps
        self.theme = theme or THEMES["dark"]

    def color_for(self, token_or_hex: str) -> str:
        return self.theme.get(token_or_hex, token_or_hex)

    def sgr(
        self,
        fg: str | None = None,
        bg: str | None = None,
        *,
        bold=False,
        dim=False,
        italic=False,
        underline=False,
        reverse=False,
    ) -> str:
        level = self.caps.color
        if level == 0:
            return ""
        codes: list[str] = []
        if bold:
            codes.append("1")
        if dim:
            codes.append("2")
        if italic:
            codes.append("3")
        if underline:
            codes.append("4")
        if reverse:
            codes.append("7")
        for value, is_bg in ((fg, False), (bg, True)):
            if not value:
                continue
            r, g, b = hex_rgb(self.color_for(value))
            if level >= 3:
                codes.append(f"{48 if is_bg else 38};2;{r};{g};{b}")
            elif level == 2:
                codes.append(f"{48 if is_bg else 38};5;{rgb_to_256(r, g, b)}")
            else:
                codes.append(str(rgb_to_16(r, g, b, is_bg)))
        return f"\x1b[{';'.join(codes)}m" if codes else ""

    def paint(self, text: str, fg: str | None = None, bg: str | None = None, **attrs) -> str:
        if not text:
            return text
        seq = self.sgr(fg, bg, **attrs)
        return f"{seq}{text}{RESET}" if seq else text

    def gradient(self, text: str, c1: str = "brand", c2: str = "brand2", **attrs) -> str:
        if self.caps.color < 2:
            return self.paint(text, c1, **attrs)
        a, b = self.color_for(c1), self.color_for(c2)
        n = max(len(text) - 1, 1)
        out = []
        for i, ch in enumerate(text):
            if ch == " ":
                out.append(ch)
            else:
                out.append(self.paint(ch, lerp_color(a, b, i / n), **attrs))
        return "".join(out)

    def pill(self, text: str, bg: str = "brand") -> str:
        if self.caps.color == 0:
            return f"[{text.strip()}]"
        return self.paint(f" {text.strip()} ", "pill_fg", bg, bold=True)
