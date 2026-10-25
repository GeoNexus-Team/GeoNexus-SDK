"""geonexus.geocard.contracts 的单元测试。"""

from __future__ import annotations

import pytest

from geonexus.geocard import GeoCard
from geonexus.geocard.contracts import (
    ContractRequest,
    ResultManifest,
    validate_contract,
)

CARD = {
    "id": "demo-amazon-reflectance",
    "type": "data",
    "name": "亚马逊反射率 · 合成 Demo",
    "description": "2015 / 2025 两期合成红光与近红外栅格，非真实卫星观测。",
    "spatial": {"bbox": [-73.9, -15, -44, 5], "crs": "EPSG:4326"},
    "temporal": {"start": "2015-01-01", "end": "2025-12-31"},
    "bands": [
        {"name": "red", "units": "reflectance"},
        {"name": "nir", "units": "reflectance"},
    ],
    "license": "Apache-2.0",
    "compliance": {
        "sovereignty": "local-node",
        "restrictions": ["no-export"],
        "sensitivity": "public",
    },
}
CONTEXT = {
    "bbox": [-73.9, -15, -44, 5],
    "crs": "EPSG:4326",
    "start": "2015-01-01",
    "end": "2025-12-31",
    "required_bands": ["red", "nir"],
    "required_units": {"red": "reflectance", "nir": "reflectance"},
}


def card():
    return GeoCard.from_dict(CARD)


def test_valid_context_is_satisfied():
    report = validate_contract(card(), ContractRequest(**CONTEXT))
    assert report.valid and not [f for f in report.findings if f.severity == "error"]
    assert report.normalized_context.required_bands == ["red", "nir"]


def test_export_is_rejected_by_no_export_restriction():
    report = validate_contract(card(), ContractRequest(**CONTEXT, export_data=True))
    assert not report.valid
    assert any(f.code == "raw_export_forbidden" for f in report.findings)


def test_clipped_extent_is_rejected():
    report = validate_contract(card(), ContractRequest(**{**CONTEXT, "bbox": [-120, 30, -100, 40]}))
    assert not report.valid
    assert any(f.code == "extent_out_of_bounds" for f in report.findings)


def test_time_window_outside_card_is_rejected():
    report = validate_contract(card(), ContractRequest(**{**CONTEXT, "start": "2010-01-01"}))
    assert not report.valid
    assert any(f.code == "temporal_out_of_bounds" for f in report.findings)


def test_missing_band_and_unit_mismatch_are_reported():
    missing = validate_contract(card(), ContractRequest(**{**CONTEXT, "required_bands": ["swir"]}))
    assert any(f.code == "missing_band" for f in missing.findings)
    units = validate_contract(
        card(), ContractRequest(**{**CONTEXT, "required_units": {"red": "dn"}})
    )
    assert any(f.code == "unit_mismatch" for f in units.findings)


def test_crs_mismatch_is_rejected():
    report = validate_contract(card(), ContractRequest(**{**CONTEXT, "crs": "EPSG:3857"}))
    assert not report.valid


def test_result_manifest_roundtrip():
    manifest = ResultManifest(
        execution_id="run-1",
        status="succeeded",
        card_ids=["demo-amazon-reflectance"],
        node_id="node-local",
        synthetic=True,
        created_at="2026-10-25T00:00:00Z",
        artifacts=[{"name": "preview.geojson", "href": "/api/v1/results/run-1/artifacts/preview.geojson"}],
        provenance={"raw_data_exported": False, "mean_change": -0.42},
    )
    again = ResultManifest.model_validate_json(manifest.model_dump_json())
    assert again.artifacts[0].name == "preview.geojson"
    assert again.provenance["raw_data_exported"] is False
