"""GeoMCPBridge.call_tool 的单元测试（不依赖官方 mcp 包）。"""

from __future__ import annotations

import asyncio

import pytest

from geonexus.geomcp import GeoMCPServer
from geonexus.mcp_adapter import GeoMCPBridge, create_mcp_server


@pytest.fixture()
def server():
    return GeoMCPServer(name="unit-test")


def test_bridge_dispatches_capabilities_and_health(server):
    bridge = create_mcp_server(server, http_guarded=True)
    assert isinstance(bridge, GeoMCPBridge)
    assert bridge.http_guarded is True
    capabilities = asyncio.run(bridge.call_tool("geo_capabilities", {}))
    assert isinstance(capabilities.content, dict) and "protocol" in capabilities.content
    health = asyncio.run(bridge.call_tool("geo_health", {}))
    assert health.content["status"] in {"ok", "degraded"}


def test_bridge_rejects_unknown_tool(server):
    bridge = create_mcp_server(server)
    with pytest.raises(ValueError):
        asyncio.run(bridge.call_tool("geo_nope", {}))
