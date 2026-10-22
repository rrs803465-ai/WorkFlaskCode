# Plugins and skills

## Skills

A skill is a folder with `SKILL.md` (frontmatter `name`, `description`, then instructions). The agent sees the index and loads one with `read_skill`. Skills are text only and cannot execute code. Locations: built-in (`python-testing`, `web-accessibility`, `secure-coding`), the agent home `skills/`, and `.workflaskcode/skills/` in a project. `@skills` lists them.

## Plugins

A plugin is a folder in the agent home `plugins/` containing `plugin.toml` and `plugin.py`:

```toml
name = "word-count"
version = "0.1.0"
description = "Adds @wc"
permissions = ["commands"]
```

```python
def register(api):
    def wc(rt, raw):
        return f"{len(raw.split())} words"

    api.add_command("wc", wc, "count words in the arguments")
```

Permissions: `commands`, `tools`, `tools:write`, `tools:exec`, `templates`, `audit`, `hooks`. Using an API you did not declare raises an error and the plugin fails to load. Plugin tools still pass through the core permission policy and cannot bypass it.

`@plugins enable <name>` shows the permissions and the file path, and asks for consent. Consent is stored with a hash of the manifest and code; any change disables the plugin until you consent again.

**Plugins are not sandboxed.** They are ordinary Python running in the agent's process. Read a plugin before enabling it. See `examples/plugins/word-count`.
