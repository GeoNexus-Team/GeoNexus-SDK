"""GeoHarness 0.2.9 所需的注册中心兼容面（v1.1.1）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from geonexus.geocard import GeoCardBuilder
from geonexus.registry import RegistryServer


def card(card_id: str = "data-1"):
    return GeoCardBuilder(card_id, "data", f"Data {card_id}", "test dataset").build().model_dump(mode="json", exclude_none=True)


def test_init_accepts_harness_kwargs_and_unions_keys():
    server = RegistryServer(
        api_keys={"legacy"},
        public_submissions=True,
        api_key_principals={"submit-key": {"subject": "provider", "scopes": ["registry:submit"]}},
    )
    assert server.public_submissions is True
    assert {"legacy", "submit-key"} <= server.api_keys
    assert server.api_key_principals["submit-key"]["subject"] == "provider"


def test_register_records_submitter():
    server = RegistryServer()
    server.register(card(), "http://127.0.0.1:8787", submitted_by="local-provider")
    entry = server.get("data-1")
    assert any(item["action"] == "submitted" and item["by"] == "local-provider" for item in entry["review_history"])


def test_public_submissions_controls_pending_listing():
    private = TestClient(RegistryServer(api_keys={"k"}).create_app())
    assert private.get("/cards", params={"status": "pending"}).status_code == 401
    assert private.get("/cards", params={"status": "pending"}, headers={"X-API-Key": "k"}).status_code == 200

    public = TestClient(RegistryServer(api_keys={"k"}, public_submissions=True).create_app())
    assert public.get("/cards", params={"status": "pending"}).status_code == 200


def test_principal_scopes_gate_submission_and_review():
    principals = {
        "submit-key": {"subject": "provider", "scopes": ["registry:submit"]},
        "review-key": {"subject": "reviewer", "scopes": ["registry:review"]},
    }
    client = TestClient(RegistryServer(api_keys={"legacy"}, api_key_principals=principals).create_app())
    body = {"card": card(), "node_url": "http://127.0.0.1:8787", "status": "pending"}

    assert client.post("/cards", json=body, headers={"X-API-Key": "review-key"}).status_code == 403
    assert client.post("/cards", json=body, headers={"X-API-Key": "submit-key"}).status_code == 201
    assert client.post("/cards/data-1/approve", json={}, headers={"X-API-Key": "submit-key"}).status_code == 403
    assert client.post("/cards/data-1/approve", json={}, headers={"X-API-Key": "review-key"}).status_code == 200
