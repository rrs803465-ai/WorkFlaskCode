# Security policy

Please report suspected vulnerabilities privately through GitHub's "Report a vulnerability" feature on this repository (Security tab). Include the version (`python3 workflaskcode.py --version`), your OS, and steps to reproduce. Do not include real API keys.

In scope: path-traversal or symlink escapes from the workspace guard, permission-policy bypasses, secret leakage into logs, config, exports or project metadata, plugin consent bypass, and unsafe command execution paths.

Out of scope by design (documented in [docs/security.md](docs/security.md)): approved commands running with your user's privileges, and plugins running unsandboxed in-process after you consent to them.

We aim to acknowledge reports within a week. Supported version: the latest release.
