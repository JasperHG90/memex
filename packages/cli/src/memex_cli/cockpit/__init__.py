"""Textual TUI cockpit for the maintenance ledger.

`ProposalCockpitApp` is the main entry point — instantiate it with a
`MemexClient`-shaped object, an optional vault filter, and a runtime
configuration, then call `await app.run_async()`. See
`memex_cli.lint:lint_review` for the wiring into the `memex lint review`
subcommand.
"""

from __future__ import annotations

from memex_cli.cockpit.app import ProposalCockpitApp
from memex_cli.cockpit.controller import CockpitController, CockpitProposal

__all__ = ['CockpitController', 'CockpitProposal', 'ProposalCockpitApp']
