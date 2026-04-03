"Progressive disclosure transform for the Memex MCP server."

from collections.abc import Sequence

from fastmcp.experimental.transforms.code_mode import (
    DiscoveryToolFactory,
    GetSchemas,
    GetTags,
    Search,
)
from fastmcp.server.transforms import GetToolNext
from fastmcp.server.transforms.catalog import CatalogTransform
from fastmcp.tools.tool import Tool
from fastmcp.utilities.versions import VersionSpec


class DiscoveryMode(CatalogTransform):
    """Progressive disclosure: meta-tools for discovery, real tools still directly callable.

    Replaces ``tools/list`` with three discovery meta-tools (tags, search, get_schema)
    while keeping all real tools callable via ``tools/call``.  This lets LLMs discover
    tools incrementally instead of loading all schemas upfront.
    """

    def __init__(
        self,
        *,
        discovery_tools: list[DiscoveryToolFactory] | None = None,
    ) -> None:
        super().__init__()
        self._discovery_factories = discovery_tools or [
            GetTags(name='memex_tags'),
            Search(name='memex_search', default_detail='brief', default_limit=10),
            GetSchemas(name='memex_get_schema', default_detail='detailed'),
        ]
        self._built: list[Tool] | None = None

    def _build(self) -> list[Tool]:
        if self._built is None:
            self._built = [f(self.get_tool_catalog) for f in self._discovery_factories]
        return self._built

    async def transform_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        """Return only discovery meta-tools for tools/list."""
        return self._build()

    async def get_tool(
        self,
        name: str,
        call_next: GetToolNext,
        *,
        version: VersionSpec | None = None,
    ) -> Tool | None:
        """Check discovery tools first, then fall through to real tools."""
        for tool in self._build():
            if tool.name == name:
                return tool
        return await call_next(name, version=version)
