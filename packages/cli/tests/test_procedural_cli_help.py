"""Procedural-plane CLI help text regression fences.

Guards against procedural-plane CLI drift. The two top-level groups — ``memex procedure``
and ``memex case`` — are the discoverable entry points for the
procedural plane; their help text must:

* Identify the plane as the *procedural* plane (not "procedural",
  which is the legacy engine-internal term and a tell that the rename
  to ``procedural`` regressed).
* Cover the core verbs (create, upsert, get, get-by-identity, search,
  update, deprecate, briefing-cards) for ``procedural`` and ``submit``
  for ``case``.
* Not include "" or ticket references in user-facing prose — the
  rename removed those deliberately.
"""

from __future__ import annotations

import re

from memex_cli.procedural import app, case_app


def _normalize(text: str) -> str:
    """Collapse Typer's terminal-wrapped whitespace into single spaces."""
    return re.sub(r'\s+', ' ', text).strip()


def test_procedural_group_registered_with_correct_name():
    """The CLI group is named 'procedure' — a singular noun matching the other
    command groups (note/memory/vault/kv). The PLANE concept stays 'procedural'."""
    assert app.info.name == 'procedure'


def test_procedural_help_identifies_plane_as_procedural(runner, strip_ansi):
    """The top-level help must call this the 'procedural' plane, not
    'procedural' (engine-internal legacy term)."""
    result = runner.invoke(app, ['--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert 'procedural' in text.lower()
    assert 'experiential' not in text.lower(), (
        'Top-level procedural help must not contain the legacy '
        'term "experiential" — the plane is named procedural '
        'everywhere (JG directive 2026-06-10).'
    )


def test_procedural_help_lists_eight_subcommands(runner, strip_ansi):
    """All 8 procedural subcommands must be discoverable from --help."""
    result = runner.invoke(app, ['--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    for cmd in (
        'create',
        'upsert',
        'view',
        'get-by-identity',
        'search',
        'list',
        'briefing-cards',
        'update',
        'deprecate',
    ):
        assert cmd in text, f'memex procedure help is missing subcommand: {cmd}'
    # The legacy 'get' name is a hidden alias: still works, but not advertised.
    assert ' get ' not in text, 'legacy "get" should be hidden from procedure --help'


def test_procedural_help_has_no_ticket_marker(runner, strip_ansi):
    """User-facing prose must not carry a parenthesised ticket-version
    marker like ``(v7)`` / ``(v8)`` — ticket names don't belong in
    surfaced help text."""
    import re

    result = runner.invoke(app, ['--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert not re.search(r'\(v\d+\)', text.lower()), 'help text leaks a ticket-version marker'


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
    assert 'experiential' not in text.lower()


def test_case_group_lists_all_subcommands(runner, strip_ansi):
    """`memex case --help` must surface submit, list, and view."""
    result = runner.invoke(case_app, ['--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    for cmd in ('submit', 'list', 'view'):
        assert cmd in text, f'memex case help is missing subcommand: {cmd}'


def test_case_submit_is_a_real_subcommand(runner, strip_ansi):
    """`memex case submit` must RUN the submit subcommand — not fail with
    'unexpected extra argument (submit)'. Typer flattens a single-command group
    unless it has a callback; this pins that the callback keeps `case` a group."""
    result = runner.invoke(case_app, ['submit'])  # no required args supplied
    combined = (strip_ansi(result.stdout) + ' ' + str(result.exception or '')).lower()
    assert 'unexpected extra argument' not in combined
    # It reaches our own runtime validation (title/trigger/outcome required).
    assert result.exit_code != 0


def test_case_list_and_view_help_surface_filters(runner, strip_ansi):
    """list/view help must expose the case-specific filters (outcome,
    project-id, tag) and the note-id argument."""
    list_result = runner.invoke(case_app, ['list', '--help'])
    assert list_result.exit_code == 0
    list_text = _normalize(strip_ansi(list_result.stdout))
    assert '--outcome' in list_text
    assert '--project-id' in list_text
    assert '--tag' in list_text
    assert '--submitted-by' in list_text
    assert '--slim' in list_text

    view_result = runner.invoke(case_app, ['view', '--help'])
    assert view_result.exit_code == 0
    view_text = _normalize(strip_ansi(view_result.stdout))
    assert 'note-id' in view_text or 'NOTE_ID' in view_text
    assert '--json' in view_text


def test_case_submit_help_surfaces_trigger_and_file(runner, strip_ansi):
    """Cases are findable by trigger — the help must surface it. title/trigger/
    outcome are required, but can come from the --file §5.1 template instead of
    flags (validated at runtime), so they are no longer typer-`required`
    options."""
    result = runner.invoke(case_app, ['submit', '--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert '--trigger' in text
    assert '--outcome' in text
    # The deterministic file path (the §5.1 template inverse).
    assert '--file' in text


def test_procedural_create_help_explains_identity_anchor(runner, strip_ansi):
    """`memex procedure create --help` must call out the
    (kind, scope, verb, context) identity anchor rule so the user
    knows where 409s come from."""
    result = runner.invoke(app, ['create', '--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert 'verb' in text.lower()
    assert 'context' in text.lower()
    assert '409' not in text, (
        "Don't expose the raw HTTP status in the CLI help — route the "
        'user to `memex procedure upsert` for idempotent re-writes.'
    )
    assert 'upsert' in text.lower(), (
        'The CLI help should cross-link upsert so the user knows how to recover from a 409.'
    )
