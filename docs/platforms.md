# Platforms

| Platform | Status |
|---|---|
| Linux | Developed and tested here (Python 3.13). Syntax checked for 3.11. |
| macOS | Code paths for the agent home (`~/Library/Application Support/WorkFlaskCode`) and readline/libedit exist; not tested on real hardware. |
| Windows | Paths (`%LOCALAPPDATA%`), Windows key reading, and process handling exist; not tested on real hardware. Use Windows Terminal. |

The terminal layer detects colour, Unicode and width, honours `NO_COLOR`, and `--plain` forces ASCII output with no colour. Without a TTY the interactive session refuses to start and `run`/subcommands still work. Playwright browser testing needs `python3 -m playwright install chromium`.
