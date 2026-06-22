"""Tests for MCP asset-path confinement (prompt-injection exfil guard).

``memex_add_assets`` / ``memex_add_note`` read agent-supplied local file paths.
The confinement helper restricts reads to the CWD plus ``MEMEX_MCP_ASSET_ROOTS``
so a prompt-injected agent cannot exfiltrate sensitive files (``~/.ssh/id_rsa``,
``/etc/passwd``, …) by ingesting them as note assets.
"""

import pathlib as plb

import pytest
from fastmcp.exceptions import ToolError

from memex_mcp.server import _allowed_asset_roots, _resolve_confined_asset_path


class TestAllowedRoots:
    def test_cwd_always_allowed(self, monkeypatch):
        monkeypatch.delenv('MEMEX_MCP_ASSET_ROOTS', raising=False)
        assert plb.Path.cwd().resolve() in _allowed_asset_roots()

    def test_env_var_adds_roots(self, monkeypatch, tmp_path):
        monkeypatch.setenv('MEMEX_MCP_ASSET_ROOTS', str(tmp_path))
        assert tmp_path.resolve() in _allowed_asset_roots()


class TestConfinement:
    def test_allows_path_under_env_root(self, monkeypatch, tmp_path):
        monkeypatch.setenv('MEMEX_MCP_ASSET_ROOTS', str(tmp_path))
        asset = tmp_path / 'asset.png'
        asset.write_bytes(b'x')
        assert _resolve_confined_asset_path(str(asset)) == asset.resolve()

    def test_rejects_path_outside_roots(self, monkeypatch, tmp_path):
        allowed = tmp_path / 'allowed'
        allowed.mkdir()
        monkeypatch.setenv('MEMEX_MCP_ASSET_ROOTS', str(allowed))
        outside = tmp_path / 'secret.txt'
        outside.write_text('secret')
        with pytest.raises(ToolError):
            _resolve_confined_asset_path(str(outside))

    def test_rejects_absolute_system_path(self, monkeypatch, tmp_path):
        monkeypatch.setenv('MEMEX_MCP_ASSET_ROOTS', str(tmp_path))
        with pytest.raises(ToolError):
            _resolve_confined_asset_path('/etc/passwd')

    def test_symlink_escape_is_rejected(self, monkeypatch, tmp_path):
        # A symlink that lives inside an allowed root but points outside it must
        # be rejected — resolve() follows the link before the containment check.
        allowed = tmp_path / 'allowed'
        allowed.mkdir()
        secret = tmp_path / 'secret.txt'
        secret.write_text('secret')
        link = allowed / 'link.txt'
        link.symlink_to(secret)
        monkeypatch.setenv('MEMEX_MCP_ASSET_ROOTS', str(allowed))
        with pytest.raises(ToolError):
            _resolve_confined_asset_path(str(link))
