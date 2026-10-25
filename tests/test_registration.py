"""Provider onboarding must not fabricate missing metadata or leak credentials."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from geonexus.registration import (
    RegistrationError,
    ServiceState,
    main,
    make_draft,
    submit,
    validate_profile,
)


def descriptor(kind):
    value = {
        "id": f"sample.{kind}",
        "name": "Sample",
        "description": "Explicit provider metadata",
        "access": {"protocol": "mcp", "endpoint": "https://node.example.org/mcp"},
        "license": "MIT",
        "provenance": {"provider": "Example"},
        "revision": "1.0.0",
    }
    if kind in {"operator", "model"}:
        value.update(
            inputSchema={"type": "object", "properties": {"value": {"type": "number"}}},
            outputSchema={"type": "object", "properties": {"result": {"type": "number"}}},
        )
    if kind == "model":
        value["model_id"] = "sample-model"
    if kind == "compute":
        value.update(runtime={"cpu": 2, "memory": "1Gi"}, backends=["python"])
    return value


@pytest.mark.parametrize("kind", ["data", "operator", "model", "compute"])
def test_four_resource_profiles(kind):
    draft = make_draft(descriptor(kind), kind)
    assert draft["validation"]["valid"]
    assert draft["card"]["type"] == ("skill" if kind == "operator" else kind)
    assert "source_digest" in draft["card"]["x-geonexus"]
    assert "node_url" not in draft["binding"]


def test_missing_metadata_and_unknown_capacity_fail_explicitly():
    draft = make_draft({"name": "Incomplete"}, "compute")
    assert not draft["validation"]["valid"]
    assert not draft["card"].get("license")
    assert "cpu" not in draft["card"].get("runtime", {})


@pytest.mark.parametrize("location", ["card", "binding"])
def test_credentials_and_dynamic_state_are_separate(location):
    draft = make_draft(descriptor("model"), "model")
    draft[location]["api_key"] = "test-secret"
    assert not validate_profile(draft["card"], draft["binding"]).valid
    draft[location].pop("api_key")
    draft["card"]["x-geonexus"]["service_state"] = {"available": True}
    assert not validate_profile(draft["card"], draft["binding"]).valid


def test_data_endpoint_cannot_implicitly_become_owner_node():
    draft = make_draft(descriptor("data"), "data")
    with pytest.raises(RegistrationError, match="owner GeoNode"):
        submit(draft, "https://registry.example.org")


def test_state_expires_and_unknown_timestamps_do_not_become_available():
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    assert not ServiceState("x", "available", past, past).fresh()
    assert ServiceState("x", "available", past, future).fresh()
    assert not ServiceState("x", "available", past, "invalid").fresh()


def test_cli_generates_explicit_node_binding_and_refuses_overwrite(tmp_path):
    src, out = tmp_path / "metadata.json", tmp_path / "draft.json"
    src.write_text(json.dumps(descriptor("operator")))
    args = [
        "init",
        str(src),
        "--kind",
        "operator",
        "--node",
        "https://owner.example.org",
        "--out",
        str(out),
    ]
    assert main(args) == 0
    assert json.loads(out.read_text())["binding"]["node_url"] == "https://owner.example.org"
    assert main(args) == 1
