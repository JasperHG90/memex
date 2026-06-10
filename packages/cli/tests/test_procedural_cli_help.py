"""Procedural-plane CLI help text regression fences.

Guards against the V7 procedural-plane CLI drift that the v11 worktree
saw in the v7 branch. The two top-level groups — ``memex procedural``
and ``memex case`` — are the discoverable entry points for the
procedural plane; their help text must:

* Identify the plane as the *procedural* plane (not "procedural",
  which is the legacy engine-internal term and a tell that the rename
  to ``procedural`` regressed).
* Cover the core verbs (create, upsert, get, get-by-identity, search,
  update, deprecate, briefing-cards) for ``procedural`` and ``submit``
  for ``case``.
* Not include "(V7)" or ticket references in user-facing prose — the
  rename removed those deliberately.
"""

from __future__ import annotations

import re

from memex_cli.procedural import app, case_app


def _normalize(text: str) -> str:
    """Collapse Typer's terminal-wrapped whitespace into single spaces."""
    return re.sub(r'\s+', ' ', text).strip()


def test_procedural_group_registered_with_correct_name():
    """The top-level group must be named 'procedural' (not 'procedural')."""
    assert app.info.name == 'procedural'


def test_procedural_help_identifies_plane_as_procedural(runner, strip_ansi):
    """The top-level help must call this the 'procedural' plane, not
    'procedural' (engine-internal legacy term)."""
    result = runner.invoke(app, ['--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert 'procedural' in text.lower()
    assert 'procedural' not in text.lower(), (
        'Top-level procedural help must not contain the legacy '
        'engine-internal term "procedural" — that belongs in '
        'SQLAlchemy and DTO internals only.'
    )


def test_procedural_help_lists_eight_subcommands(runner, strip_ansi):
    """All 8 procedural subcommands must be discoverable from --help."""
    result = runner.invoke(app, ['--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    for cmd in (
        'create',
        'upsert',
        'get',
        'get-by-identity',
        'search',
        'briefing-cards',
        'update',
        'deprecate',
    ):
        assert cmd in text, f'memex procedural help is missing subcommand: {cmd}'


def test_procedural_help_has_no_ticket_marker(runner, strip_ansi):
    """User-facing prose must not carry ticket markers like '(V7)'."""
    result = runner.invoke(app, ['--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert '(V7)' not in text
    assert '(v7)' not in text.lower()


def test_case_group_registered_with_correct_name():
    assert case_app.info.name == 'case'


def test_case_help_routes_to_procedural_plane(runner, strip_ansi):
    """`memex case` is the discoverable shortcut for case submission;
    its help must point at the procedural plane (not the legacy
    'procedural' label)."""
    result = runner.invoke(case_app, ['--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert 'procedural' in text.lower()
    assert 'procedural' not in text.lower()


def test_case_submit_help_requires_trigger(runner, strip_ansi):
    """Cases are findable by trigger — the help must surface that."""
    result = runner.invoke(case_app, ['submit', '--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert '--trigger' in text
    # The trigger flag must be marked required, not optional.
    assert 'trigger' in text.lower()
    assert 'required' in text.lower()


def test_procedural_create_help_explains_identity_anchor(runner, strip_ansi):
    """`memex procedural create --help` must call out the
    (kind, scope, verb, context) identity anchor rule so the user
    knows where 409s come from."""
    result = runner.invoke(app, ['create', '--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert 'verb' in text.lower()
    assert 'context' in text.lower()
    assert '409' not in text, (
        "Don't expose the raw HTTP status in the CLI help — route the "
        'user to `memex procedural upsert` for idempotent re-writes.'
    )
    assert 'upsert' in text.lower(), (
        'The CLI help should cross-link upsert so the user knows how to recover from a 409.'
    )
