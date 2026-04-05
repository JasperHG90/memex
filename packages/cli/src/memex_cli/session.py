"""Session commands — briefing generation for LLM agent sessions."""

from typing import Annotated

import typer

from memex_common.config import MemexConfig
from memex_cli.utils import async_command, get_api_context, handle_api_error

app = typer.Typer(
    name='session',
    help='Session management commands.',
    no_args_is_help=True,
)


@app.command('briefing')
@async_command
async def briefing(
    ctx: typer.Context,
    vault: Annotated[
        str | None,
        typer.Option('--vault', '-v', help='Vault name or UUID. Defaults to active vault.'),
    ] = None,
    budget: Annotated[
        int,
        typer.Option('--budget', '-b', help='Token budget (1000 or 2000).'),
    ] = 2000,
    project_id: Annotated[
        str | None,
        typer.Option('--project-id', '-p', help='Project ID for KV namespace scoping.'),
    ] = None,
):
    """Generate a session briefing for LLM agents.

    Outputs raw markdown to stdout for consumption by hooks and scripts.
    """
    config: MemexConfig = ctx.obj

    async with get_api_context(config) as api:
        vault_name = vault or config.write_vault
        try:
            vault_uuid = await api.resolve_vault_identifier(vault_name)
        except Exception as e:
            handle_api_error(e)

        try:
            result = await api.get_session_briefing(
                vault_id=vault_uuid,
                budget=budget,
                project_id=project_id,
            )
        except Exception as e:
            handle_api_error(e)

    # Output raw markdown to stdout (no Rich formatting)
    print(result)
