"""``geo.plan`` 的协议层测试：加法式扩展、只读、参数错误可被机器读懂。

``geo.plan`` 是 V1.1 的加法式方法。这里同时验证"老客户端不受影响"这一条：
不实现它的节点返回 ``METHOD_NOT_FOUND``，不认识它的客户端只读
``geo.capabilities.methods``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from geonexus.geomcp import (
    METHOD_NOT_FOUND,
    RECIPE_NOT_FOUND,
    RECIPE_PARAM_INVALID,
    GeoMCPClient,
    GeoMCPClientError,
    GeoMCPServer,
    build_request,
)
from geonexus.recipe import RecipeRegistry, load_recipe

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sdg_15_3_1" / "recipe.yaml"
ADDRESS = "recipe://geonexus/sdg-15-3-1"


def _server(**kwargs) -> GeoMCPServer:
    registry = RecipeRegistry()
    registry.register(load_recipe(EXAMPLE), provider_url="http://node-a")
    return GeoMCPServer(name="plan-node", recipe_registry=registry, **kwargs)


def _call(server: GeoMCPServer, params: dict) -> dict:
    return server._dispatcher.dispatch(build_request("geo.plan", params, id="p1"))


# --------------------------------------------------------------------------- #
# 声明
# --------------------------------------------------------------------------- #
def test_geo_plan_is_advertised_additively() -> None:
    server = _server()
    caps = server.capabilities()
    assert "geo.plan" in caps["methods"]
    # 老客户端读的方法表仍然包含原有四个方法。
    for method in ("geo.capabilities", "geo.describe", "geo.execute", "geo.health"):
        assert method in caps["methods"]
    assert caps["recipe_planning"] == {
        "enabled": True,
        "method": "geo.plan",
        "read_only": True,
        "recipes": 1,
    }
    assert [r["id"] for r in caps["recipes"]] == ["recipe://geonexus/sdg-15-3-1@1.0.0"]


def test_geo_plan_declares_itself_unavailable_without_a_registry() -> None:
    bare = GeoMCPServer(name="bare")
    capability = bare.recipe_planning_capability()
    assert capability["enabled"] is False
    assert "recipe_registry" in capability["reason"]

    response = _call(bare, {"recipe": ADDRESS, "run_id": "r"})
    assert response["error"]["code"] == RECIPE_NOT_FOUND
    assert response["error"]["data"]["reason"] == "recipe_registry_not_configured"


def test_unknown_method_still_reports_method_not_found() -> None:
    """加法式扩展不能把"不认识的方法"变成别的东西。"""
    server = _server()
    response = server._dispatcher.dispatch(build_request("geo.decompose", {}, id="x"))
    assert response["error"]["code"] == METHOD_NOT_FOUND


# --------------------------------------------------------------------------- #
# 物化
# --------------------------------------------------------------------------- #
def test_geo_plan_returns_a_task_graph_without_executing() -> None:
    server = _server()
    response = _call(
        server,
        {"recipe": ADDRESS, "params": {"year": 2021, "region": "蒙古"}, "run_id": "run-7"},
    )
    result = response["result"]
    assert response.get("error") is None
    assert result["run_id"] == "run-7"
    assert result["recipe_id"] == "recipe://geonexus/sdg-15-3-1@1.0.0"
    assert [t["task_id"] for t in result["tasks"]] == ["run-7:s1", "run-7:s2", "run-7:s3"]
    s2 = result["tasks"][1]
    assert s2["step"] == "s2"
    assert s2["kind"] == "skill"
    assert s2["uses"] == "2026.9.16 SDG15.3.1"
    assert s2["depends_on"] == ["run-7:s1"]
    assert s2["params"]["year"] == 2021
    assert s2["inputs"]["mask"] == "step://s1/mask"
    assert result["reusable_params"] == ["year", "region", "report_format"]
    assert result["fixed_params"] == ["baseline"]
    assert set(result["deliverables"]) == {"degradation_map", "transition_table", "report"}
    # 全量配方也在响应里：平台展示"这一步到底跑什么"不需要第二次请求。
    assert result["recipe"]["id"] == result["recipe_id"]


def test_geo_plan_resolves_an_unversioned_address() -> None:
    server = _server()
    result = _call(server, {"recipe": ADDRESS, "params": {"year": 2020}, "run_id": "r"})["result"]
    assert result["recipe_id"].endswith("@1.0.0")


def test_geo_plan_reports_parameter_contract_failures_machine_readably() -> None:
    server = _server()

    bad_range = _call(server, {"recipe": ADDRESS, "params": {"year": 1999}, "run_id": "r"})
    assert bad_range["error"]["code"] == RECIPE_PARAM_INVALID
    assert bad_range["error"]["data"]["field"] == "year"
    assert bad_range["error"]["data"]["code"] == "param_out_of_range"

    missing = _call(server, {"recipe": ADDRESS, "params": {}, "run_id": "r"})
    assert missing["error"]["data"]["code"] == "param_missing"

    unknown = _call(server, {"recipe": ADDRESS, "params": {"year": 2019, "crs": "x"}, "run_id": "r"})
    assert unknown["error"]["data"] == {
        "field": "crs",
        "code": "param_unknown",
        "message": "recipe recipe://geonexus/sdg-15-3-1@1.0.0 has no parameter(s): crs",
    }

    fixed = _call(server, {"recipe": ADDRESS, "params": {"year": 2019, "baseline": 2010}, "run_id": "r"})
    assert fixed["error"]["data"]["code"] == "param_fixed"


def test_geo_plan_rejects_an_unknown_or_unapproved_recipe() -> None:
    server = _server()
    response = _call(server, {"recipe": "recipe://geonexus/nope", "params": {}, "run_id": "r"})
    assert response["error"]["code"] == RECIPE_NOT_FOUND
    assert response["error"]["data"]["recipe"] == "recipe://geonexus/nope"

    # 未批准的版本不参与解析，即使它是"最新"的。
    from geonexus.recipe import STATUS_PENDING, Recipe

    newer = load_recipe(EXAMPLE)
    data = newer.to_dict()
    data["id"] = "recipe://geonexus/sdg-15-3-1@2.0.0"
    server.recipe_registry.register(Recipe.from_dict(data), status=STATUS_PENDING)
    result = _call(server, {"recipe": ADDRESS, "params": {"year": 2020}, "run_id": "r"})["result"]
    assert result["recipe_id"].endswith("@1.0.0")


def test_geo_plan_resolve_maps_uses_to_asset_endpoints() -> None:
    """``resolve`` 把步骤声明的卡片 id 换成可取的地址；解析不到就原样保留。"""
    server = _server()
    plain = _call(server, {"recipe": ADDRESS, "params": {"year": 2019}, "run_id": "r"})
    assert plain["result"]["tasks"][0]["uses"] == "geocard.packed.menggu"

    resolved = _call(
        server, {"recipe": ADDRESS, "params": {"year": 2019}, "run_id": "r", "resolve": True}
    )
    # 这个节点没有注册这些卡片，所以没有任何东西被改写——而不是报错。
    assert resolved["result"]["tasks"][0]["uses"] == "geocard.packed.menggu"
    assert resolved["result"]["tasks"][0]["declared_uses"] == "geocard.packed.menggu"


def test_geo_describe_lists_recipes_and_can_select_them() -> None:
    server = _server()
    described = server.describe()
    assert [r["id"] for r in described["recipes"]] == ["recipe://geonexus/sdg-15-3-1@1.0.0"]
    # 摘要是摘要：给步骤的**数量**，不给每一步的定义。
    assert described["recipes"][0]["steps"] == 3
    assert "recipe" not in described["recipes"][0]
    assert described["recipes"][0]["reusable_params"] == ["year", "region", "report_format"]

    from geonexus.geomcp.models import DescribeParams

    picked = server.describe(DescribeParams(recipes=[ADDRESS]))
    assert len(picked["recipes"]) == 1
    with pytest.raises(Exception) as exc:
        server.describe(DescribeParams(recipes=["recipe://geonexus/nope"]))
    assert getattr(exc.value, "code", None) == RECIPE_NOT_FOUND


def test_geo_plan_is_read_only_and_needs_no_api_key() -> None:
    """物化不改状态，所以即使节点要求 API key，它也必须保持开放。"""
    from geonexus.geomcp.protocol import WRITE_METHODS

    assert "geo.plan" not in WRITE_METHODS
    server = _server(api_keys={"secret"})
    response = _call(server, {"recipe": ADDRESS, "params": {"year": 2019}, "run_id": "r"})
    assert response.get("error") is None


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
def test_client_plan_round_trip_over_http(tmp_path) -> None:
    from geonexus.geonode import GeoNode
    from geonexus.recipe import RecipeRegistry
    from geonexus.recipe import load_recipe as _load

    registry = RecipeRegistry()
    registry.register(_load(EXAMPLE))
    node = GeoNode(name="recipe-node", port=0, workdir=str(tmp_path))
    node.server.recipe_registry = registry
    running = node.start_in_thread(port=0)
    running.wait_until_ready()
    try:
        with GeoMCPClient(f"http://127.0.0.1:{running.port}", timeout=10) as client:
            caps = client.capabilities()
            assert "geo.plan" in caps["methods"]
            assert caps["recipes"][0]["id"] == "recipe://geonexus/sdg-15-3-1@1.0.0"

            plan = client.plan(ADDRESS, {"year": 2019, "region": "墨西哥"}, run_id="http-1")
            assert [t["task_id"] for t in plan["tasks"]] == ["http-1:s1", "http-1:s2", "http-1:s3"]
            assert plan["params"]["region"] == "墨西哥"

            described = client.describe(recipes=[ADDRESS])
            assert described["recipes"][0]["id"].startswith(ADDRESS)

            with pytest.raises(GeoMCPClientError) as exc:
                client.plan(ADDRESS, {"year": 1800}, run_id="http-2")
            assert exc.value.code == RECIPE_PARAM_INVALID
    finally:
        running.stop()
