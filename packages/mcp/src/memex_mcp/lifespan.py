"""Lifespan for Memex FastMCP app"""

from contextlib import asynccontextmanager
from typing import AsyncIterator, cast

import httpx
from fastmcp import Context, FastMCP
from mcp.shared.context import RequestContext

from memex_common.client import RemoteMemexAPI
from memex_common.config import MemexConfig
from memex_mcp.models import AppContext


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """
    Lifespan context manager for the memex MCP server.
    Initializes a remote API client based on configuration.
    """
    config = MemexConfig()

    # In Client mode, we just use the remote server URL from config
    server_url = config.server_url
    base_url = f'{server_url.rstrip("/")}/api/v1/'
    client = httpx.AsyncClient(base_url=base_url, timeout=30.0)
    api = RemoteMemexAPI(client)

    app_context = AppContext(config=config)
    app_context._api = api

    try:
        yield app_context
    finally:
        await client.aclose()


def get_api(ctx: Context) -> RemoteMemexAPI:
    """
    Get the RemoteMemexAPI instance from the context.
    """
    app_context: AppContext = cast(RequestContext, ctx.request_context).lifespan_context
    return app_context._api


def get_config(ctx: Context) -> MemexConfig:
    app_context: AppContext = cast(RequestContext, ctx.request_context).lifespan_context
    return app_context.config
