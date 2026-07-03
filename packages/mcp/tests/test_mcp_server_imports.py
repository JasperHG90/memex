"""Backstop: the MCP server module imports cleanly after the FSRS-5 rip-out.

The CI guard `scripts/ci/check_no_fsrs_residue.sh` catches textual residue;
this test catches the case where a textually-clean import path still fails
because a transitive module was removed.
"""

from __future__ import annotations


def test_mcp_server_imports_clean() -> None:
    import memex_mcp.server  # noqa: F401
