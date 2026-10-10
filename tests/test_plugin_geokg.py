"""GeoKG 市场插件测试：真数据检索、卡片合法、缺内容层时降级说人话。

插件是适配层，测试要钉住的是**适配契约**：
  · 六个技能与 CLI/HTTP 同源（都走 geokg.admin.*），输出带 provenance；
  · 知识卡是合法 GeoCard（type=knowledge，非空间 → 侧栏）；
  · 缺 GeoKG 时技能报可执行的安装提示，而不是栈或假数据。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys   # 仅用于 monkeypatch sys.modules 模拟缺依赖（**不**用来改 sys.path）

import pytest

from geonexus.geocard import GeoCard
from geonexus.geonode import GeoNode, load_plugins


def _load_plugin_module():
    """按文件路径加载示例插件。

    **不要**改成 ``sys.path.insert('examples/plugin_geokg')`` —— 一旦源码目录进了
    sys.path，``importlib.metadata`` 就会把该目录下残留的 ``*.egg-info``（就地构建
    wheel 的产物）当成"已安装的 entry point"，于是整个 pytest 会话里每个 GeoNode
    都会自动加载这个插件，别处的计数断言集体错位（血泪教训，别重演）。
    """
    path = pathlib.Path(__file__).resolve().parent.parent / "examples/plugin_geokg/geonexus_plugin_geokg/__init__.py"
    spec = importlib.util.spec_from_file_location("geonexus_plugin_geokg_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_p = _load_plugin_module()
CARD_ID, DIST_HINT, plugin = _p.CARD_ID, _p.DIST_HINT, _p.plugin


def _skill(name):
    return next(s for s in plugin.skills() if s.name == name)


def test_plugin_metadata_and_card_are_valid():
    assert plugin.name == "geokg"
    assert plugin.version == "0.1.0"
    names = [s.name for s in plugin.skills()]
    assert names == ["geokg-search", "geokg-entity", "geokg-subgraph",
                     "geokg-types", "geokg-counts", "geokg-status"]
    cards = plugin.cards()
    assert [c.id for c in cards] == [CARD_ID]
    card = cards[0]
    assert card.type == "knowledge", "知识库是 knowledge 类型的产品"
    assert card.spatial is None, "知识库没有空间范围：不许编一个塞到地球上"
    GeoCard.model_validate(card.to_dict()).validate()   # schema 真校验


def test_loads_onto_a_node_with_skills_and_card(tmp_path):
    class _Ep:
        name = "geokg"

        @staticmethod
        def load():
            return plugin

    node = GeoNode(name="kg-node", port=0, workdir=str(tmp_path))
    status = load_plugins(node, entry_points=[_Ep()])
    assert status == [{"name": "geokg", "version": "", "ok": True}] or status[0]["ok"] is True
    caps = node.server.capabilities()
    assert "geokg-search" in [s["name"] for s in caps["skills"]]
    assert CARD_ID in caps["geocards"]


def test_search_returns_real_results_with_provenance():
    out = _skill("geokg-search").handler({"query": "SDG", "limit": 3}, None)
    assert out["count"] >= 1 and out["returned"] >= 1
    first = out["results"][0]
    assert first["id"] and first["type"]
    assert first["provenance"]["origin"], "检索结果必须带溯源，否则不可审计"
    assert out["dataset"]["fingerprint"], "结果要能对上数据集指纹"


def test_search_type_filter_and_limit_are_applied():
    out = _skill("geokg-search").handler({"query": "", "types": "ConceptCategory", "limit": 2}, None)
    assert out["returned"] <= 2
    assert all(r["type"] == "ConceptCategory" for r in out["results"])


def test_types_and_status_are_available():
    types = _skill("geokg-types").handler({}, None)
    assert types["types"] and types["count"] >= 1
    status = _skill("geokg-status").handler({}, None)
    assert status, "数据集状态不能为空（知识库能不能用先问它）"


def test_entity_missing_is_data_not_crash():
    out = _skill("geokg-entity").handler({"entity_id": "no.such.entity"}, None)
    assert out["found"] is False, "查无此实体是数据事实，不该当异常抛"


def test_entity_requires_id():
    with pytest.raises(ValueError, match="entity_id 必填"):
        _skill("geokg-entity").handler({}, None)
    with pytest.raises(ValueError, match="focus 必填"):
        _skill("geokg-subgraph").handler({}, None)


def test_missing_geokg_degrades_with_actionable_hint(monkeypatch):
    """内容层缺失：技能报"装什么、怎么装"，而不是栈或假结果。"""
    monkeypatch.setitem(sys.modules, "geokg", None)
    with pytest.raises(RuntimeError) as exc:
        _skill("geokg-search").handler({"query": "SDG"}, None)
    msg = str(exc.value)
    assert "GeoKG 不可用" in msg
    assert "pip install GeoKG" in msg
    assert DIST_HINT.split("（")[0] in msg
