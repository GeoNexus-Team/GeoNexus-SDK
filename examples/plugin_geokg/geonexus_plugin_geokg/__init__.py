"""GeoKG 市场插件：把领域知识图谱装成任意 GeoNode 的技能与知识卡。

分层纪律（见 GeoKG README）：本插件是**适配层**，内容与图引擎都在外部包——
    GeoKG（领域内容 / ETL / 管理面）  ──依赖──▶  geonexus-sdk（图引擎 / 协议）
本插件只做三件事：
  1. 把 GeoKG 的 ``admin.*`` 逻辑层暴露成 ``geo.execute`` 技能（CLI、HTTP、节点三方同源）；
  2. 建一张 ``type: knowledge`` 的 GeoCard，让知识库本身成为可发现的产品；
  3. GeoKG 缺失或数据不齐时给出**可执行的修复提示**，绝不假装有数据。

装法：``pip install geonexus-plugin-geokg``（内容层另装 ``pip install GeoKG``）。
"""

from __future__ import annotations

from typing import Any

from geonexus.geocard import GeoCardBuilder
from geonexus.geonode import Skill

#: 缺失依赖时给用户看的一句话（技能报错里原样带上）
DIST_HINT = ("pip install GeoKG（或把 GeoKG 装进节点环境；HTTP 管理面可用 geokg-api 起服务）")

CARD_ID = "geocard.geokg.knowledge-base"


def _admin():
    """惰性取 GeoKG 逻辑层：缺包/缺数据都要变成**可读的提示**，不是栈。"""
    try:
        from geokg import admin  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 - ImportError 与数据缺失都归这里
        raise RuntimeError(
            f"GeoKG 不可用（{exc.__class__.__name__}: {exc}）。装法：{DIST_HINT}"
        ) from exc
    return admin


def _as_list(value: Any) -> list[str] | None:
    if value in (None, "", []):
        return None
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v) for v in value]


def _limit(value: Any, default: int = 20) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, 200))


# --------------------------------------------------------------------------- #
# 技能：与 geokg CLI / geokg-api 共用同一批 admin.* 逻辑（三方不会给出不同答案）
# --------------------------------------------------------------------------- #
def geokg_search(params: dict[str, Any], context: Any) -> dict[str, Any]:
    """关键词检索实体（返回结果自带 provenance 溯源）。"""
    return _admin().search(
        str(params.get("query", "") or ""),
        types=_as_list(params.get("types")),
        limit=_limit(params.get("limit"), 20),
    )


def geokg_entity(params: dict[str, Any], context: Any) -> dict[str, Any]:
    """实体详情：属性、溯源与双向关系。查无此实体时 ``found: false``（是数据，不是崩溃）。"""
    entity_id = str(params.get("entity_id", "") or "").strip()
    if not entity_id:
        raise ValueError("entity_id 必填（先用 geokg-search 拿 id）")
    return _admin().entity(entity_id)


def geokg_subgraph(params: dict[str, Any], context: Any) -> dict[str, Any]:
    """以某实体为中心展开子图（图谱视图数据）。"""
    focus = str(params.get("focus", "") or "").strip()
    if not focus:
        raise ValueError("focus 必填（中心实体 id，先用 geokg-search 查）")
    try:
        depth = max(1, min(int(params.get("depth", 2)), 4))
    except (TypeError, ValueError):
        depth = 2
    direction = str(params.get("direction", "both") or "both")
    return _admin().subgraph(focus, depth=depth, direction=direction)


def geokg_types(params: dict[str, Any], context: Any) -> dict[str, Any]:
    """实体类型清单与计数（检索界面的图例）。"""
    return _admin().entity_types()


def geokg_counts(params: dict[str, Any], context: Any) -> dict[str, Any]:
    """计数口径与溯源审计（较重：会建图，首次调用慢，之后走 GeoKG 内部缓存）。"""
    return _admin().counts()


def geokg_status(params: dict[str, Any], context: Any) -> dict[str, Any]:
    """数据集状态与陈旧度告警（知识库能不能用，先问它）。"""
    return _admin().status()


def _card():
    """知识库自身的卡片：``type: knowledge``，非空间 → 走侧栏（不塞到地球上）。"""
    return (
        GeoCardBuilder(
            id=CARD_ID,
            type="knowledge",
            name="GeoKG 领域知识图谱",
            description=("SDG 框架 / 国家 / 卫星 / 仪器 / 行政区划 / 气候与灾害参考数据"
                         "构成的知识图谱；内容由 GeoKG 包提供，查询与 geokg CLI、"
                         "geokg-api 同源。"),
        )
        .tag("knowledge", "geokg", "reference-data")
        .provenance(provider="GeoKG", source="geokg（领域知识图谱内容包）")
        .license(name="Apache-2.0 + 各数据集原始许可（见 geokg-status / licenses）")
        .compliance(sensitivity="public")
        .build()
    )


class plugin:  # noqa: N801 - entry-point 指向的小写对象，保持导入路径稳定
    name = "geokg"
    version = "0.1.0"
    description = "GeoKG 领域知识图谱：检索/实体/子图/类型/计数/数据集状态 六技能 + 知识卡"

    @staticmethod
    def cards():
        return [_card()]

    @staticmethod
    def skills():
        return [
            Skill(
                name="geokg-search",
                description="按关键词检索 GeoKG 实体（带 provenance 溯源）",
                input_schema={"required": [], "properties": {
                    "query": {"type": "string", "description": "关键词；空串=只做类型过滤"},
                    "types": {"type": "array", "items": {"type": "string"},
                              "description": "实体类型过滤，可多个"},
                    "limit": {"type": "integer", "description": "最多返回条数（1..200，默认 20）"}}},
                handler=geokg_search,
            ),
            Skill(
                name="geokg-entity",
                description="实体详情：属性、溯源与双向关系",
                input_schema={"required": ["entity_id"], "properties": {
                    "entity_id": {"type": "string", "description": "实体 id（先用 geokg-search 查）"}}},
                handler=geokg_entity,
            ),
            Skill(
                name="geokg-subgraph",
                description="以某实体为中心的子图（图谱视图数据）",
                input_schema={"required": ["focus"], "properties": {
                    "focus": {"type": "string"},
                    "depth": {"type": "integer", "description": "展开层数 1..4（默认 2）"},
                    "direction": {"type": "string", "description": "both / out / in"}}},
                handler=geokg_subgraph,
            ),
            Skill(
                name="geokg-types",
                description="实体类型清单与计数",
                input_schema={"required": []},
                handler=geokg_types,
            ),
            Skill(
                name="geokg-counts",
                description="计数口径与溯源审计（首次较慢：会建图）",
                input_schema={"required": []},
                handler=geokg_counts,
            ),
            Skill(
                name="geokg-status",
                description="数据集状态、陈旧度告警与严格门禁",
                input_schema={"required": []},
                handler=geokg_status,
            ),
        ]
