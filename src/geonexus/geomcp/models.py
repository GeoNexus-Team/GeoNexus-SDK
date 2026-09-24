"""GeoMCP protocol data models.

GeoMCP is the GeoNexus geospatial interaction protocol. For the MVP it is a
lightweight JSON-RPC 2.0 protocol with geospatial context. These Pydantic
models describe the request/response envelope and the geospatial context
parameters.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SpatialContext(BaseModel):
    """Geospatial context attached to a GeoMCP request."""

    bbox: list[float] = Field(
        description="Bounding box [west, south, east, north] in the given CRS."
    )
    crs: str = Field(default="EPSG:4326", description="CRS of the bbox.")
    resolution: float | None = Field(default=None, description="Requested resolution in metres.")


class TemporalContext(BaseModel):
    """Temporal context attached to a GeoMCP request."""

    start: str | None = Field(default=None, description="ISO 8601 start.")
    end: str | None = Field(default=None, description="ISO 8601 end.")
    interval: str | None = Field(default=None, description="e.g. 'P16D'.")


class ExecuteParams(BaseModel):
    """Parameters of ``geo.execute``."""

    skill: str = Field(description="Name of the skill to execute.")
    geocards: list[str] = Field(
        default_factory=list,
        description="GeoCard ids the skill should operate on (asset references).",
    )
    spatial: SpatialContext | None = None
    temporal: TemporalContext | None = None
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Skill-specific execution parameters.",
    )
    request_id: str | None = Field(
        default=None, description="Optional caller-provided correlation id."
    )

    # ── GeoMCP V2 增强字段 ──
    contract_binding: str | None = Field(
        default=None,
        description="V2: 绑定的 GAAG 合约 ID，执行前通过 ContractGate 双验证",
    )
    pushdown_spec: dict[str, Any] | None = Field(
        default=None,
        description="V2: 计算下推规格 {'strategy':'local'|'migrate'|'auto'}",
    )
    federated_sources: list[str] | None = Field(
        default=None,
        description="V2: 联邦数据源引用列表（跨注册中心 URI）",
    )


class InvokeParams(BaseModel):
    """Parameters of ``geo.invoke`` —— 调用一个 ``type: model`` 的 GeoCard."""

    model: str = Field(description="model GeoCard id to invoke.")
    inputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Inputs named after the card's declared inputs.",
    )
    params: dict[str, Any] = Field(
        default_factory=dict, description="Model-specific invocation parameters."
    )
    timeout: float | None = Field(
        default=None, description="Override the card/task timeout in seconds."
    )


class PlanParams(BaseModel):
    """Parameters of ``geo.plan`` (V1.1).

    物化**不执行**：它把"配方 + 参数"变成一张任务图，返回每个步骤要跑什么、
    依赖谁、交付什么。平台据此在用户点"运行"之前就给出参数校验与影响范围，
    因此这是一个**只读**方法，不需要 API key。
    """

    recipe: str = Field(
        description="Recipe address, e.g. 'recipe://geonexus/sdg-15-3-1@1.0.0'. "
        "A version may be omitted, in which case the newest approved version is used."
    )
    params: dict[str, Any] = Field(
        default_factory=dict, description="Parameter values, validated against the recipe contract."
    )
    run_id: str = Field(
        default="run", description="Run identity; enters every task id, so replays are idempotent."
    )
    resolve: bool = Field(
        default=False,
        description="Resolve asset references through the node's cards (adds 'resolved' per task).",
    )


class DescribeParams(BaseModel):
    """Parameters of ``geo.describe``."""

    geocards: list[str] | None = Field(
        default=None, description="GeoCard ids to describe; None describes all."
    )
    skills: list[str] | None = Field(
        default=None, description="Skill names to describe; None describes all."
    )
    recipes: list[str] | None = Field(
        default=None,
        description="V1.1: recipe addresses to describe; None describes all registered recipes.",
    )


class GeoMCPRequest(BaseModel):
    """A JSON-RPC 2.0 request envelope."""

    jsonrpc: str = "2.0"
    id: Any | None = Field(default=None, description="Request id (echoed).")
    method: str
    params: dict[str, Any] | None = None


class GeoMCPError(BaseModel):
    """A JSON-RPC 2.0 error object."""

    code: int
    message: str
    data: Any | None = None


class GeoMCPResponse(BaseModel):
    """A JSON-RPC 2.0 response envelope."""

    jsonrpc: str = "2.0"
    id: Any | None = None
    result: Any | None = None
    error: GeoMCPError | None = None
