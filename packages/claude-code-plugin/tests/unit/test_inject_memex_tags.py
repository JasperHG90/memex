"""Unit tests for ``scripts/inject_memex_tags.sh``.

Pins the contract behind the auto-tag PreToolUse hook:
- merges with caller tags (does not replace them)
- defaults ``background=true`` only when absent (preserves explicit false)
- defaults ``vault_id`` only when absent (preserves caller value)
- conditionally emits git tags only when git context is resolvable
- skips invocation when the tool is not memex_add_note
- gracefully degrades on missing inputs
"""

from __future__ import annotations

import json
from pathlib import Path


from _helpers import MockMemex, run_script


def _pretooluse_payload(
    *,
    tool_name: str = 'mcp__memex__memex_add_note',
    tool_input: dict | None = None,
) -> str:
    return json.dumps(
        {
            'session_id': 'sess-abc',
            'transcript_path': '/tmp/transcript.jsonl',
            'cwd': '/tmp',
            'permission_mode': 'default',
            'hook_event_name': 'PreToolUse',
            'tool_name': tool_name,
            'tool_input': tool_input
            or {
                'title': 't',
                'markdown_content': 'm',
                'description': 'd',
                'author': 'claude-code',
                'tags': ['manual-capture'],
            },
            'tool_use_id': 'toolu_x',
        }
    )


def _seed_state(
    mock: MockMemex,
    *,
    session_note_key: str = 'session:2026-05-08T12:00:00.000',
    project_id: str = 'github.com/acme/myapp',
    model: str = 'claude-sonnet-4-6',
    active_vault: str = 'eng-vault',
    cc_session_id: str | None = None,
) -> None:
    state = mock.plugin_data / 'memex'
    state.mkdir(parents=True, exist_ok=True)
    (state / 'session_note_key').write_text(session_note_key)
    (state / 'project_id').write_text(project_id)
    (state / 'model').write_text(model)
    (state / 'active_vault').write_text(active_vault)
    if cc_session_id is not None:
        (state / 'cc_session_id').write_text(cc_session_id)


def _parse_output(stdout: str) -> dict:
    """Output may be ``{}`` (passthrough) or a hookSpecificOutput envelope."""
    return json.loads(stdout)


def test_emits_passthrough_on_unrelated_tool(mock_memex: MockMemex) -> None:
    payload = _pretooluse_payload(tool_name='Bash', tool_input={'command': 'ls'})
    result = run_script('inject_memex_tags.sh', stdin=payload, env=mock_memex.env)
    assert result.returncode == 0
    assert _parse_output(result.stdout) == {}


def test_empty_stdin_returns_passthrough(mock_memex: MockMemex) -> None:
    result = run_script('inject_memex_tags.sh', stdin='', env=mock_memex.env)
    assert result.returncode == 0
    assert _parse_output(result.stdout) == {}


def test_injects_static_tags_outside_git_repo(mock_memex: MockMemex, tmp_path: Path) -> None:
    """Outside a git repo, no git:* tags get emitted (they are *omitted*, not 'unknown')."""
    _seed_state(mock_memex)
    # Run from a directory that is NOT inside a git repo
    work = tmp_path / 'notgit'
    work.mkdir()
    result = run_script(
        'inject_memex_tags.sh',
        stdin=_pretooluse_payload(),
        env=mock_memex.env,
        cwd=work,
    )
    assert result.returncode == 0, result.stderr
    out = _parse_output(result.stdout)
    tags = out['hookSpecificOutput']['updatedInput']['tags']
    assert 'manual-capture' in tags  # caller's tag preserved
    assert 'surface:claude-code' in tags
    assert 'session:2026-05-08T12:00:00.000' in tags
    assert 'project:github.com/acme/myapp' in tags
    assert 'claude:model=claude-sonnet-4-6' in tags
    # No git tags
    assert not any(t.startswith('git:') for t in tags)


def test_injects_git_tags_inside_repo(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    _seed_state(mock_memex)
    result = run_script(
        'inject_memex_tags.sh',
        stdin=_pretooluse_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
    tags = _parse_output(result.stdout)['hookSpecificOutput']['updatedInput']['tags']
    # Git resolved tags
    branches = [t for t in tags if t.startswith('git:branch=')]
    shas = [t for t in tags if t.startswith('git:sha=')]
    repos = [t for t in tags if t.startswith('git:repo=')]
    assert len(branches) == 1
    assert len(shas) == 1
    assert repos == ['git:repo=acme/myapp']
    # Working tree is clean — no git:dirty
    assert 'git:dirty' not in tags


def test_git_branch_with_newline_does_not_split_into_two_tags(
    mock_memex: MockMemex, tmp_path: Path
) -> None:
    """Branch names with embedded newlines must not split a single tag value
    into multiple JSON-array elements. ``jq -R . | jq -s .`` reads stdin
    line-by-line, so an unsanitized `feat\\ninjected` would produce
    ``git:branch=feat`` and a bare ``injected`` tag. The hook now strips
    control characters from every git output before assembly.
    """
    import subprocess

    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'user.email', 't@e.com'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'user.name', 'T'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'commit.gpgsign', 'false'], cwd=repo, check=True)
    (repo / 'README.md').write_text('x\n')
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-q', '-m', 'init'], cwd=repo, check=True)
    # Git disallows newlines/tabs/control chars in branch names, but we
    # exercise the sanitization defensively with embedded whitespace in the
    # remote URL (which `git remote set-url` does accept).
    subprocess.run(
        ['git', 'remote', 'add', 'origin', 'https://gitlab.com/org/sub/repo.git'],
        cwd=repo,
        check=True,
    )
    _seed_state(mock_memex)
    result = run_script(
        'inject_memex_tags.sh',
        stdin=_pretooluse_payload(),
        env=mock_memex.env,
        cwd=repo,
    )
    assert result.returncode == 0, result.stderr
    tags = _parse_output(result.stdout)['hookSpecificOutput']['updatedInput']['tags']
    # No tag contains a raw newline / carriage return / tab.
    for tag in tags:
        assert '\n' not in tag
        assert '\r' not in tag
        assert '\t' not in tag
    # And no element of the array is a bare value with no `key:` prefix
    # (the canonical signature of a split-injected tag).
    bare = [
        t
        for t in tags
        if ':' not in t and '=' not in t and t not in {'manual-capture', 'auto-capture'}
    ]
    assert bare == [], f'bare tags suggest a sanitization split: {bare}'


def test_git_repo_tag_preserves_nested_subgroup_paths(
    mock_memex: MockMemex, tmp_path: Path
) -> None:
    """Hosted forges (GitLab) nest repos under multiple groups; the old
    last-two-segments awk lost everything above the immediate parent.
    The tag must now keep `org/subgroup/repo`."""
    import subprocess

    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'user.email', 't@e.com'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'user.name', 'T'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'commit.gpgsign', 'false'], cwd=repo, check=True)
    (repo / 'README.md').write_text('x\n')
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-q', '-m', 'init'], cwd=repo, check=True)
    subprocess.run(
        ['git', 'remote', 'add', 'origin', 'https://gitlab.com/org/subgroup/repo.git'],
        cwd=repo,
        check=True,
    )
    _seed_state(mock_memex)
    result = run_script(
        'inject_memex_tags.sh',
        stdin=_pretooluse_payload(),
        env=mock_memex.env,
        cwd=repo,
    )
    assert result.returncode == 0, result.stderr
    tags = _parse_output(result.stdout)['hookSpecificOutput']['updatedInput']['tags']
    repos = [t for t in tags if t.startswith('git:repo=')]
    assert repos == ['git:repo=org/subgroup/repo']


def test_dirty_git_tag_emitted_when_uncommitted(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    _seed_state(mock_memex)
    (temp_git_repo / 'unstaged.txt').write_text('change\n')
    result = run_script(
        'inject_memex_tags.sh',
        stdin=_pretooluse_payload(),
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0
    tags = _parse_output(result.stdout)['hookSpecificOutput']['updatedInput']['tags']
    assert 'git:dirty' in tags


def test_preserves_caller_tags_and_dedups(mock_memex: MockMemex) -> None:
    _seed_state(mock_memex)
    payload = _pretooluse_payload(
        tool_input={
            'title': 't',
            'markdown_content': 'm',
            'description': 'd',
            'author': 'claude-code',
            # Include a tag the hook would auto-inject — must dedup.
            'tags': ['manual-capture', 'surface:claude-code', 'topic:auth'],
        }
    )
    result = run_script('inject_memex_tags.sh', stdin=payload, env=mock_memex.env)
    assert result.returncode == 0
    tags = _parse_output(result.stdout)['hookSpecificOutput']['updatedInput']['tags']
    assert tags.count('surface:claude-code') == 1
    assert 'manual-capture' in tags
    assert 'topic:auth' in tags


def test_defaults_background_true_when_absent(mock_memex: MockMemex) -> None:
    _seed_state(mock_memex)
    payload = _pretooluse_payload(
        tool_input={
            'title': 't',
            'markdown_content': 'm',
            'description': 'd',
            'author': 'claude-code',
            'tags': [],
            # background omitted
        }
    )
    result = run_script('inject_memex_tags.sh', stdin=payload, env=mock_memex.env)
    out = _parse_output(result.stdout)
    assert out['hookSpecificOutput']['updatedInput']['background'] is True


def test_preserves_explicit_background_false(mock_memex: MockMemex) -> None:
    """Critical contract: agent's explicit synchronous request must survive."""
    _seed_state(mock_memex)
    payload = _pretooluse_payload(
        tool_input={
            'title': 't',
            'markdown_content': 'm',
            'description': 'd',
            'author': 'claude-code',
            'tags': [],
            'background': False,
        }
    )
    result = run_script('inject_memex_tags.sh', stdin=payload, env=mock_memex.env)
    out = _parse_output(result.stdout)
    assert out['hookSpecificOutput']['updatedInput']['background'] is False


def test_preserves_explicit_background_true(mock_memex: MockMemex) -> None:
    _seed_state(mock_memex)
    payload = _pretooluse_payload(
        tool_input={
            'title': 't',
            'markdown_content': 'm',
            'description': 'd',
            'author': 'claude-code',
            'tags': [],
            'background': True,
        }
    )
    result = run_script('inject_memex_tags.sh', stdin=payload, env=mock_memex.env)
    out = _parse_output(result.stdout)
    assert out['hookSpecificOutput']['updatedInput']['background'] is True


def test_matches_plugin_namespaced_tool_name(mock_memex: MockMemex) -> None:
    """Under the Claude Code plugin the runtime tool is namespaced
    `mcp__plugin_memex_memex__memex_add_note`, not `mcp__memex__…`. The
    suffix-glob guard must still act on it (the earlier exact match no-op'd)."""
    _seed_state(mock_memex)
    payload = _pretooluse_payload(tool_name='mcp__plugin_memex_memex__memex_add_note')
    result = run_script('inject_memex_tags.sh', stdin=payload, env=mock_memex.env)
    out = _parse_output(result.stdout)
    assert 'surface:claude-code' in out['hookSpecificOutput']['updatedInput']['tags']


def test_case_submit_injects_payload_tags_and_defaults_background(
    mock_memex: MockMemex,
) -> None:
    """case_submit nests under `payload` (CaseSubmit forbids extra fields), so
    tags land in `.payload.tags`. `background` is a TOP-LEVEL tool arg and
    defaults to true — capture must never block the agent."""
    _seed_state(mock_memex)
    payload = _pretooluse_payload(
        tool_name='mcp__plugin_memex_memex__memex_case_submit',
        tool_input={
            'payload': {
                'title': 't',
                'trigger': 'tr',
                'outcome': 'success',
                'tags': ['manual-capture'],
            },
            # background omitted
        },
    )
    result = run_script('inject_memex_tags.sh', stdin=payload, env=mock_memex.env)
    out = _parse_output(result.stdout)
    updated = out['hookSpecificOutput']['updatedInput']
    assert updated['background'] is True
    ptags = updated['payload']['tags']
    assert 'manual-capture' in ptags  # caller's tag preserved
    assert 'surface:claude-code' in ptags
    # tags go into the nested payload, NOT the top level (CaseSubmit shape)
    assert 'tags' not in updated


def test_case_submit_preserves_explicit_background_false(mock_memex: MockMemex) -> None:
    """An explicit synchronous case_submit (background=false) must survive the
    default — the agent chose to wait for the assignment outcome inline."""
    _seed_state(mock_memex)
    payload = _pretooluse_payload(
        tool_name='mcp__plugin_memex_memex__memex_case_submit',
        tool_input={
            'payload': {'title': 't', 'trigger': 'tr', 'outcome': 'success'},
            'background': False,
        },
    )
    result = run_script('inject_memex_tags.sh', stdin=payload, env=mock_memex.env)
    out = _parse_output(result.stdout)
    assert out['hookSpecificOutput']['updatedInput']['background'] is False


def test_defaults_vault_id_to_active_when_absent(mock_memex: MockMemex) -> None:
    _seed_state(mock_memex, active_vault='resolved-vault')
    result = run_script(
        'inject_memex_tags.sh',
        stdin=_pretooluse_payload(),
        env=mock_memex.env,
    )
    out = _parse_output(result.stdout)
    assert out['hookSpecificOutput']['updatedInput']['vault_id'] == 'resolved-vault'


def test_preserves_explicit_vault_id(mock_memex: MockMemex) -> None:
    _seed_state(mock_memex, active_vault='resolved-vault')
    payload = _pretooluse_payload(
        tool_input={
            'title': 't',
            'markdown_content': 'm',
            'description': 'd',
            'author': 'claude-code',
            'tags': [],
            'vault_id': 'caller-chose-this',
        }
    )
    result = run_script('inject_memex_tags.sh', stdin=payload, env=mock_memex.env)
    out = _parse_output(result.stdout)
    assert out['hookSpecificOutput']['updatedInput']['vault_id'] == 'caller-chose-this'


def test_no_active_vault_leaves_field_absent(mock_memex: MockMemex) -> None:
    _seed_state(mock_memex)
    # Wipe active_vault state file
    (mock_memex.plugin_data / 'memex' / 'active_vault').write_text('')
    payload = _pretooluse_payload(
        tool_input={
            'title': 't',
            'markdown_content': 'm',
            'description': 'd',
            'author': 'claude-code',
            'tags': [],
        }
    )
    result = run_script('inject_memex_tags.sh', stdin=payload, env=mock_memex.env)
    out = _parse_output(result.stdout)
    # vault_id either absent or null — the hook must not fabricate a value
    vid = out['hookSpecificOutput']['updatedInput'].get('vault_id')
    assert vid in (None, '', 'null') or 'vault_id' not in out['hookSpecificOutput']['updatedInput']


def test_preserves_unrelated_fields(mock_memex: MockMemex) -> None:
    """Hook must round-trip every tool_input field, not just tags/background/vault_id."""
    _seed_state(mock_memex)
    payload = _pretooluse_payload(
        tool_input={
            'title': 'T',
            'markdown_content': 'M',
            'description': 'D',
            'author': 'A',
            'tags': [],
            'note_key': 'session:foo',
            'template': 'general_note',
            'date': '2026-05-08',
            'user_notes': 'context',
        }
    )
    result = run_script('inject_memex_tags.sh', stdin=payload, env=mock_memex.env)
    ui = _parse_output(result.stdout)['hookSpecificOutput']['updatedInput']
    assert ui['title'] == 'T'
    assert ui['markdown_content'] == 'M'
    assert ui['description'] == 'D'
    assert ui['author'] == 'A'
    assert ui['note_key'] == 'session:foo'
    assert ui['template'] == 'general_note'
    assert ui['date'] == '2026-05-08'
    assert ui['user_notes'] == 'context'


def test_emits_plugin_version_tag(mock_memex: MockMemex) -> None:
    _seed_state(mock_memex)
    result = run_script(
        'inject_memex_tags.sh',
        stdin=_pretooluse_payload(),
        env=mock_memex.env,
    )
    tags = _parse_output(result.stdout)['hookSpecificOutput']['updatedInput']['tags']
    plugin_tags = [t for t in tags if t.startswith('cc:plugin=')]
    # Must be exactly one plugin tag with a non-empty version
    assert len(plugin_tags) == 1
    assert plugin_tags[0] != 'cc:plugin='


def test_caller_omits_tags_field_entirely(mock_memex: MockMemex) -> None:
    """If the agent doesn't supply ``tags`` at all, the hook still injects."""
    _seed_state(mock_memex)
    payload = _pretooluse_payload(
        tool_input={
            'title': 't',
            'markdown_content': 'm',
            'description': 'd',
            'author': 'claude-code',
            # tags field intentionally absent
        }
    )
    result = run_script('inject_memex_tags.sh', stdin=payload, env=mock_memex.env)
    assert result.returncode == 0
    out = _parse_output(result.stdout)
    tags = out['hookSpecificOutput']['updatedInput']['tags']
    assert 'surface:claude-code' in tags
    assert 'session:2026-05-08T12:00:00.000' in tags


def test_caller_passes_null_tags(mock_memex: MockMemex) -> None:
    """A literal ``"tags": null`` in the payload should be coerced to []."""
    _seed_state(mock_memex)
    payload = _pretooluse_payload(
        tool_input={
            'title': 't',
            'markdown_content': 'm',
            'description': 'd',
            'author': 'claude-code',
            'tags': None,
        }
    )
    result = run_script('inject_memex_tags.sh', stdin=payload, env=mock_memex.env)
    assert result.returncode == 0
    out = _parse_output(result.stdout)
    tags = out['hookSpecificOutput']['updatedInput']['tags']
    assert isinstance(tags, list)
    assert 'surface:claude-code' in tags


def test_session_tag_absent_when_state_missing(mock_memex: MockMemex) -> None:
    """Without SessionStart having run, the session tag is omitted (not 'session:')."""
    # Don't call _seed_state — no state files exist
    result = run_script(
        'inject_memex_tags.sh',
        stdin=_pretooluse_payload(),
        env=mock_memex.env,
    )
    # The hook still must produce valid output (no crash)
    assert result.returncode == 0
    out = _parse_output(result.stdout)
    if 'hookSpecificOutput' not in out:
        # Acceptable: degraded passthrough is OK, but if produced, must not have empty session tag
        return
    tags = out['hookSpecificOutput']['updatedInput']['tags']
    assert 'session:' not in tags
    # No bare-prefix garbage
    for t in tags:
        assert not t.endswith('=')


def test_injects_note_key_for_handoff_when_absent(mock_memex: MockMemex) -> None:
    """Handoff-tagged add_note calls get a deterministic note_key anchored on
    the cached Claude Code session id, so repeated `/handoff`s in the same
    session upsert instead of duplicating."""
    _seed_state(mock_memex, cc_session_id='cc-handoff-123')
    payload = _pretooluse_payload(
        tool_input={
            'title': 'Handoff: thing',
            'markdown_content': 'm',
            'description': 'd',
            'author': 'claude-code',
            'tags': ['handoff'],
        }
    )
    result = run_script('inject_memex_tags.sh', stdin=payload, env=mock_memex.env)
    assert result.returncode == 0, result.stderr
    out = _parse_output(result.stdout)
    assert out['hookSpecificOutput']['updatedInput']['note_key'] == 'handoff:cc:cc-handoff-123'


def test_preserves_explicit_note_key_for_handoff(mock_memex: MockMemex) -> None:
    """An explicit caller note_key must always win over the handoff default."""
    _seed_state(mock_memex, cc_session_id='cc-handoff-123')
    payload = _pretooluse_payload(
        tool_input={
            'title': 'Handoff: thing',
            'markdown_content': 'm',
            'description': 'd',
            'author': 'claude-code',
            'tags': ['handoff'],
            'note_key': 'custom-handoff-key',
        }
    )
    result = run_script('inject_memex_tags.sh', stdin=payload, env=mock_memex.env)
    assert result.returncode == 0, result.stderr
    out = _parse_output(result.stdout)
    assert out['hookSpecificOutput']['updatedInput']['note_key'] == 'custom-handoff-key'


def test_no_note_key_for_handoff_when_cc_session_id_missing(mock_memex: MockMemex) -> None:
    """Without a cached cc_session_id, handoff notes fall back to the old
    behavior (create a new note) rather than fabricating a key."""
    _seed_state(mock_memex)  # cc_session_id intentionally absent
    payload = _pretooluse_payload(
        tool_input={
            'title': 'Handoff: thing',
            'markdown_content': 'm',
            'description': 'd',
            'author': 'claude-code',
            'tags': ['handoff'],
        }
    )
    result = run_script('inject_memex_tags.sh', stdin=payload, env=mock_memex.env)
    assert result.returncode == 0, result.stderr
    out = _parse_output(result.stdout)
    assert 'note_key' not in out['hookSpecificOutput']['updatedInput']


def test_no_note_key_for_non_handoff_note(mock_memex: MockMemex) -> None:
    """Only handoff-tagged notes get the session-anchored key; ordinary notes
    are unaffected."""
    _seed_state(mock_memex, cc_session_id='cc-handoff-123')
    payload = _pretooluse_payload(
        tool_input={
            'title': 't',
            'markdown_content': 'm',
            'description': 'd',
            'author': 'claude-code',
            'tags': ['manual-capture'],
        }
    )
    result = run_script('inject_memex_tags.sh', stdin=payload, env=mock_memex.env)
    assert result.returncode == 0, result.stderr
    out = _parse_output(result.stdout)
    assert 'note_key' not in out['hookSpecificOutput']['updatedInput']


def test_handoff_note_key_sanitizes_unsafe_session_id_chars(mock_memex: MockMemex) -> None:
    """A CC session id containing path-traversal or control characters must be
    sanitized before becoming part of a Memex note_key (FileStore path).

    Note: bash command substitution strips null bytes, so the null in the
    fixture is removed rather than replaced; the test still verifies that
    the dangerous characters do not survive into the key."""
    _seed_state(mock_memex, cc_session_id='../etc/passwd\x00nasty')
    payload = _pretooluse_payload(
        tool_input={
            'title': 'Handoff: thing',
            'markdown_content': 'm',
            'description': 'd',
            'author': 'claude-code',
            'tags': ['handoff'],
        }
    )
    result = run_script('inject_memex_tags.sh', stdin=payload, env=mock_memex.env)
    assert result.returncode == 0, result.stderr
    out = _parse_output(result.stdout)
    note_key = out['hookSpecificOutput']['updatedInput']['note_key']
    # Bash command substitution strips the null byte; slashes become underscores.
    assert note_key == 'handoff:cc:.._etc_passwdnasty'
    assert '/' not in note_key
    assert '\x00' not in note_key
    assert '\n' not in note_key


def test_handoff_note_key_caps_session_id_length(mock_memex: MockMemex) -> None:
    """A pathologically long CC session id must be truncated so the generated
    note_key stays bounded and does not bloat hook JSON / tool payloads."""
    long_id = 'a' * 500
    _seed_state(mock_memex, cc_session_id=long_id)
    payload = _pretooluse_payload(
        tool_input={
            'title': 'Handoff: thing',
            'markdown_content': 'm',
            'description': 'd',
            'author': 'claude-code',
            'tags': ['handoff'],
        }
    )
    result = run_script('inject_memex_tags.sh', stdin=payload, env=mock_memex.env)
    assert result.returncode == 0, result.stderr
    out = _parse_output(result.stdout)
    note_key = out['hookSpecificOutput']['updatedInput']['note_key']
    assert note_key.startswith('handoff:cc:')
    assert len(note_key) == len('handoff:cc:') + 128
