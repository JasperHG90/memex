"""Lifespan for Memex FastMCP app"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, cast

import httpx
from fastmcp import Context, FastMCP
from mcp.shared.context import RequestContext

from memex_common.asset_cache import SessionAssetCache
from memex_common.client import RemoteMemexAPI
from memex_common.config import MemexConfig
from memex_mcp.models import AppContext

logger = logging.getLogger('memex.mcp')


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
    headers: dict[str, str] = {}
    if config.api_key:
        headers['X-API-Key'] = config.api_key.get_secret_value()
    client = httpx.AsyncClient(base_url=base_url, timeout=120.0, headers=headers)
    api = RemoteMemexAPI(client)

    cache = SessionAssetCache()

    app_context = AppContext(config=config)
    app_context._api = api
    app_context._asset_cache = cache

    try:
        vault = await api.get_active_vault()
        logger.info('Connected to Memex. Active vault: "%s" (id: %s)', vault.name, vault.id)
    except Exception as e:
        logger.warning('Could not verify active vault: %s. Server may not be running.', e)

    try:
        yield app_context
    finally:
        cache.cleanup()
        await client.aclose()


def get_api(ctx: Context) -> RemoteMemexAPI:
    """
    Get the RemoteMemexAPI instance from the context.
    """
    app_context: AppContext = cast(RequestContext, ctx.request_context).lifespan_context
    return cast(RemoteMemexAPI, app_context._api)


def get_asset_cache(ctx: Context) -> SessionAssetCache:
    """
    Get the SessionAssetCache instance from the context.
    """
    app_context: AppContext = cast(RequestContext, ctx.request_context).lifespan_context
    return cast(SessionAssetCache, app_context._asset_cache)


def get_config(ctx: Context) -> MemexConfig:
    app_context: AppContext = cast(RequestContext, ctx.request_context).lifespan_context
    return app_context.config
