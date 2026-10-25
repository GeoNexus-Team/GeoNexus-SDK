"""Official MCP SDK adapter (V0.5).

Exposes a GeoMCP server's ``geo.*`` methods as **Model Context Protocol**
tools using the official ``mcp`` Python SDK. An MCP client (Claude Desktop,
any MCP host, or the stdio smoke test) can then drive GeoNexus through the
standard MCP interface.

Transport for the MVP: **stdio** (``geonexus mcp run``).
"""

from __future__ import annotations

import logging
from typing import Any

from . import __version__
from .geomcp.models import (
    DescribeParams,
    ExecuteParams,
    PlanParams,
    SpatialContext,
    TemporalContext,
)
from .geomcp.server import GeoMCPServer

logger = logging.getLogger(__name__)

# Protocol versions the official SDK understands; advertise a recent one.
DEFAULT_MCP_PROTOCOL_VERSION = "2025-06-18"


class MCPToolResult:
    """Result of an in-process ``call_tool`` (mirrors an MCP CallToolResult)."""

    def __init__(self, content: Any) -> None:
        self.content = content

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"MCPToolResult({self.content!r})"


class GeoMCPBridge:
    """In-process bridge between a GeoMCP server and a GeoMCP-style caller.

    Two callers share one object:

    * the official MCP host (stdio / Streamable HTTP) — everything it needs is
      delegated to the wrapped MCP server;
    * trusted in-process services — ``await call_tool(...)`` dispatches straight
      to the GeoMCP server without a transport round-trip.

    ``http_guarded`` keeps the HTTP transport closed unless the caller presents
    an ``X-API-Key`` header; the key itself is validated by the node in front.
    """

    TOOLS = {"geo_capabilities", "geo_describe", "geo_execute", "geo_plan", "geo_health"}

    def __init__(self, server: GeoMCPServer, mcp_server: Any = None, http_guarded: bool = False) -> None:
        self.server = server
        self.mcp_server = mcp_server
        self.http_guarded = http_guarded

    def __getattr__(self, name: str) -> Any:
        if self.mcp_server is None:
            raise AttributeError(name)
        return getattr(self.mcp_server, name)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> MCPToolResult:
        arguments = arguments or {}
        if name == "geo_capabilities":
            return MCPToolResult(self.server.capabilities())
        if name == "geo_describe":
            return MCPToolResult(self.server.describe(DescribeParams(**arguments)))
        if name == "geo_execute":
            return MCPToolResult(self.server.execute(ExecuteParams(**arguments)))
        if name == "geo_plan":
            return MCPToolResult(self.server.plan(PlanParams(**arguments)))
        if name == "geo_health":
            return MCPToolResult(self.server.health())
        raise ValueError(f"unknown GeoMCP tool: {name}")

    def streamable_http_app(self) -> Any:
        if self.mcp_server is None:  # pragma: no cover - env dependent
            raise ImportError("The MCP SDK is required for the HTTP transport.")
        app = self.mcp_server.streamable_http_app()
        if not self.http_guarded:
            return app

        async def guarded(scope, receive, send):  # pragma: no cover - needs ASGI loop
            if scope.get("type") == "http":
                headers = dict(scope.get("headers") or [])
                if not headers.get(b"x-api-key"):
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 401,
                            "headers": [(b"content-type", b"application/json")],
                        }
                    )
                    await send({"type": "http.response.body", "body": b'{"error":"API key required"}'})
                    return
            await app(scope, receive, send)

        return guarded


def create_mcp_server(server: GeoMCPServer, http_guarded: bool = False) -> Any:
    """Build the GeoMCP bridge (MCP tools + in-process ``call_tool``).

    When the official MCP SDK is installed the returned object also exposes the
    MCP server itself (``.name``, ``.list_tools()``, ``.run_stdio_async()``,
    ``.streamable_http_app()``), so existing stdio/HTTP entry points are
    unchanged. Without the SDK only ``call_tool`` is available.
    """
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError:  # pragma: no cover - env dependent
        logger.warning("MCP SDK not installed; only the in-process bridge is available.")
        return GeoMCPBridge(server, None, http_guarded=http_guarded)

    mcp = MCPServer(
        name="geonexus",
        title=f"GeoNexus GeoMCP bridge ({server.name})",
        version=__version__,
        instructions=(
            "GeoNexus geospatial interaction protocol over MCP. "
            "Use geo_plan to see what a reusable recipe would do (and which of its "
            "parameters you may change) before spending compute, then geo_execute "
            "to run a skill on the node that owns the data."
        ),
    )

    @mcp.tool(description="List the GeoMCP protocol surface (methods, skills, tools, geocards).")
    async def geo_capabilities() -> dict[str, Any]:
        return server.capabilities()

    @mcp.tool(
        description="Describe registered GeoCards and skills. "
        "Pass geocards and/or skills as lists of ids (None = all)."
    )
    async def geo_describe(
        geocards: list[str] | None = None,
        skills: list[str] | None = None,
        recipes: list[str] | None = None,
    ) -> dict[str, Any]:
        return server.describe(
            DescribeParams(geocards=geocards, skills=skills, recipes=recipes)
        )

    @mcp.tool(
        description=(
            "Execute a GeoSkill on the node that owns the data. "
            "skill: name; geocards: card ids the request references; "
            "spatial: {bbox: [w,s,e,n], crs}; temporal: {start, end}; "
            "params: skill-specific arguments (e.g. red/nir raster paths)."
        )
    )
    async def geo_execute(
        skill: str,
        geocards: list[str] | None = None,
        spatial: dict[str, Any] | None = None,
        temporal: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return server.execute(
            ExecuteParams(
                skill=skill,
                geocards=geocards or [],
                spatial=SpatialContext(**spatial) if spatial else None,
                temporal=TemporalContext(**temporal) if temporal else None,
                params=params or {},
                request_id=request_id,
            )
        )

    @mcp.tool(
        description=(
            "Materialise a reusable recipe into a run WITHOUT executing it: returns "
            "the tasks (step, kind, uses, depends_on, resolved params) and the "
            "deliverables, plus the recipe's parameter contract. recipe: address "
            "such as 'recipe://ns/name@1.0.0' (version optional); params: values to "
            "validate; run_id: identity for this run. Read-only."
        )
    )
    async def geo_plan(
        recipe: str,
        params: dict[str, Any] | None = None,
        run_id: str = "run",
        resolve: bool = False,
    ) -> dict[str, Any]:
        return server.plan(
            PlanParams(recipe=recipe, params=params or {}, run_id=run_id, resolve=resolve)
        )

    @mcp.tool(description="GeoMCP health check (node status, version, time).")
    async def geo_health() -> dict[str, Any]:
        return server.health()

    return GeoMCPBridge(server, mcp, http_guarded=http_guarded)


def run_stdio(server: GeoMCPServer) -> None:
    """Run the MCP adapter over stdio (blocking)."""
    import asyncio

    mcp = create_mcp_server(server)
    logger.info("Starting MCP stdio bridge for GeoMCP server '%s'", server.name)
    asyncio.run(mcp.run_stdio_async())


def create_streamable_http_app(server: GeoMCPServer) -> Any:
    """Build the MCP Streamable HTTP app (for serving under uvicorn)."""
    mcp = create_mcp_server(server)
    return mcp.streamable_http_app()


def run_http(
    server: GeoMCPServer,
    host: str = "127.0.0.1",
    port: int = 9000,
    path: str = "/mcp",
) -> None:
    """Run the MCP adapter over Streamable HTTP (blocking)."""
    import asyncio

    mcp = create_mcp_server(server)
    logger.info(
        "Starting MCP Streamable HTTP bridge for GeoMCP server '%s' on http://%s:%s%s",
        server.name,
        host,
        port,
        path,
    )
    asyncio.run(mcp.run_streamable_http_async(host=host, port=port, streamable_http_path=path))
