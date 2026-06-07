"""`memex diagnostics` CLI subgroup.

Subcommands:
- manifold   — emit UMAP projection JSON (or 202 task info)
- retrieval  — emit top-N entities heatmap JSON
- summary    — emit full diagnostics summary JSON
- findings   — emit lint-finding pivot JSON
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console

from memex_cli.utils import (
    VaultOption,
    async_command,
    get_api_context,
    emit_json,
    handle_api_error,
    resolve_active_vault,
)
from memex_common.config import MemexConfig

console = Console()

app = typer.Typer(
    name='diagnostics',
    help='Diagnostics (UMAP manifold, retrieval heatmap, vault summary).',
    no_args_is_help=True,
)


@app.command('manifold')
@async_command
async def manifold_cmd(
    ctx: typer.Context,
    vault: VaultOption = None,
    force_refresh: Annotated[
        bool, typer.Option('--force-refresh', help='Force recompute, ignore cache.')
    ] = False,
):
    """Print the UMAP manifold JSON. 202 task info if cache is cold."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            vault_id = await resolve_active_vault(api, config, vault)
            status_code, payload = await api.get_diagnostics_manifold(
                vault_id, force_refresh=force_refresh
            )
        except Exception as e:
            handle_api_error(e)
            return
    payload['_http_status'] = status_code
    emit_json(payload)


@app.command('retrieval')
@async_command
async def retrieval_cmd(
    ctx: typer.Context,
    vault: VaultOption = None,
    top_n: Annotated[
        int, typer.Option('--limit', '-l', help='Maximum number of entities to return.')
    ] = 50,
):
    """Print the top-N entity outcome heatmap JSON."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            vault_id = await resolve_active_vault(api, config, vault)
            payload = await api.get_diagnostics_retrieval(vault_id, top_n=top_n)
        except Exception as e:
            handle_api_error(e)
            return
    emit_json(payload)


@app.command('summary')
@async_command
async def summary_cmd(
    ctx: typer.Context,
    vault: VaultOption = None,
):
    """Print the full diagnostics summary JSON (synchronous, no UMAP block)."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            vault_id = await resolve_active_vault(api, config, vault)
            payload = await api.get_diagnostics_summary(vault_id)
        except Exception as e:
            handle_api_error(e)
            return
    emit_json(payload)


@app.command('findings')
@async_command
async def findings_cmd(
    ctx: typer.Context,
    vault: VaultOption = None,
):
    """Print the lint-finding pivot JSON.

    Surfaces the (lint_type, status, source) pivot, the pending-by-type slice,
    and the top-5 most-recent pending findings for the vault. Distinct from
    `memex lint status` (single count) and `memex lint findings` (paginated
    rows) — this is the operator/observability dashboard view.
    """
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            vault_id = await resolve_active_vault(api, config, vault)
            payload = await api.get_diagnostics_lint(vault_id)
        except Exception as e:
            handle_api_error(e)
            return
    emit_json(payload)
