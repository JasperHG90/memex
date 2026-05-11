"""Unit tests for the resolver helpers in ``scripts/resolve_config.sh``.

The helpers are bash functions; we exercise them via a small wrapper script
that sources the file and prints the result.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from _helpers import MockMemex, PLUGIN_ROOT


def _run_resolver(
    snippet: str,
    *,
    env: dict[str, str],
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Source resolve_config.sh and execute the snippet, capturing stdout."""
    wrapper = f'set -uo pipefail\nsource "{PLUGIN_ROOT}/scripts/resolve_config.sh"\n{snippet}\n'
    return subprocess.run(
        ['bash', '-c', wrapper],
        env=env,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=15.0,
    )


def test_project_id_from_git_remote(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    result = _run_resolver(
        'memex_resolve_project_id',
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0
    # Origin is https://github.com/acme/myapp.git → normalized to host/path
    assert result.stdout.strip() == 'github.com/acme/myapp'


@pytest.mark.parametrize(
    'remote_url',
    [
        'https://github.com/acme/myapp.git',
        'https://oauth2:token@github.com/acme/myapp.git',
        'git@github.com:acme/myapp.git',
        'ssh://git@github.com/acme/myapp.git',
    ],
)
def test_project_id_normalizes_url_formats(
    mock_memex: MockMemex, tmp_path: Path, remote_url: str
) -> None:
    """All common URL formats for the same repo must produce the same project ID.

    Otherwise a contributor cloning via SSH gets a different KV key than one
    cloning via HTTPS — and the per-project vault binding splits in two.
    """
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    subprocess.run(['git', 'remote', 'add', 'origin', remote_url], cwd=repo, check=True)
    result = _run_resolver(
        'memex_resolve_project_id',
        env=mock_memex.env,
        cwd=repo,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'github.com/acme/myapp'


@pytest.mark.parametrize(
    'remote_url,expected',
    [
        ('https://github.com/acme/myapp.git', 'github.com/acme/myapp'),
        ('git@github.com:acme/myapp.git', 'github.com/acme/myapp'),
        ('https://gitlab.com/org/subgroup/repo.git', 'gitlab.com/org/subgroup/repo'),
        ('git@gitlab.com:org/subgroup/repo.git', 'gitlab.com/org/subgroup/repo'),
    ],
)
def test_normalize_git_remote_url_preserves_nested_subgroups(
    mock_memex: MockMemex, remote_url: str, expected: str
) -> None:
    """`memex_normalize_git_remote_url` must keep every path segment.

    Nested forges (GitLab, self-hosted Gitea, …) expose repos under multiple
    path components; collapsing to the last two segments mis-identifies them.
    """
    result = _run_resolver(
        f'memex_normalize_git_remote_url "{remote_url}"',
        env=mock_memex.env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_project_id_falls_back_to_path_outside_repo(mock_memex: MockMemex, tmp_path: Path) -> None:
    workdir = tmp_path / 'plain'
    workdir.mkdir()
    result = _run_resolver(
        'memex_resolve_project_id',
        env=mock_memex.env,
        cwd=workdir,
    )
    assert result.returncode == 0
    out = result.stdout.strip()
    # Should be the path (relative to HOME if applicable, else absolute)
    assert out
    assert ' ' not in out  # Sanity: no spaces


def test_active_vault_resolves_from_project_kv(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    mock_memex.set_kv('app:claude-code:project:github.com/acme/myapp:vault', 'project-vault')
    result = _run_resolver(
        'memex_resolve_active_vault',
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == 'project-vault'


def test_active_vault_falls_back_to_user(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    user_env = {**mock_memex.env, 'USER': 'jasper'}
    mock_memex.set_kv('app:claude-code:user:jasper:vault', 'user-vault')
    # No project-level binding
    result = _run_resolver(
        'memex_resolve_active_vault',
        env=user_env,
        cwd=temp_git_repo,
    )
    assert result.stdout.strip() == 'user-vault'


def test_active_vault_falls_back_to_agent(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    env = {**mock_memex.env, 'MEMEX_CC_AGENT_ID': 'devops'}
    mock_memex.set_kv('app:claude-code:agent:devops:vault', 'agent-vault')
    result = _run_resolver(
        'memex_resolve_active_vault',
        env=env,
        cwd=temp_git_repo,
    )
    assert result.stdout.strip() == 'agent-vault'


def test_active_vault_falls_back_to_env_var(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    env = {**mock_memex.env, 'MEMEX_VAULT': 'env-vault'}
    result = _run_resolver(
        'memex_resolve_active_vault',
        env=env,
        cwd=temp_git_repo,
    )
    assert result.stdout.strip() == 'env-vault'


def test_active_vault_returns_empty_when_nothing_set(mock_memex: MockMemex, tmp_path: Path) -> None:
    """No KV bindings, no env var, no agent id — returns empty (server picks default)."""
    workdir = tmp_path / 'empty'
    workdir.mkdir()
    result = _run_resolver(
        'memex_resolve_active_vault',
        env=mock_memex.env,
        cwd=workdir,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ''


def test_active_vault_priority_project_over_user(
    mock_memex: MockMemex, temp_git_repo: Path
) -> None:
    user_env = {**mock_memex.env, 'USER': 'jasper'}
    mock_memex.set_kv('app:claude-code:project:github.com/acme/myapp:vault', 'project-vault')
    mock_memex.set_kv('app:claude-code:user:jasper:vault', 'user-vault')
    result = _run_resolver(
        'memex_resolve_active_vault',
        env=user_env,
        cwd=temp_git_repo,
    )
    assert result.stdout.strip() == 'project-vault'


def test_active_vault_priority_user_over_env(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    env = {**mock_memex.env, 'USER': 'jasper', 'MEMEX_VAULT': 'env-vault'}
    mock_memex.set_kv('app:claude-code:user:jasper:vault', 'user-vault')
    result = _run_resolver(
        'memex_resolve_active_vault',
        env=env,
        cwd=temp_git_repo,
    )
    assert result.stdout.strip() == 'user-vault'


def test_kv_namespace_migration_reads_legacy_key(
    mock_memex: MockMemex, temp_git_repo: Path
) -> None:
    """If only the bare project: key is set, the resolver returns it AND
    forward-migrates to the new namespaced key."""
    mock_memex.set_kv('project:github.com/acme/myapp:vault', 'legacy-vault')
    # New-namespace key is intentionally NOT set
    result = _run_resolver(
        'memex_resolve_active_vault',
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.stdout.strip() == 'legacy-vault'

    # Inspect mock KV state — the new key should have been written
    kv_text = mock_memex.kv_file.read_text()
    assert 'app:claude-code:project:github.com/acme/myapp:vault=legacy-vault' in kv_text


def test_kv_namespace_migration_falls_back_when_new_key_empty(
    mock_memex: MockMemex, temp_git_repo: Path
) -> None:
    """Empty-string value in the new key must NOT shadow a legacy value."""
    mock_memex.set_kv('app:claude-code:project:github.com/acme/myapp:vault', '')
    mock_memex.set_kv('project:github.com/acme/myapp:vault', 'legacy-vault')
    result = _run_resolver(
        'memex_resolve_active_vault',
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.stdout.strip() == 'legacy-vault'


def test_kv_namespace_migration_prefers_new_key_when_both_exist(
    mock_memex: MockMemex, temp_git_repo: Path
) -> None:
    mock_memex.set_kv('project:github.com/acme/myapp:vault', 'legacy-vault')
    mock_memex.set_kv('app:claude-code:project:github.com/acme/myapp:vault', 'new-vault')
    result = _run_resolver(
        'memex_resolve_active_vault',
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.stdout.strip() == 'new-vault'


@pytest.mark.parametrize(
    'raw,expected_timeout',
    [
        ('', '8'),  # empty string → default
        ('abc', '8'),  # non-numeric → default
        ('-5', '8'),  # negative (dash matches *[!0-9]*) → default
        ('0', '8'),  # zero → too low, default
        ('1', '1'),  # min → respected
        ('60', '60'),  # mid-range → respected
        ('600', '600'),  # max → respected
        ('601', '600'),  # over max → clamped
        ('99999999999999999999', '8'),  # overflow → default
    ],
)
def test_memex_timeout_validation(
    mock_memex: MockMemex, tmp_path: Path, raw: str, expected_timeout: str
) -> None:
    """The memex() wrapper must clamp MEMEX_CC_TIMEOUT to [1, 600]."""
    # Install a PATH-priority `timeout` shim that records its first argument
    # and exits successfully without running the wrapped command.
    probe_bin = tmp_path / 'probe_bin'
    probe_bin.mkdir()
    record_file = tmp_path / 'timeout_arg.txt'
    shim = probe_bin / 'timeout'
    shim.write_text(f'#!/usr/bin/env bash\nprintf "%s" "$1" > {record_file}\nexit 0\n')
    shim.chmod(0o755)

    env = {
        **mock_memex.env,
        'MEMEX_CC_TIMEOUT': raw,
        'PATH': f'{probe_bin}:{mock_memex.env["PATH"]}',
    }
    probe = (
        f'source "{PLUGIN_ROOT}/scripts/resolve_config.sh"\n'
        'memex --version >/dev/null 2>&1 || true\n'
    )
    subprocess.run(
        ['bash', '-c', probe],
        env=env,
        capture_output=True,
        text=True,
        timeout=15.0,
    )
    assert record_file.exists(), 'timeout shim never recorded a call'
    actual = record_file.read_text()
    assert actual == expected_timeout, (
        f'For MEMEX_CC_TIMEOUT={raw!r}, expected timeout={expected_timeout}, got {actual}'
    )


def test_resolved_vault_caches_within_invocation(
    mock_memex: MockMemex, temp_git_repo: Path
) -> None:
    """Two direct (non-subshell) calls in one bash process must hit the cache."""
    mock_memex.set_kv('app:claude-code:project:github.com/acme/myapp:vault', 'cached-vault')
    # Direct calls (no $(...)) so the export propagates between calls.
    result = _run_resolver(
        'memex_resolve_active_vault\nprintf "|"\nmemex_resolve_active_vault\n',
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == 'cached-vault|cached-vault'
    kv_get_calls = mock_memex.calls_matching(
        'kv', 'get', 'app:claude-code:project:github.com/acme/myapp:vault'
    )
    assert len(kv_get_calls) == 1, (
        f'Expected exactly one KV read for the cache hit, got {len(kv_get_calls)}'
    )
