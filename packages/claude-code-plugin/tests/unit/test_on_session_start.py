"""Unit tests for ``scripts/on_session_start.sh`` — KV migration & state caching."""

from __future__ import annotations

import json
from pathlib import Path


from _helpers import MockMemex, run_script


def _session_start_payload(model: str = 'claude-opus-4-7') -> str:
    return json.dumps(
        {
            'session_id': 'cc-session-xyz',
            'transcript_path': '/tmp/x.jsonl',
            'cwd': '/tmp',
            'hook_event_name': 'SessionStart',
            'source': 'startup',
            'model': model,
        }
    )


def test_writes_session_note_key_state_file(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    result = run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
    state_dir = mock_memex.plugin_data / 'memex'
    note_key_file = state_dir / 'session_note_key'
    assert note_key_file.exists()
    note_key = note_key_file.read_text().strip()
    assert note_key.startswith('session:')
    # Must be a recognizable timestamp (YYYY-MM-DD prefix)
    assert note_key[8:18].count('-') == 2


def test_caches_model_from_payload(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(model='claude-haiku-4-5'),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    state_dir = mock_memex.plugin_data / 'memex'
    model_file = state_dir / 'model'
    assert model_file.exists()
    assert model_file.read_text().strip() == 'claude-haiku-4-5'


def test_caches_cc_session_id_from_payload(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    """The Claude Code session id is load-bearing for `/handoff` upsert and must
    be cached alongside model/project state."""
    run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    state_dir = mock_memex.plugin_data / 'memex'
    cc_session_file = state_dir / 'cc_session_id'
    assert cc_session_file.exists()
    assert cc_session_file.read_text().strip() == 'cc-session-xyz'


def test_caches_project_id_and_active_vault(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    mock_memex.set_kv('app:claude-code:project:github.com/acme/myapp:vault', 'eng-vault')
    run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    state_dir = mock_memex.plugin_data / 'memex'
    assert (state_dir / 'project_id').read_text().strip() == 'github.com/acme/myapp'
    assert (state_dir / 'active_vault').read_text().strip() == 'eng-vault'


def test_legacy_kv_key_forward_migrates(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    """SessionStart should auto-migrate the bare project: key into app:claude-code:*."""
    mock_memex.set_kv('project:github.com/acme/myapp:vault', 'old-vault')
    # New-namespace key intentionally absent
    run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    kv_text = mock_memex.kv_file.read_text()
    # Both keys now present with the same value
    assert 'project:github.com/acme/myapp:vault=old-vault' in kv_text
    assert 'app:claude-code:project:github.com/acme/myapp:vault=old-vault' in kv_text
    # And the resolved active vault matches
    assert (mock_memex.plugin_data / 'memex' / 'active_vault').read_text().strip() == 'old-vault'


def test_clears_stale_state_on_new_session(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    """All per-session state files must be wiped fresh.

    Includes the PreCompact/SessionEnd `session_note_offset_*` /
    `session_note_created_*` family — otherwise they accumulate over
    hundreds of sessions and the offsets from previous sessions could
    nominally collide on the next session_id reuse.

    Also clears the cached CC session id so `/handoff` notes cannot anchor
    to a previous session.
    """
    state_dir = mock_memex.plugin_data / 'memex'
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'write_count').write_text('99')
    (state_dir / 'file_edits').mkdir(exist_ok=True)
    (state_dir / 'file_edits' / 'stale').write_text('5 stalefile')
    (state_dir / 'capture_count_oldsession').write_text('a\nb\n')
    (state_dir / 'session_note_offset_oldsession').write_text('42')
    (state_dir / 'session_note_created_oldsession').write_text('')
    (state_dir / 'cc_session_id').write_text('stale-old-session')

    run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )

    assert not (state_dir / 'write_count').exists()
    assert not (state_dir / 'file_edits').exists()
    assert not (state_dir / 'capture_count_oldsession').exists()
    assert not (state_dir / 'session_note_offset_oldsession').exists()
    assert not (state_dir / 'session_note_created_oldsession').exists()
    # The stale CC session id is wiped and rewritten from the current payload.
    assert (state_dir / 'cc_session_id').read_text().strip() == 'cc-session-xyz'


def test_emits_additional_context_with_briefing(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    mock_memex.set_kv('app:claude-code:project:github.com/acme/myapp:vault', 'eng-vault')
    result = run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    out = json.loads(result.stdout)
    assert 'hookSpecificOutput' in out
    ctx = out['hookSpecificOutput']['additionalContext']
    assert '(mock briefing content)' in ctx
    # Vault instruction surfaces the active vault
    assert 'eng-vault' in ctx
    # Auto-tag instruction must appear so the agent knows what's being injected
    assert 'Auto-injected metadata' in ctx
    assert 'session:' in ctx
    # The new namespaced KV key should appear in the "no vault set" branch
    # (not relevant here, but sanity-check the project_id is referenced)
    assert 'github.com/acme/myapp' in ctx
    # The compact vault block must LEAD the briefing so it survives Claude Code's
    # additionalContext truncation (collapsed to a ~2KB preview above 10K chars).
    assert ctx.index('### Per-project vault') < ctx.index('Memex briefing'), (
        'the per-project vault block must precede the briefing so truncation cannot drop it'
    )
    # The briefing heading must start its own line, not glue onto the end of the
    # auto-tag block (regression guard for the reorder separator).
    assert 'preserved.## Memex briefing' not in ctx, (
        'briefing heading is glued onto the auto-tag block — missing separator'
    )
    assert '\n\n## Memex briefing' in ctx, (
        'briefing must be separated from the preceding block by a blank line'
    )


# ---------------------------------------------------------------------------
# Agent-surface install — delivered as <project>/.claude/rules/memex-agent-surface.md
# instead of inline in `additionalContext`. Claude Code v2.1.x silently truncates
# SessionStart hook output above ~10K chars (Anthropic-side #42369), so the
# Tier 1b/2 agent surface now travels via the project rules dir (auto-loaded
# into the system prompt without going through hooks).
# ---------------------------------------------------------------------------


def test_agent_surface_installed_as_project_rule(
    mock_memex: MockMemex, temp_git_repo: Path
) -> None:
    """The hook calls `memex agent-surface claude-code --output-dir <project>/.claude/rules`
    and the resulting rule file lands at the path the harness auto-loads."""
    result = run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr

    rule_path = temp_git_repo / '.claude' / 'rules' / 'memex-agent-surface.md'
    assert rule_path.is_file(), (
        f'agent-surface rule file not installed at {rule_path}; '
        f'calls={[c.get("argv") for c in mock_memex.calls()]!r}'
    )

    # And the hook reached the CLI with the right invocation shape.
    surface_calls = mock_memex.calls_matching('agent-surface', 'claude-code')
    assert surface_calls, 'expected `memex agent-surface claude-code` call'
    argv = surface_calls[0]['argv']
    assert '--output-dir' in argv, f'expected --output-dir flag in argv={argv!r}'


def test_first_install_emits_restart_warning(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    """First install (rule file did not exist before the hook ran) must
    surface a restart hint in the `systemMessage` — the system prompt is
    assembled BEFORE SessionStart fires, so the brand-new rule file isn't
    live until the next session boot.

    Pin the filename (a stable contract) rather than the surrounding
    English wording (editorial)."""
    rule_path = temp_git_repo / '.claude' / 'rules' / 'memex-agent-surface.md'
    assert not rule_path.exists(), 'pre-condition: rule absent for first-install test'

    result = run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    sm = out['systemMessage']
    assert 'memex-agent-surface.md' in sm, (
        f'expected install hint referencing the rule filename; got {sm!r}'
    )


def test_routine_install_silent(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    """When the rule file already exists, no install hint should leak —
    routine refreshes are silent so the systemMessage stays clean.

    Also assert the success status IS present so a hook that returns ``{}``
    (the ERR trap) doesn't false-pass this negative check."""
    rule_dir = temp_git_repo / '.claude' / 'rules'
    rule_dir.mkdir(parents=True, exist_ok=True)
    (rule_dir / 'memex-agent-surface.md').write_text('pre-existing content', encoding='utf-8')

    result = run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    sm = out['systemMessage']
    assert 'Memex connected' in sm, f'unexpected ERR-trap fallback; got {sm!r}'
    assert 'memex-agent-surface.md' not in sm, f'install hint leaked on routine install; got {sm!r}'


def test_additional_context_excludes_agent_surface_body(
    mock_memex: MockMemex, temp_git_repo: Path
) -> None:
    """The agent-surface body must NOT appear in additionalContext — that's
    the whole point of the rules-file route. Pinning this prevents a future
    refactor from accidentally re-inlining the surface and re-hitting the
    10K cap."""
    result = run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    ctx = out['hookSpecificOutput']['additionalContext']
    assert '<mock-agent-surface' not in ctx, (
        f'agent-surface body leaked into additionalContext: {ctx[:400]!r}'
    )


def test_additional_context_under_10k_chars(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    """additionalContext must stay under Claude Code's hardcoded 10K-char
    `persistHookOutput` cap (Anthropic-side #42369). Above the cap the
    harness truncates to a 2KB preview and the briefing is lost."""
    mock_memex.set_kv('app:claude-code:project:github.com/acme/myapp:vault', 'eng-vault')
    result = run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    ctx = out['hookSpecificOutput']['additionalContext']
    assert len(ctx) < 10_000, (
        f'additionalContext is {len(ctx)} chars — exceeds the harness 10K cap; '
        f'first 400 chars: {ctx[:400]!r}'
    )


def test_agent_surface_install_failure_does_not_block_briefing(
    mock_memex: MockMemex, temp_git_repo: Path
) -> None:
    """If the agent-surface install fails (e.g. older plugin install pinned
    to a version that predates the --output-dir flag), the hook must
    degrade gracefully — the dynamic briefing still lands in additionalContext."""
    mock_memex.force_fail('agent-surface claude-code')

    result = run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    ctx = out['hookSpecificOutput']['additionalContext']
    assert '(mock briefing content)' in ctx
    # No rule file should have been written either.
    assert not (temp_git_repo / '.claude' / 'rules' / 'memex-agent-surface.md').is_file()


def test_memex_missing_emits_exactly_one_json_document(
    mock_memex: MockMemex, temp_git_repo: Path, tmp_path: Path
) -> None:
    """When the `memex` CLI is absent from PATH, ``resolve_config.sh`` emits
    the user-actionable install systemMessage AND signals
    ``MEMEX_HOOK_ALREADY_EMITTED=1``. The outer hook MUST exit before writing
    a second JSON document — Claude Code's SessionStart contract is one
    document per invocation. Pinned so a future refactor that drops the flag
    set / check fails CI rather than silently doubling output."""
    import shutil

    # Construct a PATH that contains NO memex (use only system bins).
    sandbox_bin = tmp_path / 'no-memex'
    sandbox_bin.mkdir()
    env_no_memex = dict(mock_memex.env)
    env_no_memex['PATH'] = f'{sandbox_bin}:/usr/bin:/bin'
    if shutil.which('memex', path=env_no_memex['PATH']) is not None:
        import pytest

        pytest.skip('a real `memex` is on the system PATH; cannot test the missing-CLI branch')

    result = run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=env_no_memex,
        cwd=temp_git_repo,
    )

    # Exactly one parseable JSON document on stdout.
    docs = [line for line in result.stdout.split('}\n{') if line.strip()]
    stdout = result.stdout.strip()
    assert stdout, f'no stdout emitted; stderr={result.stderr!r}'
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f'stdout is not a single JSON document — memex-missing path emitted '
            f'≥2 documents. stdout={stdout!r}; parse error={e!r}; '
            f'split-detection: {docs!r}'
        ) from None

    # And it must be the install systemMessage, not a misleading "server
    # unreachable" or a downstream one.
    assert 'systemMessage' in parsed
    assert 'Memex CLI not found' in parsed['systemMessage']
    assert 'uv tool install' in parsed['systemMessage']


def test_temp_files_cleaned_up_on_success(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    """The hook's EXIT trap removes `tmp_briefing` on the success path.
    Verify by listing /tmp before/after and asserting no script-created
    temp files remain."""
    before = set(Path('/tmp').glob('tmp.*'))
    result = run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
    after = set(Path('/tmp').glob('tmp.*'))
    leaked = after - before
    assert not leaked, f'hook leaked temp files: {leaked!r}'


# ---------------------------------------------------------------------------
# V4: MEMEX_CC_SESSION_BRIEFING opt-out
# ---------------------------------------------------------------------------


def test_briefing_skipped_when_MEMEX_CC_SESSION_BRIEFING_off(
    mock_memex: MockMemex, temp_git_repo: Path
) -> None:
    """Disabled briefing: no `memex briefing` call, but agent-surface install still runs."""
    result = run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
        extra_env={'MEMEX_CC_SESSION_BRIEFING': 'off'},
    )
    assert result.returncode == 0, result.stderr

    # No `memex briefing` call landed on the mock.
    assert not mock_memex.calls_matching('briefing'), (
        f'expected no briefing call, got: {mock_memex.calls_matching("briefing")}'
    )

    # Agent-surface install MUST still have run — gated upstream of the toggle.
    assert mock_memex.calls_matching('agent-surface', 'claude-code'), (
        'agent-surface install was incorrectly gated by MEMEX_CC_SESSION_BRIEFING'
    )

    parsed = json.loads(result.stdout)
    assert 'Briefing disabled' in parsed.get('systemMessage', '')
    assert '### Per-project vault' in parsed['hookSpecificOutput']['additionalContext']


def test_briefing_runs_when_env_unset(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    """Default-on: unset toggle invokes `memex briefing`."""
    result = run_script(
        'on_session_start.sh',
        stdin=_session_start_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
    assert mock_memex.calls_matching('briefing'), 'briefing should run when toggle is unset'


def test_briefing_toggle_accepts_off_0_false_no_disabled(
    mock_memex: MockMemex, temp_git_repo: Path
) -> None:
    """All five falsy values suppress the briefing fetch."""
    for falsy in ('off', '0', 'false', 'no', 'disabled'):
        # Clear prior calls between iterations
        if mock_memex.calls_file.exists():
            mock_memex.calls_file.unlink()
        result = run_script(
            'on_session_start.sh',
            stdin=_session_start_payload(),
            env=mock_memex.env,
            cwd=temp_git_repo,
            extra_env={'MEMEX_CC_SESSION_BRIEFING': falsy},
        )
        assert result.returncode == 0, f'falsy={falsy} stderr={result.stderr}'
        assert not mock_memex.calls_matching('briefing'), (
            f'falsy value {falsy!r} did not suppress briefing'
        )


def test_briefing_toggle_treats_unknown_value_as_on(
    mock_memex: MockMemex, temp_git_repo: Path
) -> None:
    """Anything not in the falsy set runs the fetch — documented asymmetric contract."""
    for not_falsy in ('true', '1', 'yes', 'random-garbage', 'ON'):
        if mock_memex.calls_file.exists():
            mock_memex.calls_file.unlink()
        result = run_script(
            'on_session_start.sh',
            stdin=_session_start_payload(),
            env=mock_memex.env,
            cwd=temp_git_repo,
            extra_env={'MEMEX_CC_SESSION_BRIEFING': not_falsy},
        )
        assert result.returncode == 0, f'value={not_falsy} stderr={result.stderr}'
        assert mock_memex.calls_matching('briefing'), (
            f'value {not_falsy!r} unexpectedly suppressed briefing'
        )
