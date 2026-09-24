"""Registry recipe surface (V1.1): HTTP + client round trip.

The registry is the **catalogue** for recipes just as it is for cards, with the
same review vocabulary. The interesting part is the address: a recipe id is
``recipe://ns/name@semver``, so it contains ``/``, ``:`` and ``@`` — these tests
exercise the real HTTP path handling rather than assuming it works.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
import uvicorn

from geonexus.recipe import Recipe, RecipeRegistry, load_recipe
from geonexus.registry import RegistryClient, RegistryClientError, RegistryServer

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sdg_15_3_1" / "recipe.yaml"
ADDRESS = "recipe://geonexus/sdg-15-3-1"


@pytest.fixture()
def registry(tmp_path):
    server = RegistryServer(name="test-registry", persist_path=str(tmp_path / "registry.json"))
    config = uvicorn.Config(server.create_app(), host="127.0.0.1", port=0, log_level="error")
    running = uvicorn.Server(config)
    threading.Thread(target=running.run, daemon=True).start()
    for _ in range(200):
        if running.started:
            break
        time.sleep(0.05)
    port = running.servers[0].sockets[0].getsockname()[1]
    client = RegistryClient(f"http://127.0.0.1:{port}", timeout=10)
    try:
        yield client, server, str(tmp_path / "registry.json")
    finally:
        client.close()
        running.should_exit = True


def test_register_defaults_to_pending(registry) -> None:
    client, _, _ = registry
    recipe = load_recipe(EXAMPLE)
    result = client.register_recipe(recipe.to_dict(), node_url="http://node-a")
    assert result["status"] == "registered"
    assert result["id"] == "recipe://geonexus/sdg-15-3-1@1.0.0"
    assert result["entry"]["status"] == "pending"
    # 待审的方案不出现在"已批准"列表里：可发现 ≠ 可用。
    assert client.list_recipes(approved_only=True) == []
    assert [r["id"] for r in client.list_recipes(status="pending")] == [result["id"]]


def test_register_accepts_a_dict_and_rejects_a_broken_one(registry) -> None:
    """回归：``register`` 曾经对传入的 dict 调 ``.validate()``，HTTP 路径直接 500。"""
    client, server, _ = registry
    recipe = load_recipe(EXAMPLE).to_dict()
    assert client.register_recipe(recipe)["id"] == recipe["id"]

    with pytest.raises(RegistryClientError) as bad:
        client.register_recipe({"id": "not-an-address"})
    assert bad.value.code == 422

    with pytest.raises(RegistryClientError) as conflict:
        client.register_recipe(recipe, status="approved")
    assert conflict.value.code == 409

    # replace=True 才允许覆盖，并留下审核历史。
    replaced = client.register_recipe(recipe, status="approved", replace=True)
    assert replaced["entry"]["status"] == "approved"
    assert [h["action"] for h in replaced["entry"]["review_history"]] == ["registered"]


def test_addresses_with_slashes_survive_the_round_trip(registry) -> None:
    """``recipe://`` 里的 ``/`` ``:`` ``@`` 必须原样到达服务端。"""
    client, _, _ = registry
    client.register_recipe(load_recipe(EXAMPLE).to_dict(), status="approved")

    entry = client.get_recipe("recipe://geonexus/sdg-15-3-1@1.0.0")
    assert entry["summary"]["id"] == "recipe://geonexus/sdg-15-3-1@1.0.0"
    # 不带版本 → 解析到最新的已批准版本，而不是 404。
    assert client.get_recipe(ADDRESS)["summary"]["id"] == "recipe://geonexus/sdg-15-3-1@1.0.0"

    with pytest.raises(RegistryClientError) as missing:
        client.get_recipe("recipe://geonexus/nope")
    assert missing.value.code == 404


def test_review_workflow_approve_and_reject(registry) -> None:
    client, server, _ = registry
    recipe = load_recipe(EXAMPLE).to_dict()
    client.register_recipe(recipe, submitted_by="alice")

    assert client.approve_recipe(ADDRESS, by="bob", note="口径清楚")["status"] == "approved"
    assert client.list_recipes(approved_only=True)[0]["id"] == f"{ADDRESS}@1.0.0"
    assert server.health()["recipes"] == 1

    client.reject_recipe(ADDRESS, by="carol", note="数据快照过期")
    assert client.list_recipes(approved_only=True) == []
    entry = client.get_recipe(f"{ADDRESS}@1.0.0")["entry"]
    assert [h["action"] for h in entry["review_history"]] == ["registered", "approved", "rejected"]
    assert entry["review_note"] == "数据快照过期"

    with pytest.raises(RegistryClientError) as missing:
        client.approve_recipe("recipe://geonexus/nope")
    assert missing.value.code == 404


def test_search_filters_by_contract(registry) -> None:
    client, _, _ = registry
    client.register_recipe(load_recipe(EXAMPLE).to_dict(), status="approved")

    assert client.list_recipes(param="year")[0]["reusable_params"] == [
        "year",
        "region",
        "report_format",
    ]
    assert client.list_recipes(param="crs") == []
    assert client.list_recipes(role="knowledge")
    assert client.list_recipes(role="model") == []
    assert client.list_recipes(query="土地退化")
    assert client.list_recipes(namespace="other") == []
    assert client.list_recipes(tag="sdg")


def test_fork_through_the_registry_leaves_the_source_untouched(registry) -> None:
    client, server, _ = registry
    client.register_recipe(load_recipe(EXAMPLE).to_dict(), status="approved")

    forked = client.fork_recipe(
        ADDRESS,
        name="sdg-15-3-1-mexico",
        title="SDG 15.3.1 墨西哥",
        params={"region": "墨西哥"},
        fixed={"year": 2019},
    )
    assert forked["id"] == "recipe://geonexus/sdg-15-3-1-mexico@1.0.0"
    assert forked["entry"]["status"] == "pending"
    assert forked["entry"]["review_note"] == "forked from recipe://geonexus/sdg-15-3-1@1.0.0"
    # 派生件的参数取值被类型收敛过（HTTP 传 JSON，整数仍是整数）。
    params = {p["name"]: p for p in forked["recipe"]["params"]}
    assert params["region"]["default"] == "墨西哥"
    assert params["year"]["scope"] == "fixed"
    assert params["year"]["default"] == 2019

    source = client.get_recipe(f"{ADDRESS}@1.0.0")["entry"]["recipe"]
    source_params = {p["name"]: p for p in source["params"]}
    assert source_params["region"]["default"] == "蒙古"
    assert source_params["year"]["scope"] == "reusable"

    with pytest.raises(RegistryClientError) as unknown:
        client.fork_recipe(ADDRESS, params={"crs": "EPSG:4326"})
    assert unknown.value.code == 422

    with pytest.raises(RegistryClientError) as missing:
        client.fork_recipe("recipe://geonexus/nope")
    assert missing.value.code == 404


def test_recipes_persist_across_a_restart(tmp_path) -> None:
    """卡片与配方分文件存：一个坏文件不该带走另一个目录。"""
    persist = tmp_path / "registry.json"
    server = RegistryServer(name="r", persist_path=str(persist))
    assert server.recipes.persist_path == str(persist) + ".recipes.json"

    server.register_recipe(load_recipe(EXAMPLE), status="approved", provider_url="http://node-a")
    server.approve_recipe(ADDRESS, by="bob")

    reloaded = RegistryServer(name="r", persist_path=str(persist))
    assert len(reloaded.recipes) == 1
    entry = reloaded.get_recipe(ADDRESS)["entry"]
    assert entry["status"] == "approved"
    assert entry["provider_url"] == "http://node-a"
    assert reloaded.health()["cards"] == 0  # 卡片目录没被配方写入污染


def test_recipe_endpoints_require_the_api_key_when_configured(tmp_path) -> None:
    server = RegistryServer(name="locked", api_keys={"secret"})
    from fastapi.testclient import TestClient

    with TestClient(server.create_app()) as client:
        recipe = load_recipe(EXAMPLE).to_dict()
        assert client.post("/recipes", json={"recipe": recipe}).status_code == 401
        assert (
            client.post(
                "/recipes", json={"recipe": recipe}, headers={"X-API-Key": "secret"}
            ).status_code
            == 201
        )
        # 读端保持开放：能不能用必须先能被发现。
        assert client.get("/recipes").status_code == 200
        assert client.get(f"/recipes/{ADDRESS}").status_code == 200


def test_recipe_registry_is_isolated_from_a_plain_card_registry() -> None:
    """没有持久化路径时两边都是内存态，互不影响。"""
    server = RegistryServer(name="mem")
    assert len(server.recipes) == 0
    assert server.recipes.persist_path is None
    server.register_recipe(Recipe.from_dict(load_recipe(EXAMPLE).to_dict()))
    assert len(server.recipes) == 1
    # 另一个实例看不到它 —— 没有隐式全局状态。
    assert len(RegistryServer(name="mem2").recipes) == 0


def test_standalone_recipe_registry_file_is_reusable() -> None:
    """同一份文件可以被 node 直接挂上，不需要经过 HTTP。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "recipes.json"
        registry = RecipeRegistry(path)
        registry.register(load_recipe(EXAMPLE), status="approved")
        node_side = RecipeRegistry(path)
        assert [e.recipe_id for e in node_side.search(approved_only=True)] == [
            "recipe://geonexus/sdg-15-3-1@1.0.0"
        ]


def test_ambiguous_approval_is_refused_rather_than_guessed(registry) -> None:
    """审批是治理动作：不带版本又匹配到多条时，报错而不是猜"最新那版"。"""
    client, _, _ = registry
    base = load_recipe(EXAMPLE).to_dict()
    for version in ("1.0.0", "1.1.0"):
        data = dict(base)
        data["id"] = f"recipe://geonexus/sdg-15-3-1@{version}"
        client.register_recipe(data)

    with pytest.raises(RegistryClientError) as ambiguous:
        client.approve_recipe(ADDRESS)
    assert ambiguous.value.code == 409
    assert "1.0.0, 1.1.0" in str(ambiguous.value)

    # 明确写出版本就正常。
    assert client.approve_recipe(f"{ADDRESS}@1.1.0")["id"] == f"{ADDRESS}@1.1.0"

    # 只有一个版本时，不带版本也能审批（人不会为了唯一一条记录去查版本号）。
    client.unregister_recipe(f"{ADDRESS}@1.0.0")
    assert client.approve_recipe(ADDRESS)["id"] == f"{ADDRESS}@1.1.0"
