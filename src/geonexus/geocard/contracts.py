"""Transport independent, fail-closed contract reports for control planes.

GeoCard remains the static semantic source; observations and executions are
separate envelopes. No platform routes, persistence, or knowledge engine here.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .model import GeoCard
from .validator import ContractValidator


class ContractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bbox: list[float] = Field(min_length=4, max_length=4)
    crs: str = "EPSG:4326"
    start: str
    end: str
    required_bands: list[str] = Field(default_factory=list)
    required_units: dict[str, str] = Field(default_factory=dict)
    required_resolution: float | None = Field(default=None, gt=0)
    execution_jurisdiction: str = "local-node"
    export_data: bool = False

    @model_validator(mode="after")
    def ordered(self) -> ContractRequest:
        if not all(math.isfinite(value) for value in self.bbox):
            raise ValueError("bbox must contain only finite coordinates")
        if self.bbox[0] >= self.bbox[2] or self.bbox[1] >= self.bbox[3]:
            raise ValueError("bbox must have increasing west/east and south/north")
        start = datetime.fromisoformat(self.start.replace("Z", "+00:00"))
        end = datetime.fromisoformat(self.end.replace("Z", "+00:00"))
        if bool(start.tzinfo) != bool(end.tzinfo):
            raise ValueError("start and end must use the same timezone convention")
        if start > end:
            raise ValueError("start must not exceed end")
        return self


class ContractFinding(BaseModel):
    code: str
    status: Literal["pass", "fail", "unknown"]
    messages: list[str]


class ContractReport(BaseModel):
    card_id: str
    valid: bool
    findings: list[ContractFinding]
    method: str = "deterministic-v1"


def validate_contract(card: GeoCard, request: ContractRequest) -> ContractReport:
    """Use the canonical validator, refusing execution if metadata is unknown.

    Spatial/temporal checks mean overlap, not complete coverage. Units are
    exact declared units: this layer never silently converts pixels or units.
    """
    validator = ContractValidator()
    findings = []
    checks: dict[str, dict[str, Any]] = {
        "crs": {"crs": request.crs},
        "extent": {"bbox": request.bbox, "crs": request.crs},
        "time": {"start": request.start, "end": request.end},
        "bands": {"required_bands": request.required_bands},
    }
    if request.required_resolution is not None:
        checks["resolution"] = {"required_resolution": request.required_resolution}
    for code, args in checks.items():
        report = validator.check(card, **args)
        status: Literal["pass", "fail", "unknown"] = (
            "fail" if not report.satisfied else "unknown" if report.warnings else "pass"
        )
        findings.append(
            ContractFinding(code=code, status=status, messages=report.reasons + report.warnings)
        )
    bands = {b.name.lower(): b for b in card.bands}
    for name, units in request.required_units.items():
        actual = bands.get(name.lower())
        status = (
            "unknown"
            if not actual or not actual.units
            else "pass"
            if actual.units == units
            else "fail"
        )
        findings.append(
            ContractFinding(
                code=f"units:{name}",
                status=status,
                messages=[f"declared={actual.units if actual else None}; required={units}"],
            )
        )
    compliance = card.compliance
    policy: Literal["pass", "fail", "unknown"] = "unknown"
    message = "No sovereignty policy is declared"
    if compliance and compliance.sovereignty:
        policy = "pass"
        message = f"Execute in {request.execution_jurisdiction}; export={request.export_data}"
        if compliance.sovereignty != request.execution_jurisdiction:
            policy = "fail"
        if request.export_data and "no-export" in compliance.restrictions:
            policy = "fail"
        # Other restrictions need an external authorization decision, never guessed.
        if set(compliance.restrictions) - {"no-export"} or compliance.sensitivity not in (
            None,
            "public",
        ):
            policy = "unknown" if policy == "pass" else policy
    findings.append(ContractFinding(code="policy", status=policy, messages=[message]))
    return ContractReport(
        card_id=card.id, valid=all(f.status == "pass" for f in findings), findings=findings
    )


class RuntimeObservation(BaseModel):
    """Measured dynamic state; never persisted inside a static GeoCard."""

    model_config = ConfigDict(extra="forbid")
    node_id: str
    registered: bool
    reachable: bool | None = None
    executing: bool = False
    observed_at: datetime | None = None
    detail: str | None = None


class ResultArtifact(BaseModel):
    name: str
    href: str
    media_type: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ResultManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_id: str
    status: Literal["succeeded", "failed"]
    card_ids: list[str]
    node_id: str
    synthetic: bool
    created_at: datetime
    artifacts: list[ResultArtifact]
    provenance: dict
