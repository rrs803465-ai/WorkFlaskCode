# Design lock

`@design-lock` scans the project and records what it finds: CSS custom properties and colours, fonts, breakpoints, spacing values, component folders, routes, database tables, dominant naming style and test commands. The snapshot is stored in the project metadata (`design.json`) and a managed block in `PROJECT.md`, and the rules are sent to the agent with every task.

Add your own constraints: `@design-lock rule use var(--brand) for accents`, `@design-lock prohibit inline styles`, `@design-lock decision keep the sidebar layout`.

- `@design-diff` compares the project to the snapshot and lists drift.
- `@design-audit` checks the code against the lock: literal colours not in the token set, fonts and breakpoints not recorded, prohibited patterns, naming drift.
- `@design-lock --update` accepts the current state; `@design-lock unlock` removes the lock after confirmation.

The scans are regex and structure based. They catch literal values and recorded structure, not visual quality.
