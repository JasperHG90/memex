"""F32 — `memex diagnostics` CLI subgroup.

Subcommands:
- manifold   — emit UMAP projection JSON (or 202 task info)
- retrieval  — emit top-N entities heatmap JSON
- summary    — emit full diagnostics summary JSON

NOTE: there is intentionally NO `lint` subcommand in this module — that ships
in #26 (last in sub-wave C, after F6's MaintenanceProposal table is merged).
"""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console

from memex_cli.utils import async_command, get_api_context, handle_api_error
from memex_common.config import MemexConfig

console = Console()

app = typer.Typer(
    name='diagnostics',
    help='F32 — Diagnostics (UMAP manifold, retrieval heatmap, vault summary).',
    no_args_is_help=True,
)


@app.command('manifold')
@async_command
async def manifold_cmd(
    ctx: typer.Context,
    vault: Annotated[str, typer.Option('--vault', help='Vault name or ID.')],
    force_refresh: Annotated[
        bool, typer.Option('--force-refresh', help='Force recompute, ignore cache.')
    ] = False,
):
    """Print the UMAP manifold JSON. 202 task info if cache is cold."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            vault_id = await api.resolve_vault_identifier(vault)
            status_code, payload = await api.get_diagnostics_manifold(
                vault_id, force_refresh=force_refresh
            )
        except Exception as e:
            handle_api_error(e)
            return
    payload['_http_status'] = status_code
    console.print_json(json.dumps(payload, default=str))


@app.command('retrieval')
@async_command
async def retrieval_cmd(
    ctx: typer.Context,
    vault: Annotated[str, typer.Option('--vault', help='Vault name or ID.')],
    top_n: Annotated[int, typer.Option('--top-n', help='Number of entities to return.')] = 50,
):
    """Print the top-N entity outcome heatmap JSON."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            vault_id = await api.resolve_vault_identifier(vault)
            payload = await api.get_diagnostics_retrieval(vault_id, top_n=top_n)
        except Exception as e:
            handle_api_error(e)
            return
    console.print_json(json.dumps(payload, default=str))


@app.command('summary')
@async_command
async def summary_cmd(
    ctx: typer.Context,
    vault: Annotated[str, typer.Option('--vault', help='Vault name or ID.')],
):
    """Print the full diagnostics summary JSON (synchronous, no UMAP block)."""
    config: MemexConfig = ctx.obj
    async with get_api_context(config) as api:
        try:
            vault_id = await api.resolve_vault_identifier(vault)
            payload = await api.get_diagnostics_summary(vault_id)
        except Exception as e:
            handle_api_error(e)
            return
    console.print_json(json.dumps(payload, default=str))
