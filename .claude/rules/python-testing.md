---
name: Python Testing Rules
description: How to write, run, and locate tests in Python projects (pytest + uv). Read before adding or changing Python code.
---

<constraint name="all-code-needs-tests">
Every code change ships with a test. You cannot claim code works or a
feature is done until a test exercises it and passes. For a bug fix, first
write a test that reproduces the bug, then make it pass.
</constraint>

## Running tests (your verify loop)

Run tests through `uv`, never a bare `pytest`: the bare interpreter may lack
the project environment. If the project defines task-runner recipes
(`justfile`, `Makefile`), prefer those, since they encode the blessed
invocation.

| Goal | Command |
|------|---------|
| Full suite | `uv run pytest` |
| One directory | `uv run pytest tests/<area> -q` |
| One test | `uv run pytest tests/<area>/test_x.py::test_name` |
| Marked tests (off by default) | `uv run pytest -m integration` |

<constraint name="mark-and-exclude-slow-tests">
Tests that hit real external systems (network, databases, paid or
credentialed model APIs) must carry a marker (for example `integration`,
`llm`) and be excluded from the default run via `addopts` in
pyproject.toml. The default `uv run pytest` stays fast and offline. Run the
marked tests on purpose with `-m`.
</constraint>

<constraint name="tests-are-real-code">
Tests are linted and type-checked like the rest of the codebase (commonly
ruff and mypy). Before declaring work done, run the project's lint,
type-check, and test gates and make them pass. Never hide a failure with
`skip`, `xfail`, or `# type: ignore` to make a gate go green. Fix the cause
(see the pre-existing-issues rule).
</constraint>

## Where tests live

Mirror the source tree. Logic in `src/<pkg>/foo.py` is tested in
`tests/.../test_foo.py`. Name files `test_*.py` and functions `test_*`.
Keep shared fixtures in `conftest.py` at the narrowest level that serves the
tests using them (a suite-wide `tests/conftest.py`, or a subdir
`tests/<area>/conftest.py`). Keep reusable builders and factories in a plain
helper module (for example `tests/fixtures/`), separate from the test cases.

## Test isolation

<constraint name="test-isolation">
Each test is independent and must not rely on the state, order, or outcome
of another. A test that touches global state cleans up after itself.
</constraint>

Prefer pytest built-ins that clean up for you over manual teardown:

- `tmp_path` for files and directories (auto-removed).
- `monkeypatch` for env vars, attributes, and `cwd` (auto-reverted).
- An `autouse` fixture to reset shared or global state around each test.
- Choose the narrowest fixture `scope` that works (`function` by default).
  Widen to `module` or `session` only to share expensive setup, and only
  when the fixture holds no per-test mutable state.

<example name="redirect-global-state">
When the app reads global state (env vars, a user config or cache dir, a
database path), add an autouse fixture that redirects it into `tmp_path`,
for example `monkeypatch.setenv("APP_HOME", str(tmp_path / "home"))`, and
resets any cached singletons before and after each test. No test should read
or write real user directories.
</example>

## Mocking: run the real thing first

<constraint name="dont-mock-what-you-can-run">
Do not mock a dependency you can exercise for real cheaply. Mock only at
true external boundaries (network, paid or credentialed services).
Over-mocking yields tests that pass while production breaks.
</constraint>

- Filesystem and local tools: use real temp dirs and real local resources.
  For git, create a real local repo or bare remote under `tmp_path` instead
  of mocking the git layer.
- Database: point the app at a real, throwaway database under `tmp_path`
  (for example a temp SQLite file) rather than mocking the data layer.
- HTTP: mock with the library's community-standard tool, not hand-rolled
  patches and not live requests. For `httpx`, use respx.

<constraint name="check-mocking-capabilities">
Before adding any external mocking library, check whether the dependency
ships its own test support and whether the project already standardized on a
tool. Add a new dependency with `uv add` only when nothing existing fits.
</constraint>

## Writing good tests

- Assert observable behavior and outputs, not private implementation detail,
  so tests survive refactors.
- Make negative assertions precise. To assert a mock method was not called,
  use `assert mock.method_calls == []`, not `mock.assert_not_called()`. The
  latter only checks the mock was never called as a function, so it passes
  even when `mock.error(...)` fired.
- Cover many inputs with `@pytest.mark.parametrize`, not copy-pasted tests
  or a loop inside one test.
- For async tests, set `asyncio_mode = "auto"` (pytest-asyncio) so a plain
  `async def test_...` runs without a per-test decorator.
- Keep tests deterministic: no reliance on wall-clock time, ordering, real
  network, or unseeded randomness.

## TUI snapshot tests (Textual)

For a Textual TUI, use pytest-textual-snapshot. When a change intentionally
alters rendered output, review the snapshot diff, then refresh the baselines
with `uv run pytest <tui-tests> --snapshot-update`. Do not run
`--snapshot-update` just to clear a red test.
