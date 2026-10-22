from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from workflaskcode.ui.console import Console

Completer = Callable[[str, str], Sequence[tuple[str, str]]]


class LineReader:
    def __init__(
        self,
        console: Console,
        *,
        history_path: Path | None = None,
        completer: Completer | None = None,
        toolbar: Callable[[], str] | None = None,
        allow_ptk: bool = True,
    ) -> None:
        self.console = console
        self.history_path = history_path
        self.completer = completer or (lambda text, line: [])
        self.toolbar = toolbar
        self._readline = None
        self._ptk_session = None
        self._ptk_patch = None
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        if interactive and allow_ptk and not os.environ.get("WFC_NO_PTK"):
            self._setup_ptk()
        if self._ptk_session is None and interactive:
            self._setup_readline()

    @property
    def uses_ptk(self) -> bool:
        return self._ptk_session is not None

    def _setup_ptk(self) -> None:
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.completion import Completer as PtkCompleter
            from prompt_toolkit.completion import Completion
            from prompt_toolkit.formatted_text import ANSI
            from prompt_toolkit.history import FileHistory, InMemoryHistory
            from prompt_toolkit.key_binding import KeyBindings
            from prompt_toolkit.patch_stdout import patch_stdout

            outer = self

            class _C(PtkCompleter):
                def get_completions(self, document, complete_event):
                    word = document.get_word_before_cursor(WORD=True)
                    for value, meta in outer.completer(word, document.text_before_cursor):
                        yield Completion(value, start_position=-len(word), display_meta=meta)

            bindings = KeyBindings()

            @bindings.add("escape", "enter")
            def _newline(event):
                event.current_buffer.insert_text("\n")

            history = InMemoryHistory()
            if self.history_path:
                self.history_path.parent.mkdir(parents=True, exist_ok=True)
                history = FileHistory(str(self.history_path))
            self._ptk_session = PromptSession(
                history=history,
                completer=_C(),
                complete_while_typing=True,
                key_bindings=bindings,
                bottom_toolbar=(lambda: ANSI(self.toolbar())) if self.toolbar else None,
                multiline=False,
            )
            self._ptk_patch = patch_stdout(raw=True)
            self._ptk_patch.__enter__()
            self.console.managed_prompt = True
            self._ANSI = ANSI
        except Exception:
            self._ptk_session = None
            self.console.managed_prompt = False

    def _setup_readline(self) -> None:
        try:
            import readline
        except ImportError:
            return
        self._readline = readline
        with contextlib.suppress(Exception):
            readline.set_history_length(2000)
            if self.history_path and self.history_path.exists():
                readline.read_history_file(str(self.history_path))
        readline.set_completer_delims(" \t\n")
        state: dict[str, list[str]] = {"matches": []}

        def complete(text: str, index: int) -> str | None:
            if index == 0:
                line = readline.get_line_buffer()
                state["matches"] = [v for v, _ in self.completer(text, line)]
            return state["matches"][index] if index < len(state["matches"]) else None

        readline.set_completer(complete)
        if "libedit" in (readline.__doc__ or ""):
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")

    def close(self) -> None:
        if self._ptk_patch is not None:
            with contextlib.suppress(Exception):
                self._ptk_patch.__exit__(None, None, None)
            self._ptk_patch = None
        if self._readline and self.history_path:
            with contextlib.suppress(Exception):
                self.history_path.parent.mkdir(parents=True, exist_ok=True)
                self._readline.write_history_file(str(self.history_path))
                os.chmod(self.history_path, 0o600)

    def _buffer(self) -> str:
        if self._readline:
            with contextlib.suppress(Exception):
                return self._readline.get_line_buffer()
        return ""

    def _read_one(self, prompt: str) -> str:
        c = self.console
        if self._ptk_session is not None:
            return self._ptk_session.prompt(self._ANSI(prompt))
        if self._readline is not None:
            c.bind_prompt(prompt, self._buffer)
        try:
            safe = c.prompt_text(readline_safe=True) if prompt == c.prompt_text() else prompt
            return input(safe if self._readline else prompt)
        finally:
            c.bind_prompt(None)

    def read(self, prompt: str, *, continuation: str = "… ") -> str | None:
        try:
            line = self._read_one(prompt)
        except EOFError:
            return None
        if line.strip() == '"""':
            body: list[str] = []
            while True:
                try:
                    nxt = self._read_one(continuation)
                except EOFError:
                    break
                if nxt.strip() == '"""':
                    break
                body.append(nxt)
            return "\n".join(body)
        parts = [line]
        while parts[-1].endswith("\\"):
            parts[-1] = parts[-1][:-1]
            try:
                parts.append(self._read_one(continuation))
            except EOFError:
                break
        return "\n".join(parts)

    def secret(self, prompt: str) -> str:
        import getpass

        return getpass.getpass(prompt)
