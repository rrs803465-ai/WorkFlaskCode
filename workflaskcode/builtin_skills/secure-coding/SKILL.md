---
name: secure-coding
description: Defaults to follow when touching authentication, input handling, SQL, files or subprocesses.
---
# Secure coding defaults

- Bind SQL parameters; never format values into queries.
- Pass subprocess arguments as a list; avoid `shell=True`.
- Hash passwords with a slow KDF (argon2, bcrypt, scrypt); never store plain text.
- Read secrets from the environment or a secret store; never commit them.
- Validate and normalise user-supplied paths; reject `..` and absolute paths.
- Escape output by default; avoid `innerHTML` with untrusted data.
