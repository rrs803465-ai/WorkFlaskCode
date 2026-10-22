---
name: python-testing
description: How to write, run and repair pytest suites in this project without weakening them.
---
# Python testing

- Run the narrowest test first: `pytest path/to/test_file.py::test_name -x`.
- Reproduce a failure before changing code. Read the traceback bottom-up and open the failing line.
- Fix the code under test, not the test, unless the test itself encodes a wrong requirement. State why when you change a test.
- Use `tmp_path` for files and `monkeypatch` for environment variables. Never touch the real home directory.
- Parametrize related cases instead of copying tests.
- After a fix, run the whole suite once to check for regressions.
