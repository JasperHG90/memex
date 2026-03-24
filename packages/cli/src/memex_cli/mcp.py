"""
Memex MCP CLI commands.
"""

import asyncio
import typer

app = typer.Typer(name='mcp', help='Manage the Memex MCP server.')


@app.callback()
def main():
    """
    Manage the Memex MCP server.
    """


@app.command()
def run(
    transport: str = typer.Option(
        'stdio', '--transport', '-t', help='Transport mode: stdio, http, or sse'
    ),
    host: str = typer.Option('0.0.0.0', help='Host for network transports'),
    port: int = typer.Option(8000, help='Port for network transports'),
):
    """
    Run the Memex MCP server.
    """
    try:
        from memex_mcp.server import mcp
    except ImportError:
        raise ModuleNotFoundError(
            "'memex_mcp' is not installed. Please install 'memex_cli' with the 'mcp' extra"
        )
    if transport in ('http', 'sse'):
        asyncio.run(mcp.run_async(transport=transport, host=host, port=port))
    else:
        asyncio.run(mcp.run_async(transport='stdio'))
