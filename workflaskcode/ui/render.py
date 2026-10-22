from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from workflaskcode.ui.ansi import Caps, Painter, clip_ansi, lerp_color, pad_ansi, visible_len, wrap_plain

UNICODE = {
    "tl": "╭",
    "tr": "╮",
    "bl": "╰",
    "br": "╯",
    "h": "─",
    "v": "│",
    "ml": "├",
    "mr": "┤",
    "dot": "•",
    "prompt": "❯",
    "ok": "✓",
    "err": "✗",
    "warn": "▲",
    "gear": "⚙",
    "run": "▸",
    "branch": "⎇",
    "full": "▰",
    "empty": "▱",
    "g_main": "▎",
    "g_side": "┆",
    "ell": "…",
    "sep": "│",
    "dirty": "●",
    "plus": "＋",
    "arrow": "→",
    "spin": "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏",
    "code_top": "┌",
    "code_bot": "└",
    "code_v": "│",
    "quote": "▏",
    "star": "◆",
}
ASCII = {
    "tl": "+",
    "tr": "+",
    "bl": "+",
    "br": "+",
    "h": "-",
    "v": "|",
    "ml": "+",
    "mr": "+",
    "dot": "*",
    "prompt": ">",
    "ok": "+",
    "err": "x",
    "warn": "!",
    "gear": "*",
    "run": ">",
    "branch": "@",
    "full": "#",
    "empty": "-",
    "g_main": "|",
    "g_side": ":",
    "ell": "~",
    "sep": "|",
    "dirty": "*",
    "plus": "+",
    "arrow": "->",
    "spin": "|/-\\",
    "code_top": "+",
    "code_bot": "+",
    "code_v": "|",
    "quote": "|",
    "star": "*",
}

FONT = {
    "W": ["█   █", "█   █", "█ █ █", "██ ██", "█   █"],
    "O": [" ███ ", "█   █", "█   █", "█   █", " ███ "],
    "R": ["████ ", "█   █", "████ ", "█  █ ", "█   █"],
    "K": ["█   █", "█  █ ", "███  ", "█  █ ", "█   █"],
    "F": ["█████", "█    ", "████ ", "█    ", "█    "],
    "L": ["█    ", "█    ", "█    ", "█    ", "█████"],
    "A": [" ███ ", "█   █", "█████", "█   █", "█   █"],
    "S": [" ████", "█    ", " ███ ", "    █", "████ "],
    "C": [" ████", "█    ", "█    ", "█    ", " ████"],
    "D": ["████ ", "█   █", "█   █", "█   █", "████ "],
    "E": ["█████", "█    ", "████ ", "█    ", "█████"],
}

_KEYWORDS = {
    "python": "False None True and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case self",
    "js": "async await break case catch class const continue debugger default delete do else export extends finally for function if import in instanceof let new return super switch this throw try typeof var void while with yield null undefined true false interface type enum implements public private protected readonly",
    "shell": "if then else elif fi for while do done case esac function in echo export cd ls cat grep git pip python npm sudo rm mkdir",
    "go": "break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var nil true false",
    "rust": "as break const continue crate else enum extern false fn for if impl in let loop match mod move mut pub ref return self static struct super trait true type unsafe use where while async await dyn",
    "css": "",
    "json": "true false null",
}
_LANG_ALIAS = {
    "py": "python",
    "python3": "python",
    "javascript": "js",
    "ts": "js",
    "typescript": "js",
    "jsx": "js",
    "tsx": "js",
    "sh": "shell",
    "bash": "shell",
    "zsh": "shell",
    "console": "shell",
    "golang": "go",
    "rs": "rust",
    "html": "css",
    "yaml": "json",
    "toml": "json",
    "jsonc": "json",
}


def canon_lang(lang: str) -> str:
    lang = lang.strip().lower()
    return _LANG_ALIAS.get(lang, lang)


def _highlight_regex(lang: str) -> re.Pattern[str] | None:
    kws = _KEYWORDS.get(lang)
    if kws is None:
        return None
    comment = {"python": r"#[^\n]*", "shell": r"#[^\n]*", "css": r"/\*.*?\*/|<!--.*?-->"}.get(
        lang, r"//[^\n]*|/\*.*?\*/"
    )
    parts = [
        rf"(?P<comment>{comment})",
        r"(?P<string>\"\"\".*?\"\"\"|'''.*?'''|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)",
        r"(?P<number>\b0x[0-9A-Fa-f]+\b|\b\d+(?:\.\d+)?\b)",
    ]
    if kws:
        parts.append(rf"(?P<keyword>\b(?:{'|'.join(kws.split())})\b)")
    parts.append(r"(?P<func>\b[A-Za-z_][A-Za-z0-9_]*(?=\())")
    return re.compile("|".join(parts), re.S)


_HL_CACHE: dict[str, re.Pattern[str] | None] = {}


class Renderer:
    def __init__(self, painter: Painter, caps: Caps) -> None:
        self.p = painter
        self.caps = caps
        self.c = UNICODE if caps.unicode else ASCII

    @property
    def width(self) -> int:
        return min(self.caps.width, 120)

    def highlight(self, code: str, lang: str = "") -> str:
        if self.caps.color == 0:
            return code
        lang = canon_lang(lang)
        if lang not in _HL_CACHE:
            _HL_CACHE[lang] = _highlight_regex(lang)
        rx = _HL_CACHE[lang]
        if rx is None:
            return code
        out: list[str] = []
        pos = 0
        for m in rx.finditer(code):
            out.append(code[pos : m.start()])
            kind = m.lastgroup or ""
            out.append(self.p.paint(m.group(0), f"syn_{kind}", italic=kind == "comment"))
            pos = m.end()
        out.append(code[pos:])
        return "".join(out)

    def box(
        self,
        lines: Iterable[str],
        *,
        title: str | None = None,
        token: str = "border",
        width: int | None = None,
        pad: int = 1,
        title_token: str = "accent",
    ) -> list[str]:
        c = self.c
        width = min(width or self.width, self.caps.width)
        inner = width - 2
        paint = lambda s: self.p.paint(s, token)  # noqa: E731
        if title:
            label = f" {self.p.paint(title, title_token, bold=True)} "
            top = (
                paint(c["tl"] + c["h"])
                + label
                + paint(c["h"] * max(inner - 1 - visible_len(label), 0) + c["tr"])
            )
        else:
            top = paint(c["tl"] + c["h"] * inner + c["tr"])
        out = [top]
        room = inner - 2 * pad
        for raw in lines:
            for piece in raw.split("\n") if raw else [""]:
                text = clip_ansi(piece, room)
                out.append(paint(c["v"]) + " " * pad + pad_ansi(text, room) + " " * pad + paint(c["v"]))
        out.append(paint(c["bl"] + c["h"] * inner + c["br"]))
        return out

    def rule(self, label: str | None = None, token: str = "border", width: int | None = None) -> str:
        width = min(width or self.width, self.caps.width)
        h = self.c["h"]
        if not label:
            return self.p.paint(h * width, token)
        text = f" {label} "
        left = 3
        right = max(width - left - visible_len(text), 0)
        return self.p.paint(h * left, token) + self.p.paint(text, "muted") + self.p.paint(h * right, token)

    def kv(
        self, pairs: Sequence[tuple[str, str]], *, key_token: str = "muted", key_width: int | None = None
    ) -> list[str]:
        kw = key_width or max((len(k) for k, _ in pairs), default=0)
        return [f"{self.p.paint(k.ljust(kw), key_token)}  {v}" for k, v in pairs]

    def bar(self, done: int, total: int, width: int = 10, token: str = "ok") -> str:
        total = max(total, 0)
        filled = 0 if total == 0 else round(width * min(done, total) / total)
        return self.p.paint(self.c["full"] * filled, token) + self.p.paint(
            self.c["empty"] * (width - filled), "dim"
        )

    def table(
        self,
        headers: Sequence[str],
        rows: Sequence[Sequence[str]],
        *,
        aligns: Sequence[str] | None = None,
        max_width: int | None = None,
        indent: int = 2,
        shrink_order: Sequence[int] | None = None,
    ) -> list[str]:
        max_width = (max_width or self.width) - indent
        n = len(headers)
        aligns = list(aligns or ["left"] * n)
        widths = [max([visible_len(headers[i])] + [visible_len(r[i]) for r in rows]) for i in range(n)]
        gap = 2
        order = list(shrink_order) if shrink_order else sorted(range(n), key=lambda i: -widths[i])
        while sum(widths) + gap * (n - 1) > max_width:
            target = next((i for i in order if widths[i] > 6), None)
            if target is None:
                break
            widths[target] -= 1
        pad = " " * indent
        head = pad + (" " * gap).join(
            pad_ansi(self.p.paint(clip_ansi(h, widths[i]), "accent", bold=True), widths[i], aligns[i])
            for i, h in enumerate(headers)
        )
        sep = pad + self.p.paint((self.c["h"] * (sum(widths) + gap * (n - 1))), "border")
        body = [
            pad
            + (" " * gap).join(
                pad_ansi(clip_ansi(cell, widths[i]), widths[i], aligns[i]) for i, cell in enumerate(row)
            )
            for row in rows
        ]
        return [head, sep, *body]

    def diff(self, text: str, *, max_lines: int = 400) -> list[str]:
        out: list[str] = []
        old_no = new_no = 0
        shown = 0
        lines = text.splitlines()
        for line in lines:
            if shown >= max_lines:
                out.append(self.p.paint(f"{self.c['ell']} {len(lines) - shown} more diff lines", "dim"))
                break
            shown += 1
            if line.startswith(("diff --git", "index ", "new file", "deleted file", "similarity", "rename ")):
                out.append(self.p.paint(line, "dim"))
            elif line.startswith("+++") or line.startswith("---"):
                out.append(self.p.paint(line, "muted", bold=True))
            elif line.startswith("@@"):
                m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", line)
                if m:
                    old_no, new_no = int(m.group(1)), int(m.group(2))
                out.append(self.p.paint(line, "hunk"))
            elif line.startswith("+"):
                gutter = self.p.paint(f"{'':>4} {new_no:>4} {self.c['v']} ", "dim")
                out.append(gutter + self.p.paint(line, "add"))
                new_no += 1
            elif line.startswith("-"):
                gutter = self.p.paint(f"{old_no:>4} {'':>4} {self.c['v']} ", "dim")
                out.append(gutter + self.p.paint(line, "del"))
                old_no += 1
            else:
                gutter = self.p.paint(f"{old_no:>4} {new_no:>4} {self.c['v']} ", "dim")
                out.append(gutter + self.p.paint(line, "muted"))
                old_no += 1
                new_no += 1
        return out

    def logo(self) -> list[str]:
        word = "WORKFLASKCODE"
        rows = []
        for r in range(5):
            rows.append(" ".join(FONT[ch][r] for ch in word))
        width = len(rows[0])
        out = []
        for r, row in enumerate(rows):
            line = []
            for i, ch in enumerate(row):
                if ch == " ":
                    line.append(" ")
                    continue
                t = i / max(width - 1, 1)
                shade = 1.0 - 0.18 * (r / 4)
                col = lerp_color(self.p.color_for("brand"), self.p.color_for("brand2"), t)
                if self.caps.color >= 2:
                    rr, gg, bb = (int(col[1:3], 16), int(col[3:5], 16), int(col[5:7], 16))
                    col = f"#{int(rr * shade):02X}{int(gg * shade):02X}{int(bb * shade):02X}"
                line.append(self.p.paint(ch, col))
            out.append("".join(line))
        return out

    def banner(self, version: str) -> list[str]:
        tagline = "Your code. Your models. Your workflow."
        if self.caps.width >= 80 and self.caps.unicode:
            lines = self.logo()
        else:
            star = self.c["star"]
            lines = [self.p.gradient(f"{star} W O R K F L A S K C O D E", bold=True)]
        lines.append("")
        lines.append(
            self.p.paint(tagline, "muted", italic=True)
            + "   "
            + self.p.paint(f"v{version}", "dim")
            + "  "
            + self.p.paint("terminal AI software engineering agent", "dim")
        )
        return lines

    def wrap(self, text: str, width: int | None = None) -> list[str]:
        return wrap_plain(text, width or self.width)


_INLINE = re.compile(
    r"(\*\*[^*\n]+\*\*)|(`[^`\n]+`)|(\[[^\]\n]+\]\([^)\n]+\))|(?<![\w*])(\*[^*\n]+\*)(?![\w*])"
)


class MarkdownStream:
    def __init__(self, renderer: Renderer, gutter: str, gutter_token: str, width: int | None = None) -> None:
        self.r = renderer
        self.p = renderer.p
        self.gutter = self.p.paint(gutter + " ", gutter_token)
        self.width = (width or renderer.width) - 3
        self.in_code = False
        self.lang = ""
        self.buffer = ""

    def feed(self, chunk: str) -> list[str]:
        self.buffer += chunk
        out: list[str] = []
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            out.extend(self.render_line(line))
        return out

    def flush(self) -> list[str]:
        out: list[str] = []
        if self.buffer:
            out.extend(self.render_line(self.buffer))
            self.buffer = ""
        if self.in_code:
            out.extend(self._close_code())
        return out

    def _close_code(self) -> list[str]:
        self.in_code = False
        return [self.gutter + self.p.paint(self.r.c["code_bot"] + self.r.c["h"] * 3, "dim")]

    def _inline(self, text: str) -> str:
        def sub(m: re.Match[str]) -> str:
            if m.group(1):
                return self.p.paint(m.group(1)[2:-2], "text", bold=True)
            if m.group(2):
                return self.p.paint(m.group(2)[1:-1], "accent", bg="code_bg")
            if m.group(3):
                label, _, url = m.group(3)[1:-1].partition("](")
                return self.p.paint(label, "info", underline=True) + self.p.paint(f" ({url})", "dim")
            return self.p.paint(m.group(4)[1:-1], "text", italic=True)

        return _INLINE.sub(sub, text)

    def render_line(self, line: str) -> list[str]:
        fence = re.match(r"^\s*```\s*([\w+\-.#]*)\s*$", line)
        if fence:
            if self.in_code:
                return self._close_code()
            self.in_code = True
            self.lang = fence.group(1)
            label = self.lang or "code"
            top = self.r.c["code_top"] + self.r.c["h"] + " " + label
            return [self.gutter + self.p.paint(top, "dim")]
        if self.in_code:
            body = self.r.highlight(line.replace("\t", "    "), self.lang)
            bar = self.p.paint(self.r.c["code_v"] + " ", "dim")
            return [self.gutter + bar + clip_ansi(body, self.width - 2, self.r.c["ell"])]
        if not line.strip():
            return [self.gutter.rstrip()]
        header = re.match(r"^(#{1,6})\s+(.*)$", line)
        if header:
            level = len(header.group(1))
            text = header.group(2).strip()
            if level == 1:
                return [self.gutter + self.p.gradient(text.upper(), bold=True)]
            return [self.gutter + self.p.paint(text, "accent" if level == 2 else "text", bold=True)]
        if re.match(r"^\s*([-*_])\1{2,}\s*$", line):
            return [self.gutter + self.p.paint(self.r.c["h"] * min(self.width, 48), "border")]
        bullet = re.match(r"^(\s*)([-*+])\s+(.*)$", line)
        number = re.match(r"^(\s*)(\d+[.)])\s+(.*)$", line)
        quote = re.match(r"^\s*>\s?(.*)$", line)
        if bullet:
            indent, _, text = bullet.groups()
            marker = indent + self.p.paint(self.r.c["dot"], "accent") + " "
            return self._wrapped(text, marker, " " * (len(indent) + 2))
        if number:
            indent, num, text = number.groups()
            marker = indent + self.p.paint(num, "accent") + " "
            return self._wrapped(text, marker, " " * (len(indent) + len(num) + 1))
        if quote:
            bar = self.p.paint(self.r.c["quote"] + " ", "dim")
            return self._wrapped(quote.group(1), bar, bar, token="muted")
        if line.lstrip().startswith("|") and line.rstrip().endswith("|"):
            if re.match(r"^\s*\|[\s:|-]+\|\s*$", line):
                return [self.gutter + self.p.paint(self.r.c["h"] * min(self.width, 60), "dim")]
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            joined = self.p.paint(f" {self.r.c['sep']} ", "dim").join(self._inline(c) for c in cells)
            return [self.gutter + clip_ansi(joined, self.width, self.r.c["ell"])]
        return self._wrapped(line, "", "")

    def _wrapped(self, text: str, first: str, rest: str, token: str | None = None) -> list[str]:
        room = max(self.width - visible_len(first), 20)
        out = []
        for i, piece in enumerate(wrap_plain(text, room)):
            body = self._inline(piece)
            if token:
                body = self.p.paint(body, token)
            out.append(self.gutter + (first if i == 0 else rest) + body)
        return out or [self.gutter + first]
