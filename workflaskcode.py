import sys

if sys.version_info < (3, 11):
    sys.exit("WorkFlaskCode needs Python 3.11 or newer (found %d.%d)." % sys.version_info[:2])

try:
    import pydantic  # noqa: F401
except ImportError:
    sys.exit("Missing dependencies. Run: pip install -r requirements.txt")

from workflaskcode.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
