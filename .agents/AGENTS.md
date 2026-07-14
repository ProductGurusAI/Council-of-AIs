# standing rules

## path resolution in test fixtures
- always resolve test fixture paths relative to `__file__`, never relative to the current working directory (cwd).
- example: use `os.path.abspath(os.path.join(os.path.dirname(__file__), "mock_fixture.py"))` instead of `"tests/mock_fixture.py"`. this prevents cwd-relative bugs when tests are executed from different execution contexts or directories.
