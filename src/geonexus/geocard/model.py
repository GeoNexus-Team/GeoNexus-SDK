"""GeoCard data model.

GeoCard is the machine-readable identity, capability and contract description
for a geospatial asset. This module defines the typed Pydantic model of a
GeoCard together with its sections.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

logger = logging.getLogger(__name__)


def _strip_none_values(value: Any) -> Any:
    """Recursively remove None values from dicts and lists (for serialization)."""
    if isinstance(value, dict):
        return {k: _strip_none_values(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none_values(v) for v in value if v is not None]
    return value


class GeoCardError(Exception):
    """Base error for the GeoCard SDK."""


class GeoCardValidationError(GeoCardError):
    """Raised when a GeoCard does not satisfy the official JSON schema."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("GeoCard validation failed:\n- " + "\n- ".join(errors))


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
class SpatialSection(BaseModel):
    """Spatial extent and geometry characteristics."""

    model_config = ConfigDict(extra="forbid")

    bbox: list[float] | None = Field(
        default=None,
        description="Bounding box [west, south, east, north] (or 6 values for 3D).",
    )
    crs: str | None = Field(default=None, description="e.g. 'EPSG:4326'.")
    resolution: float | None = Field(default=None, ge=0, description="Best resolution in metres.")
    geometry: str | None = Field(default=None, description="Optional GeoJSON geometry string.")


class TemporalSection(BaseModel):
    """Temporal coverage of the asset."""

    model_config = ConfigDict(extra="forbid")

    start: str | None = Field(default=None, description="ISO 8601 start.")
    end: str | None = Field(default=None, description="ISO 8601 end.")
    interval: str | None = Field(default=None, description="e.g. 'P16D'.")


class Band(BaseModel):
    """A spectral or thematic band."""

    model_config = ConfigDict(extra="forbid")

    name: str
    dtype: str | None = Field(default=None, description="e.g. 'uint16'.")
    units: str | None = Field(default=None, description="e.g. 'dn', 'reflectance'.")
    description: str | None = None


class IOField(BaseModel):
    """An input or output declaration of a skill / model / workflow."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str = Field(description="e.g. 'raster', 'vector', 'number', 'string'.")
    description: str | None = None
    required: bool = False

    @field_serializer("required")
    def _serialize_required(self, value: bool) -> bool | None:
        """Omit ``required`` when False.

        The official GeoCard schema allows ``required`` on ``inputs`` items
        but not on ``outputs`` items; omitting the default keeps emitted
        cards schema-valid for both.
        """
        return value if value else None


class Capability(BaseModel):
    """A named capability provided by the asset."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None


class AccessSection(BaseModel):
    """How to reach the asset."""

    model_config = ConfigDict(extra="forbid")

    protocol: str | None = Field(default=None, description="e.g. 'geomcp', 'https'.")
    endpoint: str | None = Field(default=None, description="URL or endpoint id.")
    auth: str | None = Field(default=None, description="e.g. 'none', 'apikey'.")
    format: str | None = Field(default=None, description="e.g. 'GeoTIFF', 'COG'.")


class ProvenanceSection(BaseModel):
    """Where the asset came from."""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    source: str | None = None
    lineage: str | None = None


class LicenseField(BaseModel):
    """Structured license description."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    url: str | None = None


class ComplianceSection(BaseModel):
    """Sovereignty and compliance constraints."""

    model_config = ConfigDict(extra="forbid")

    sovereignty: str | None = Field(
        default=None, description="Governing jurisdiction, e.g. 'BR', 'local-node'."
    )
    restrictions: list[str] = Field(default_factory=list)
    sensitivity: str | None = Field(
        default=None,
        description="One of public, restricted, sensitive, secret.",
    )


class TrustSection(BaseModel):
    """Trust metadata. MVP: informational, not scientifically validated."""

    model_config = ConfigDict(extra="forbid")

    verified: bool = False
    score: float | None = Field(default=None, ge=0, le=1)


class RuntimeSection(BaseModel):
    """Runtime requirements for compute assets."""

    model_config = ConfigDict(extra="forbid")

    cpu: Any | None = Field(default=None, description="Cores (int) or '2'.")
    memory: str | None = Field(default=None, description="e.g. '4Gi'.")
    gpu: str | None = Field(default=None, description="e.g. 'none'.")


class InterfaceSection(BaseModel):
    """Interface versioning information."""

    model_config = ConfigDict(extra="forbid")

    type: str | None = Field(default=None, description="e.g. 'geomcp-skill'.")
    version: str | None = Field(default=None, description="e.g. '1.0'.")


class RenderingSection(BaseModel):
    """Visualisation hints."""

    model_config = ConfigDict(extra="forbid")

    min: float | None = None
    max: float | None = None
    colormap: str | None = None
    opacity: float | None = Field(default=None, ge=0, le=1)


#: Roles a component may take inside a composite asset. Deliberately the same
#: vocabulary as ``GeoCard.type`` so a case never invents a second one — a
#: report is a ``knowledge`` component whose own card declares
#: ``interface.type: report``.
COMPONENT_ROLES = (
    "data",
    "model",
    "skill",
    "agent",
    "workflow",
    "knowledge",
    "compute",
)

#: Parameter kinds understood by :class:`ParamSpec`.
PARAM_TYPES = (
    "string",
    "number",
    "integer",
    "boolean",
    "time",
    "bbox",
    "geometry",
    "enum",
    "ref",
)

#: ``scope`` values for :class:`ParamSpec`.
PARAM_SCOPES = ("reusable", "fixed")


class ComponentRef(BaseModel):
    """One component of a composite asset (``type: workflow``).

    A reference — never a copy. The referenced card keeps its own identity,
    access, licence and compliance, so reusing a case does not move data.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="GeoCard id of the referenced asset.")
    role: str = Field(
        default="data",
        description="One of: " + ", ".join(COMPONENT_ROLES) + ".",
    )
    step: str | None = Field(
        default=None,
        description="Step id in the recipe that consumes or produces this component.",
    )
    required: bool = Field(
        default=True,
        description="False when the composite still runs without this component.",
    )
    alias: str | None = Field(
        default=None,
        description="Local name used by the recipe, when it differs from the card id.",
    )


class CompositionSection(BaseModel):
    """What a composite asset is made of.

    Answers "which assets, in which role" without embedding any of them, so a
    reader can see the shape of a solution and a validator can check that every
    reference resolves.
    """

    model_config = ConfigDict(extra="forbid")

    components: list[ComponentRef] = Field(default_factory=list)
    template: str | None = Field(
        default=None,
        description="Optional document/report template id bound to this composition.",
    )

    @field_validator("components")
    @classmethod
    def _unique_components(cls, value: list[ComponentRef]) -> list[ComponentRef]:
        """Reject duplicate ``(id, role)`` pairs and empty ids."""
        seen: set[tuple[str, str]] = set()
        for item in value:
            if not item.id or not item.id.strip():
                raise ValueError("composition.components[].id must not be empty")
            key = (item.id, item.role)
            if key in seen:
                raise ValueError(f"duplicate component: {item.id} as {item.role}")
            seen.add(key)
        return value


class ParamSpec(BaseModel):
    """One parameter of a composite asset — the thing a reuser may change.

    Without this contract a forked case is inert: nobody knows which fields are
    safe to vary, within what range, or which must stay as the author fixed
    them.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Parameter name, as used by the recipe.")
    type: str = Field(default="string", description="One of: " + ", ".join(PARAM_TYPES) + ".")
    required: bool = False
    default: Any | None = None
    enum: list[Any] = Field(
        default_factory=list,
        description="Allowed values; implies type 'enum' when non-empty.",
    )
    minimum: float | None = None
    maximum: float | None = None
    scope: str = Field(
        default="reusable",
        description="reusable (a reuser may change it) or fixed (must not be changed).",
    )
    target: str | None = Field(
        default=None,
        description="Where it lands in the DAG, e.g. 'step1.params.year'.",
    )
    description: str | None = None

    @field_validator("enum")
    @classmethod
    def _enum_not_empty_strings(cls, value: list[Any]) -> list[Any]:
        """``enum`` entries must not be blank strings — usually a typo."""
        for item in value:
            if isinstance(item, str) and not item.strip():
                raise ValueError("params.enum must not contain empty strings")
        return value

    @model_validator(mode="after")
    def _coherent_range(self) -> ParamSpec:
        """``minimum`` must not exceed ``maximum``."""
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError(
                f"param '{self.name}': minimum {self.minimum} > maximum {self.maximum}"
            )
        return self


class ParamsSection(BaseModel):
    """The reusable parameter contract of a composite asset."""

    model_config = ConfigDict(extra="forbid")

    params: list[ParamSpec] = Field(default_factory=list)

    @field_validator("params")
    @classmethod
    def _unique_params(cls, value: list[ParamSpec]) -> list[ParamSpec]:
        """Parameter names are unique within a card."""
        seen: set[str] = set()
        for item in value:
            if not item.name or not item.name.strip():
                raise ValueError("params[].name must not be empty")
            if item.name in seen:
                raise ValueError(f"duplicate param: {item.name}")
            seen.add(item.name)
        return value

    def reusable_names(self) -> list[str]:
        """Names a reuser is allowed to override."""
        return [p.name for p in self.params if p.scope == "reusable"]

    def fixed_names(self) -> list[str]:
        """Names pinned by the author."""
        return [p.name for p in self.params if p.scope == "fixed"]


class RecipeRefSection(BaseModel):
    """Pointer from a card to the executable recipe (DAG) behind it.

    The card stays a facade: the steps live in the recipe, the run lives in a
    task graph, and the produced files live under the run's outputs.
    """

    model_config = ConfigDict(extra="forbid")

    recipe_id: str = Field(
        description="Address of the recipe, e.g. 'recipe://geonexus/sdg-15-3-1@1.0.0'."
    )
    version: str | None = Field(default=None, description="Semantic version, when not in the id.")
    entrypoint: str | None = Field(
        default=None, description="Step to start from; defaults to the recipe's entry step."
    )
    data_snapshot: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Data versions this solution was authored against, e.g. "
            "{'geokg': '2026.09.1'} — so a replay can tell drift from a real change."
        ),
    )


# --------------------------------------------------------------------------- #
# GeoCard
# --------------------------------------------------------------------------- #
class GeoCard(BaseModel):
    """Machine-readable identity, capability and contract description
    for a geospatial asset."""

    model_config = ConfigDict(extra="allow")

    geocard_version: str = Field(default="1.0", description="GeoCard spec version.")
    id: str = Field(description="Stable unique identifier of the asset.")
    type: str = Field(
        description=("One of: data, model, skill, agent, workflow, knowledge, compute.")
    )
    name: str = Field(description="Human-readable display name.")
    description: str = Field(description="Free-text description.")

    tags: list[str] = Field(default_factory=list)
    spatial: SpatialSection | None = None
    temporal: TemporalSection | None = None
    bands: list[Band] = Field(default_factory=list)
    inputs: list[IOField] = Field(default_factory=list)
    outputs: list[IOField] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)
    access: AccessSection | None = None
    provenance: ProvenanceSection | None = None
    license: Any | None = None  # str (SPDX) or LicenseField
    compliance: ComplianceSection | None = None
    trust: TrustSection | None = None
    runtime: RuntimeSection | None = None
    interface: InterfaceSection | None = None
    rendering: RenderingSection | None = None
    # Composition of a solution (type: workflow / case-style cards). Additive in
    # 1.x: cards written before these sections existed stay valid untouched.
    composition: CompositionSection | None = None
    params: ParamsSection | None = None
    recipe_ref: RecipeRefSection | None = None

    # --- serialisation ------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        """Return a plain JSON-compatible dictionary of this card.

        Unset (``None``) fields are omitted — including None values produced
        by field serializers (pydantic's ``exclude_none`` does not strip
        those) — so the result conforms to the strict per-section schema.
        """
        return _strip_none_values(self.model_dump(exclude_none=True))

    def to_yaml(self) -> str:
        """Serialize the card to YAML."""
        return yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True)

    def to_json(self) -> str:
        """Serialize the card to JSON."""
        return json.dumps(self.to_dict(), indent=2)

    def save(self, path: str) -> GeoCard:
        """Persist the card to a YAML or JSON file (format by extension)."""
        if path.endswith(".json"):
            payload = self.to_json()
        else:
            payload = self.to_yaml()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(payload)
        logger.info("Saved GeoCard '%s' to %s", self.id, path)
        return self

    # --- validation --------------------------------------------------------- #
    # Intentionally shadows pydantic v2's deprecated legacy classmethod
    # `validate` with an instance method; the type override is deliberate.
    def validate(self) -> GeoCard:  # type: ignore[override]
        """Validate this card against the official GeoCard JSON Schema.

        Raises :class:`GeoCardValidationError` when the card is invalid.
        Returns ``self`` for chaining.
        """
        from .validator import validate_card_schema

        report = validate_card_schema(self.to_dict())
        if not report.valid:
            raise GeoCardValidationError(report.errors)
        return self

    @classmethod
    def validate_data(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Validate a raw dictionary against the official GeoCard schema."""
        from .validator import validate_card_schema

        report = validate_card_schema(data)
        if not report.valid:
            raise GeoCardValidationError(report.errors)
        return data

    # --- loading ------------------------------------------------------------ #
    @classmethod
    def load(cls, path: str) -> GeoCard:
        """Load a GeoCard from a YAML or JSON file."""
        from .loader import load_geocard

        return load_geocard(path)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GeoCard:
        """Build a GeoCard from a dictionary, tolerating JSON-style input."""
        return cls.model_validate(data)

    def capability_names(self) -> list[str]:
        """Return the list of capability names declared by this card."""
        return [c.name for c in self.capabilities]

    def band_names(self) -> list[str]:
        """Return the list of band names declared by this card."""
        return [b.name for b in self.bands]

    def component_ids(self) -> list[str]:
        """Return the ids of the assets this composite card references."""
        return [c.id for c in self.composition.components] if self.composition else []

    def reusable_param_names(self) -> list[str]:
        """Return the parameter names a reuser may override."""
        return self.params.reusable_names() if self.params else []

    def fixed_param_names(self) -> list[str]:
        """Return the parameter names the author pinned."""
        return self.params.fixed_names() if self.params else []

    @field_validator("capabilities", mode="before")
    @classmethod
    def _coerce_capabilities(cls, value: Any) -> Any:
        """Accept plain strings or objects for capabilities (schema allows both)."""
        if value is None:
            return value
        coerced = []
        for item in value:
            if isinstance(item, str):
                coerced.append({"name": item})
            else:
                coerced.append(item)
        return coerced

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"GeoCard(id={self.id!r}, type={self.type!r}, name={self.name!r})"
