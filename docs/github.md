# Publishing to GitHub

```bash
cd WorkFlaskCode
git status                      # review what will be committed
git remote add origin git@github.com:<you>/workflaskcode.git
git push -u origin main
```

Before the first push: choose your license holder name in `LICENSE`, set a real contact in `SECURITY.md` and `CODE_OF_CONDUCT.md`, and check `git ls-files` for anything private. Enable branch protection and the CI workflow in `.github/workflows/ci.yml` (it runs ruff and the test suite on Linux, macOS and Windows for Python 3.11 to 3.13). Tag releases with `git tag -a v0.1.0 -m "v0.1.0" && git push origin v0.1.0`; WorkFlaskCode itself never pushes for you.
