import pytest
from pydantic import ValidationError

from geonexus.geocard import GeoCard
from geonexus.geocard.contracts import ContractRequest, RuntimeObservation, validate_contract


def card():
    return GeoCard.from_dict(
        dict(
            id="demo",
            type="data",
            name="Demo",
            description="Synthetic",
            spatial=dict(bbox=[-74, -16, -44, 6], crs="EPSG:4326"),
            temporal=dict(start="2015-01-01", end="2025-12-31"),
            bands=[dict(name="red", units="dn"), dict(name="nir", units="dn")],
            compliance=dict(
                sovereignty="local-node", restrictions=["no-export"], sensitivity="public"
            ),
        )
    )


def request(**overrides):
    return ContractRequest(
        **(
            dict(
                bbox=[-70, -10, -50, 0],
                start="2025-01-01",
                end="2025-12-31",
                required_bands=["red", "nir"],
                required_units={"red": "dn", "nir": "dn"},
            )
            | overrides
        )
    )


def test_valid_contract_and_immutable_semantics():
    asset = card()
    before = asset.to_dict()
    assert validate_contract(asset, request()).valid
    RuntimeObservation(node_id="node", registered=True, reachable=True, executing=True)
    assert asset.to_dict() == before


@pytest.mark.parametrize(
    "change",
    [
        {"crs": "EPSG:3857"},
        {"bbox": [0, 0, 1, 1]},
        {"start": "2030-01-01", "end": "2030-12-31"},
        {"required_bands": ["blue"]},
        {"required_units": {"red": "reflectance"}},
        {"export_data": True},
        {"execution_jurisdiction": "external"},
    ],
)
def test_incompatible_requests_are_blocked(change):
    assert not validate_contract(card(), request(**change)).valid


def test_missing_policy_and_units_fail_closed():
    asset = card()
    asset.compliance = None
    asset.bands[0].units = None
    result = validate_contract(asset, request())
    assert not result.valid
    assert {f.code for f in result.findings if f.status == "unknown"} == {"policy", "units:red"}


def test_invalid_time_and_extent():
    for change in ({"bbox": [0, 0, 0, 1]}, {"start": "oops"}, {"end": "2010-01-01"}):
        with pytest.raises(ValidationError):
            request(**change)


def test_timezone_normalization_and_schema_parity():
    import json
    from pathlib import Path

    from geonexus.geocard.validator import _parse_datetime, validate_card_schema

    assert _parse_datetime("2025-01-01T08:00:00+08:00") == _parse_datetime("2025-01-01T00:00:00Z")
    assert validate_contract(
        card(), request(start="2025-01-01T00:00:00Z", end="2025-12-31T00:00:00Z")
    ).valid
    asset = card()
    assert "knowledge_refs" not in asset.to_dict()
    asset.knowledge_refs = ["entity:ndvi"]
    assert validate_card_schema(asset.to_dict()).valid
    root = Path(__file__).parents[1]
    assert json.loads((root / "schemas/geocard.schema.json").read_text()) == json.loads(
        (root / "src/geonexus/data/geocard.schema.json").read_text()
    )
