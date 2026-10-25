from fastapi.testclient import TestClient

from geonexus.registration import make_draft
from geonexus.registry import RegistryServer


def test_provider_cannot_self_approve_or_impersonate_reviewer(tmp_path):
    server = RegistryServer(
        public_submissions=True,
        persist_path=tmp_path / "registry.json",
        api_key_principals={
            "provider-secret": {"subject": "alice", "scopes": ["registry:submit"]},
            "review-secret": {"subject": "reviewer", "scopes": ["registry:review"]},
        },
    )
    client = TestClient(server.create_app())
    card = make_draft(
        {
            "id": "thirdparty",
            "name": "Data",
            "description": "Third party data",
            "license": "MIT",
            "provenance": {"provider": "Example"},
            "revision": "1.0.0",
            "access": {"protocol": "https", "endpoint": "https://data.example.org"},
        },
        "data",
    )["card"]
    card["trust"] = {"verified": True}
    body = {
        "card": card,
        "node_url": "https://node.example.org",
        "status": "approved",
        "submitted_by": "admin",
    }
    response = client.post("/cards", json=body, headers={"X-API-Key": "provider-secret"})
    assert response.status_code == 201
    entry = response.json()["entry"]
    assert entry["status"] == "pending" and entry["submitted_by"] == "alice"
    assert entry["card"]["trust"]["verified"] is False
    assert client.get("/cards").json()["count"] == 0
    assert (
        client.post(
            "/cards/thirdparty/approve",
            json={"by": "admin"},
            headers={"X-API-Key": "provider-secret"},
        ).status_code
        == 403
    )
    assert (
        client.post("/cards", json=body, headers={"X-API-Key": "provider-secret"}).status_code
        == 409
    )
    reviewed = client.post(
        "/cards/thirdparty/approve", json={"by": "admin"}, headers={"X-API-Key": "review-secret"}
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["entry"]["review_history"][-1]["by"] == "reviewer"
    assert client.get("/cards").json()["count"] == 1
