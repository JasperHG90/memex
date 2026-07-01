"""Unit tests for the resolver helpers in ``scripts/resolve_config.sh``.

The helpers are bash functions; we exercise them via a small wrapper script
that sources the file and prints the result.
"""

from __future__ import annotations

import shutil
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
        # SSH with explicit port — the `:22` must not leak into the project ID,
        # otherwise the same repo cloned over ports 22 vs 2222 splits across
        # different KV keys.
        ('ssh://git@github.com:22/acme/myapp.git', 'github.com/acme/myapp'),
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
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'user-vault'


def test_active_vault_falls_back_to_agent(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    env = {**mock_memex.env, 'MEMEX_CC_AGENT_ID': 'devops'}
    mock_memex.set_kv('app:claude-code:agent:devops:vault', 'agent-vault')
    result = _run_resolver(
        'memex_resolve_active_vault',
        env=env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'agent-vault'


def test_missing_memex_cli_emits_install_diagnostic(mock_memex: MockMemex, tmp_path: Path) -> None:
    """When `memex` is not on PATH, the resolver must emit CLEAR install
    instructions — not the old misleading "server unreachable" — and stub
    memex() so sourcing still succeeds.

    Guards against the regression where the hook built its own
    `uvx --from git@latest` copy (stale / missing extras) instead of using
    the CLI the user installed.
    """
    empty_bin = tmp_path / 'empty_bin'
    empty_bin.mkdir()
    env = {
        **mock_memex.env,
        # PATH WITHOUT the mock `memex`/`uvx` shims (bin_dir dropped) — only
        # system coreutils, so `command -v memex` genuinely fails.
        'PATH': f'{empty_bin}:/usr/bin:/bin',
        'MEMEX_RESOLVE_VERBOSE': '1',
    }
    if shutil.which('memex', path=env['PATH']) is not None:
        pytest.skip('a real `memex` is on the system PATH; cannot test the missing-CLI branch')

    result = _run_resolver(
        'memex briefing >/dev/null 2>&1; echo "stub_rc=$?"',
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert 'Memex CLI not found' in result.stdout
    assert 'uv tool install' in result.stdout
    # The stubbed memex() returns non-zero so callers fail cleanly.
    assert 'stub_rc=1' in result.stdout


def test_present_memex_cli_dispatches_to_path_binary(
    mock_memex: MockMemex, temp_git_repo: Path
) -> None:
    """With `memex` on PATH (the installed CLI), the wrapper dispatches to it
    directly — recorded by the mock — rather than building a git copy."""
    result = _run_resolver(
        'memex briefing --budget 100 >/dev/null 2>&1; echo done',
        env=mock_memex.env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
    briefing_calls = mock_memex.calls_matching('briefing')
    assert len(briefing_calls) == 1, f'expected one briefing dispatch, got {mock_memex.calls()!r}'


def test_active_vault_falls_back_to_env_var(mock_memex: MockMemex, temp_git_repo: Path) -> None:
    env = {**mock_memex.env, 'MEMEX_VAULT': 'env-vault'}
    result = _run_resolver(
        'memex_resolve_active_vault',
        env=env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, result.stderr
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


def test_local_path_dispatches_uv_run_with_workspace_project(
    mock_memex: MockMemex, tmp_path: Path
) -> None:
    """When MEMEX_LOCAL_PATH is set, `memex` must dispatch via
    `uv run --project <local-path> --package memex-cli memex …`, bypassing
    the `uvx --from git+…` plugin-distribution path.

    The eval suite uses this so ollama-claude (which would otherwise pull
    `memex-cli @ git+…@latest` from GitHub) runs against the workspace's
    refactored code — without it, the local branch's agent-surface
    composition is invisible to anything that goes through this hook.
    """
    # Shim `uv` so we can record what gets invoked. The shim does NOT need
    # to be functional — we only care that the wrapper picked the right
    # argv shape for the local-path branch.
    probe_bin = tmp_path / 'probe_bin'
    probe_bin.mkdir()
    record_file = tmp_path / 'uv_argv.txt'
    uv_shim = probe_bin / 'uv'
    uv_shim.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {record_file}\nexit 0\n')
    uv_shim.chmod(0o755)
    # Also place a `uvx` shim that records its own (separate) file; if the
    # local-path branch is broken and we fall through to the git path, the
    # uvx file will be written and we can fail with a clearer message.
    uvx_record = tmp_path / 'uvx_argv.txt'
    uvx_shim = probe_bin / 'uvx'
    uvx_shim.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {uvx_record}\nexit 0\n')
    uvx_shim.chmod(0o755)

    workspace = tmp_path / 'fake_workspace'
    workspace.mkdir()

    env = {
        **mock_memex.env,
        'MEMEX_LOCAL_PATH': str(workspace),
        'PATH': f'{probe_bin}:{mock_memex.env["PATH"]}',
    }
    result = _run_resolver(
        'memex --version >/dev/null 2>&1 || true\n',
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert record_file.exists(), (
        f'uv shim was never invoked — local-path branch did not dispatch. '
        f'uvx_record_exists={uvx_record.exists()}, stderr={result.stderr!r}'
    )
    assert not uvx_record.exists(), (
        f'uvx was invoked despite MEMEX_LOCAL_PATH being set — the git+'
        f'https fallback fired instead of the local-path branch. '
        f'uvx argv was: {uvx_record.read_text()!r}'
    )
    lines = record_file.read_text().splitlines()
    # Expect: ['run', '--project', '<workspace>', '--package', 'memex-cli',
    #          'memex', '--version'] — preserved order matters for `uv run`.
    assert lines[0] == 'run', f'Expected first arg "run", got {lines[0]!r}'
    assert '--project' in lines, f'Missing --project flag in {lines!r}'
    assert str(workspace) in lines, f'Workspace path {str(workspace)!r} not in uv argv: {lines!r}'
    assert '--package' in lines, f'Missing --package flag in {lines!r}'
    assert 'memex-cli' in lines, f'Missing memex-cli package name in {lines!r}'
    # The CLI subcommand must come AFTER `memex` (the entrypoint), not
    # before it — if --version arrives before `memex`, uv treats it as a
    # `uv run` flag instead of a CLI arg.
    memex_idx = lines.index('memex')
    assert lines[memex_idx + 1 :] == ['--version'], (
        f'Expected ["--version"] after "memex" entrypoint, got {lines[memex_idx + 1 :]!r}'
    )


def test_local_path_skips_ref_validation(mock_memex: MockMemex, tmp_path: Path) -> None:
    """A bad MEMEX_PLUGIN_VERSION must not cause sourcing to fail when
    MEMEX_LOCAL_PATH is set — local paths don't have remote refs.

    Without this skip, a contributor with `MEMEX_PLUGIN_VERSION=branch-name`
    + `MEMEX_LOCAL_PATH=…` would hit an unnecessary ls-remote on every hook.
    """
    workspace = tmp_path / 'fake_workspace'
    workspace.mkdir()
    env = {
        **mock_memex.env,
        'MEMEX_LOCAL_PATH': str(workspace),
        # A ref that almost certainly does not exist on the remote. With
        # the local-path branch active, this must be ignored entirely.
        'MEMEX_PLUGIN_VERSION': 'definitely-not-a-real-branch-xyz-9999',
        'MEMEX_RESOLVE_VERBOSE': '1',
    }
    result = subprocess.run(
        ['bash', '-c', f'source "{PLUGIN_ROOT}/scripts/resolve_config.sh" && echo OK'],
        env=env,
        capture_output=True,
        text=True,
        timeout=15.0,
    )
    assert result.returncode == 0, result.stderr
    assert 'OK' in result.stdout, f'Sourcing failed: stdout={result.stdout!r}'
    # No "does not exist" diagnostic should have been emitted.
    assert 'does not exist as a tag or branch' not in result.stdout


def test_local_path_missing_directory_emits_diagnostic(
    mock_memex: MockMemex, tmp_path: Path
) -> None:
    """MEMEX_LOCAL_PATH pointing at a non-existent directory must fail loudly
    (with verbose) rather than silently falling through to git."""
    env = {
        **mock_memex.env,
        'MEMEX_LOCAL_PATH': str(tmp_path / 'does_not_exist'),
        'MEMEX_RESOLVE_VERBOSE': '1',
    }
    result = subprocess.run(
        ['bash', '-c', f'source "{PLUGIN_ROOT}/scripts/resolve_config.sh"'],
        env=env,
        capture_output=True,
        text=True,
        timeout=15.0,
    )
    assert result.returncode == 0  # sourcing returns cleanly with a stub memex()
    assert 'MEMEX_LOCAL_PATH' in result.stdout
    assert 'not a directory' in result.stdout


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
