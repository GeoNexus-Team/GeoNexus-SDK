"""GeoRegister: metadata-only onboarding, independent of GIS and LLM runtimes.

Profiles supplement GeoCard 1.0; bindings and observed state stay separate.
No discovery function evaluates supplied code or runs an operator/model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import jsonschema
import yaml

from .geocard import validate_card_schema

KINDS = {"data": "data", "operator": "skill", "model": "model", "compute": "compute"}
SECRET_FIELDS = {"api_key", "apikey", "password", "token", "secret", "authorization"}


class RegistrationError(ValueError):
    """A source, profile, binding or registration is invalid."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise RegistrationError(
            "Redirect refused; inspect and use the final metadata URL explicitly."
        )


def safe_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise RegistrationError("Use an HTTP(S) endpoint without embedded credentials.")
    if parsed.fragment:
        raise RegistrationError("Endpoint fragments are not supported.")
    from urllib.parse import parse_qsl

    if any(key.lower() in SECRET_FIELDS for key, _ in parse_qsl(parsed.query)):
        raise RegistrationError("Put credentials in a credential reference, not the endpoint.")
    return value


def digest(value: Any) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
    )


def read_source(source: str, *, timeout: float = 15) -> dict[str, Any]:
    """Read a caller-selected metadata source, with a 2 MiB size limit."""
    if source.startswith(("https://", "http://")):
        request = Request(safe_url(source), headers={"Accept": "application/json"})
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            payload = response.read(2 * 1024 * 1024 + 1)
    else:
        path = Path(source)
        if path.stat().st_size > 2 * 1024 * 1024:
            raise RegistrationError("Metadata exceeds 2 MiB.")
        payload = path.read_bytes()
    if len(payload) > 2 * 1024 * 1024:
        raise RegistrationError("Metadata exceeds 2 MiB.")
    data = yaml.safe_load(payload)
    if not isinstance(data, dict):
        raise RegistrationError("The metadata source must contain an object.")
    return data


def _contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() in SECRET_FIELDS or _contains_secret(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return False


@dataclass
class ValidationReport:
    schema_errors: list[str] = field(default_factory=list)
    profile_errors: list[str] = field(default_factory=list)
    binding_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not (self.schema_errors or self.profile_errors or self.binding_errors)

    def to_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, **asdict(self)}


def validate_profile(
    card: dict[str, Any], binding: dict[str, Any] | None = None
) -> ValidationReport:
    report = ValidationReport(schema_errors=validate_card_schema(card).errors)
    if report.schema_errors:
        return report
    if _contains_secret(card) or _contains_secret(binding or {}):
        report.profile_errors.append(
            "Credentials must be external; only credential_ref is allowed."
        )
    if any(
        key in card or key in (card.get("x-geonexus") or {})
        for key in ("service_state", "available", "observed_at", "expires_at", "health")
    ):
        report.profile_errors.append(
            "Dynamic deployment state belongs in ServiceState, not the static GeoCard."
        )
    access = card.get("access") or {}
    if not access.get("endpoint") or not access.get("protocol"):
        report.profile_errors.append("access.endpoint and access.protocol are required.")
    endpoint = access.get("endpoint", "")
    if endpoint.startswith(("http://", "https://")):
        try:
            safe_url(endpoint)
        except RegistrationError as exc:
            report.profile_errors.append(str(exc))
    if not card.get("license"):
        report.profile_errors.append(
            "Specify the resource license or an explicit access-policy reference."
        )
    if not (card.get("provenance") or {}).get("provider"):
        report.profile_errors.append("provenance.provider is required.")
    extra = card.get("x-geonexus") or {}
    if not extra.get("revision"):
        report.profile_errors.append("x-geonexus.revision must identify the resource revision.")
    kind = card.get("type")
    if kind in {"skill", "model"}:
        for key in ("input_schema", "output_schema"):
            schema = extra.get(key)
            if not isinstance(schema, dict) or not schema:
                report.profile_errors.append(f"x-geonexus.{key} must be a complete JSON Schema.")
            else:
                try:
                    jsonschema.Draft202012Validator.check_schema(schema)
                except jsonschema.SchemaError as exc:
                    report.profile_errors.append(f"{key}: {exc.message}")
    if kind == "model" and not extra.get("model_id") and not extra.get("artifact"):
        report.profile_errors.append("A model_id or immutable artifact reference is required.")
    if kind == "compute":
        runtime = card.get("runtime") or {}
        if (
            not isinstance(runtime.get("cpu"), int)
            or isinstance(runtime.get("cpu"), bool)
            or runtime["cpu"] < 1
        ):
            report.profile_errors.append("compute runtime.cpu must be a known positive integer.")
        from .resource import parse_memory

        capacity = parse_memory(runtime.get("memory"))
        if capacity is None or capacity <= 0:
            report.profile_errors.append("compute runtime.memory is required.")
        if (
            not isinstance(extra.get("backends"), list)
            or not extra["backends"]
            or not all(isinstance(item, str) and item for item in extra["backends"])
        ):
            report.profile_errors.append("compute x-geonexus.backends is required.")
    if kind == "data" and not card.get("spatial"):
        report.warnings.append(
            "Spatial extent is unspecified; do not assume spatial compatibility."
        )
    if binding is not None:
        from importlib.resources import files

        binding_schema = json.loads(
            files("geonexus.data").joinpath("service-binding.schema.json").read_text()
        )
        report.binding_errors.extend(
            error.message
            for error in jsonschema.Draft202012Validator(binding_schema).iter_errors(binding)
        )
        if binding.get("card_id") != card.get("id"):
            report.binding_errors.append("Binding card_id differs from GeoCard id.")
        if not binding.get("endpoint") and not binding.get("command"):
            report.binding_errors.append(
                "Binding needs an endpoint or an explicitly configured command."
            )
        if binding.get("command") and binding.get("transport") != "stdio":
            report.binding_errors.append("Commands require a stdio binding.")
        if binding.get("endpoint", "").startswith(("http://", "https://")):
            try:
                safe_url(binding["endpoint"])
            except RegistrationError as exc:
                report.binding_errors.append(str(exc))
    return report


def make_draft(
    metadata: dict[str, Any],
    kind: str,
    *,
    source: str = "",
    operation: str | None = None,
) -> dict[str, Any]:
    """Convert a descriptor, STAC item, MCP tool, OGC process or OpenAPI operation."""
    if kind not in KINDS:
        raise RegistrationError(f"Unknown resource kind: {kind}")
    if "geocard_version" in metadata:
        card = json.loads(json.dumps(metadata))
    elif kind == "data" and metadata.get("stac_version"):
        try:
            from .adapters.stac import stac_item_to_geocard

            card = stac_item_to_geocard(metadata, geocard_version="1.0").to_dict()
        except ImportError as exc:
            raise RegistrationError("STAC conversion needs the SDK [http] extra.") from exc
    else:
        selected = metadata
        if metadata.get("openapi"):
            operations = [
                (path, method, item)
                for path, methods in metadata.get("paths", {}).items()
                for method, item in methods.items()
                if method.lower() in {"get", "post", "put", "patch", "delete"}
                and isinstance(item, dict)
                and item.get("operationId") == operation
            ]
            if len(operations) != 1:
                raise RegistrationError(
                    "Select an unambiguous OpenAPI operationId with --operation."
                )
            path, method, selected = operations[0]
            request_schema = selected.get("requestBody", {}).get("content", {}).get(
                "application/json", {}
            ).get("schema") or {
                "type": "object",
                "properties": {
                    p["name"]: p.get("schema", {})
                    for p in selected.get("parameters", [])
                    if "name" in p
                },
            }
            output_schema: dict[str, Any] = next(
                (
                    r.get("content", {}).get("application/json", {}).get("schema", {})
                    for code, r in selected.get("responses", {}).items()
                    if str(code).startswith("2")
                ),
                {},
            )
            selected = dict(
                selected, id=operation, input_schema=request_schema, output_schema=output_schema
            )
            if metadata.get("components"):
                selected["input_schema"] = dict(request_schema, components=metadata["components"])
                selected["output_schema"] = dict(output_schema, components=metadata["components"])
        card = {
            "geocard_version": "1.0",
            "id": str(
                selected.get("id")
                or selected.get("name")
                or f"urn:geocard:{kind}:{digest(metadata)[7:23]}"
            ),
            "type": KINDS[kind],
            "name": str(
                selected.get("name")
                or selected.get("title")
                or selected.get("id")
                or "Imported draft"
            ),
            "description": str(
                selected.get("description")
                or selected.get("summary")
                or "Metadata draft; provider review required."
            ),
        }
        for section in (
            "access",
            "spatial",
            "temporal",
            "bands",
            "inputs",
            "outputs",
            "capabilities",
            "provenance",
            "license",
            "compliance",
            "runtime",
            "interface",
            "tags",
        ):
            if section in selected:
                card[section] = selected[section]
        card["x-geonexus"] = dict(selected.get("x-geonexus") or {})
        for key in (
            "revision",
            "input_schema",
            "output_schema",
            "artifact",
            "model_id",
            "backends",
        ):
            if key in selected:
                card["x-geonexus"][key] = selected[key]
        for old, new in (("inputSchema", "input_schema"), ("outputSchema", "output_schema")):
            if old in selected:
                card["x-geonexus"][new] = selected[old]
        if "inputs" in selected and isinstance(selected["inputs"], dict):
            card.pop("inputs", None)
            card["x-geonexus"]["input_schema"] = {
                "type": "object",
                "properties": {
                    name: value.get("schema", {}) for name, value in selected["inputs"].items()
                },
            }
        if "outputs" in selected and isinstance(selected["outputs"], dict):
            card.pop("outputs", None)
            card["x-geonexus"]["output_schema"] = {
                "type": "object",
                "properties": {
                    name: value.get("schema", {}) for name, value in selected["outputs"].items()
                },
            }
    card["type"] = KINDS[kind]
    extra = card.setdefault("x-geonexus", {})
    extra.setdefault("profile", f"{kind}/1")
    extra["source_digest"] = digest(metadata)
    # Missing revision/license/provider stay missing, rather than fabricated.
    access = card.get("access") or {}
    binding = {
        "binding_version": "0.1",
        "card_id": card["id"],
        "transport": access.get("protocol", "unconfigured"),
        "endpoint": access.get("endpoint", ""),
    }
    if kind in {"operator", "model"}:
        binding["tool"] = metadata.get("name") or operation or metadata.get("id")
    if source:
        extra["metadata_source"] = source
    return {
        "card": card,
        "binding": binding,
        "validation": validate_profile(card, binding).to_dict(),
    }


@dataclass
class ServiceState:
    """Observed deployment state; never inserted into the static GeoCard."""

    binding_id: str
    status: str
    observed_at: str
    expires_at: str
    details: dict[str, Any] = field(default_factory=dict)

    def fresh(self, now: datetime | None = None) -> bool:
        try:
            expiry = datetime.fromisoformat(self.expires_at)
            return (
                self.status == "available"
                and expiry.tzinfo is not None
                and expiry > (now or datetime.now(timezone.utc))
            )
        except (ValueError, TypeError):
            return False


def probe(binding: dict[str, Any], *, timeout: float = 5, ttl: int = 30) -> ServiceState:
    endpoint = binding.get("endpoint", "")
    now = datetime.now(timezone.utc)
    status, details = "unverified", {}
    if endpoint.startswith(("http://", "https://")):
        request = Request(safe_url(endpoint), method="HEAD")
        try:
            with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
                status, details = (
                    "available",
                    {"http_status": response.status, "scope": "endpoint_reachability"},
                )
        except Exception as exc:
            status, details = "unavailable", {"error_type": type(exc).__name__}
    return ServiceState(
        binding_id=binding.get("binding_id") or binding.get("card_id", ""),
        status=status,
        observed_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl)).isoformat(),
        details=details,
    )


def submit(draft: dict[str, Any], registry: str, *, api_key: str = "") -> dict[str, Any]:
    report = validate_profile(draft["card"], draft.get("binding"))
    if not report.valid:
        raise RegistrationError(json.dumps(report.to_dict(), ensure_ascii=False))
    node_url = draft["binding"].get("node_url")
    if not node_url:
        raise RegistrationError(
            "Set binding.node_url to the owner GeoNode; the data endpoint is not a node address."
        )
    body = {"card": draft["card"], "node_url": safe_url(node_url), "status": "pending"}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    request = Request(
        safe_url(registry).rstrip("/") + "/cards", data=json.dumps(body).encode(), headers=headers
    )
    with build_opener(_NoRedirect()).open(request, timeout=15) as response:
        return json.loads(response.read(2 * 1024 * 1024))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GeoRegister: lightweight GeoCard onboarding")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("inspect", "init"):
        p = sub.add_parser(command)
        p.add_argument("source")
        p.add_argument("--kind", required=True, choices=KINDS)
        p.add_argument("--operation")
        p.add_argument("--node", help="Owner GeoNode URL, independent of asset endpoint")
        if command == "init":
            p.add_argument("--out", required=True)
    for command in ("validate", "probe", "submit"):
        p = sub.add_parser(command)
        p.add_argument("draft")
        if command == "submit":
            p.add_argument("--registry", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in {"inspect", "init"}:
            data = make_draft(
                read_source(args.source), args.kind, source=args.source, operation=args.operation
            )
            if args.node:
                data["binding"]["node_url"] = safe_url(args.node)
            if args.command == "init":
                target = Path(args.out)
                if target.exists():
                    raise RegistrationError("Output already exists; choose another draft path.")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        else:
            draft = read_source(args.draft)
            if args.command == "validate":
                data = validate_profile(draft["card"], draft.get("binding")).to_dict()
            elif args.command == "probe":
                data = asdict(probe(draft["binding"]))
            else:
                data = submit(
                    draft, args.registry, api_key=os.environ.get("GEONEXUS_REGISTRY_API_KEY", "")
                )
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 1 if args.command == "validate" and not data["valid"] else 0
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
