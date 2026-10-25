"""Execution contracts for GeoCards (SDK 1.1.1 compatibility surface).

The platform's typed service asks two questions before it runs anything:

* does a card satisfy the caller's requested context (extent, CRS, time window,
  bands/units, export policy)?
* what did a run produce (``ResultManifest``)?

Both are pure data questions, so this module stays free of I/O and depends only
on the card model.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .model import GeoCard

EXPORT_BLOCKING_RESTRICTIONS = {"no-export", "no_export", "export-forbidden"}
SENSITIVE_LEVELS = {"sensitive", "secret", "restricted"}
EPSILON = 1e-9


class ContractRequest(BaseModel):
    """What the caller wants to do with a card."""

    model_config = ConfigDict(extra="allow")

    bbox: list[float] | None = Field(default=None, description="[west, south, east, north]")
    crs: str | None = Field(default="EPSG:4326")
    start: str | None = None
    end: str | None = None
    required_bands: list[str] = Field(default_factory=list)
    required_units: dict[str, str] = Field(default_factory=dict)
    resolution: float | None = Field(default=None, ge=0)
    export_data: bool = False


class ContractFinding(BaseModel):
    code: str
    message: str
    severity: str = "error"


class ContractReport(BaseModel):
    valid: bool
    findings: list[ContractFinding] = Field(default_factory=list)
    normalized_context: ContractRequest | None = None


class Artifact(BaseModel):
    name: str
    href: str
    media_type: str | None = None
    bytes: int | None = None


class ResultManifest(BaseModel):
    model_config = ConfigDict(extra="allow")

    execution_id: str
    status: str
    card_ids: list[str] = Field(default_factory=list)
    node_id: str | None = None
    synthetic: bool = False
    created_at: str | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


def _parse_moment(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())
    except ValueError:
        return None


def _normalize_crs(value: str | None) -> str:
    return (value or "").strip().upper().replace("WGS84", "EPSG:4326")


def _bbox_contains(outer: list[float], inner: list[float]) -> bool:
    west, south, east, north = (float(v) for v in outer)
    w, s, e, n = (float(v) for v in inner)
    return w >= west - EPSILON and s >= south - EPSILON and e <= east + EPSILON and n <= north + EPSILON


def validate_contract(card: GeoCard, request: ContractRequest) -> ContractReport:
    """Return a report; ``valid`` is false when any error finding exists."""
    findings: list[ContractFinding] = []

    def fail(code: str, message: str) -> None:
        findings.append(ContractFinding(code=code, message=message))

    def warn(code: str, message: str) -> None:
        findings.append(ContractFinding(code=code, message=message, severity="warning"))

    spatial = getattr(card, "spatial", None)
    temporal = getattr(card, "temporal", None)
    compliance = getattr(card, "compliance", None)

    if request.crs and spatial and spatial.crs:
        if _normalize_crs(request.crs) != _normalize_crs(spatial.crs):
            fail("crs_mismatch", f"请求 CRS {request.crs} 与卡片 CRS {spatial.crs} 不一致")

    if request.bbox:
        if not spatial or not spatial.bbox:
            fail("extent_unknown", "卡片未声明空间范围，无法满足范围请求")
        elif not _bbox_contains(spatial.bbox, request.bbox):
            fail("extent_out_of_bounds", f"请求范围 {request.bbox} 超出卡片范围 {spatial.bbox}")

    if request.start or request.end:
        card_start = _parse_moment(getattr(temporal, "start", None))
        card_end = _parse_moment(getattr(temporal, "end", None))
        need_start = _parse_moment(request.start)
        need_end = _parse_moment(request.end)
        if (request.start and not card_start) or (request.end and not card_end):
            fail("temporal_unknown", "卡片未声明时间范围，无法满足时间窗请求")
        else:
            if need_start and card_start and need_start < card_start:
                fail("temporal_out_of_bounds", f"请求起始 {request.start} 早于卡片起始 {temporal.start}")
            if need_end and card_end and need_end > card_end:
                fail("temporal_out_of_bounds", f"请求结束 {request.end} 晚于卡片结束 {temporal.end}")

    band_units = {band.name: band.units for band in getattr(card, "bands", [])}
    for name in request.required_bands:
        if name not in band_units:
            fail("missing_band", f"卡片缺少所需波段 {name}")
    for name, units in (request.required_units or {}).items():
        if name not in band_units:
            fail("missing_band", f"卡片缺少所需波段 {name}")
        elif band_units[name] and units and band_units[name] != units:
            fail("unit_mismatch", f"波段 {name} 单位为 {band_units[name]}，请求 {units}")

    restrictions = set(getattr(compliance, "restrictions", []) or [])
    sensitivity = getattr(compliance, "sensitivity", None)
    if request.export_data:
        if restrictions & EXPORT_BLOCKING_RESTRICTIONS:
            fail("raw_export_forbidden", "卡片声明禁止导出原始数据")
        elif sensitivity in SENSITIVE_LEVELS:
            fail("raw_export_forbidden", f"卡片敏感级别 {sensitivity} 不允许导出原始数据")

    if request.resolution and spatial and spatial.resolution:
        if request.resolution < spatial.resolution:
            warn("resolution_too_fine", f"请求分辨率 {request.resolution} m 细于卡片最佳 {spatial.resolution} m")

    return ContractReport(
        valid=not any(f.severity == "error" for f in findings),
        findings=findings,
        normalized_context=request,
    )
