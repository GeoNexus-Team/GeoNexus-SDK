import json

import pytest
from mcp import types

from geonexus.geocard import GeoCard
from geonexus.mcp_adapter import _ExecutionAuthApp
from geonexus.mcp_client import MCPToolClient, MCPToolExecutionError, _tool_result_to_dict
from geonexus.resource import ComputeCapability, match_resource


def test_official_result_retains_output_and_image_error():
    result = types.CallToolResult(
        content=[types.ImageContent(type="image", data="AA==", mimeType="image/png")],
        structured_content={"value": 7},
    )
    value = _tool_result_to_dict(result)
    assert value["structuredContent"] == {"value": 7}
    assert value["_mcp"]["content"][0]["type"] == "image"
    result.is_error = True
    with pytest.raises(MCPToolExecutionError) as error:
        _tool_result_to_dict(result)
    assert error.value.result["isError"] is True


@pytest.mark.asyncio
async def test_official_pagination_and_output_schema():
    calls = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def list_tools(self, *, params=None):
            calls.append(params.cursor if params else None)
            return types.ListToolsResult(
                tools=[
                    types.Tool(
                        name="b" if params else "a",
                        input_schema={"type": "object"},
                        output_schema={"type": "object"},
                    )
                ],
                next_cursor=None if params else "second",
            )

    client = object.__new__(MCPToolClient)
    client._session_factory = Session
    tools = await client._list_tools_async()
    assert calls == [None, "second"] and [tool.name for tool in tools] == ["a", "b"]
    assert tools[0].output_schema == {"type": "object"}


def test_strict_compute_matching_rejects_unknown_capacity():
    card = GeoCard.model_validate(
        {
            "geocard_version": "1.0",
            "id": "model",
            "type": "model",
            "name": "Model",
            "description": "Test",
            "runtime": {"cpu": 2, "memory": "1Gi"},
        }
    )
    capability = ComputeCapability(node_url="node", cpu=None, memory_gb=None)
    assert not match_resource(card, [capability], strict=True).matched


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["geo_execute", "geo_invoke"])
async def test_http_execution_credentials_are_checked_before_dispatch(name):
    called, sent = [], []

    async def downstream(scope, receive, send):
        called.append(await receive())

    wrapper = _ExecutionAuthApp(downstream, {"secret"})
    body = json.dumps({"method": "tools/call", "params": {"name": name}}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "headers": []}
    await wrapper(scope, receive, send)
    assert not called and sent[0]["status"] == 401
    scope["headers"] = [(b"x-api-key", b"secret")]
    await wrapper(scope, receive, send)
    assert called[0]["body"] == body


def test_model_invocation_rejects_unknown_compute_before_provider():
    from geonexus.model.invocation import invoke_model

    card = GeoCard.model_validate(
        {
            "geocard_version": "1.0",
            "id": "model",
            "type": "model",
            "name": "Model",
            "description": "Test",
            "runtime": {"cpu": 2, "memory": "1Gi"},
        }
    )
    result = invoke_model(card, {}, capabilities=[ComputeCapability(node_url="node")])
    assert not result.ok and result.error_code == "no_compatible_compute"
    assert result.provider is None
