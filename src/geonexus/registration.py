"""High-level registration profile helpers (SDK 1.1.1 compatibility surface).

PROVISIONAL: the released 1.1.0 tree exposes the low-level ``geonexus.register``
pipeline (probe → build → validate → submit), while GeoHarness 0.2.9 and
Platform v0.9.2 import this higher-level module. It adapts the existing builders
instead of duplicating them, but the exact ``metadata``/``binding`` contract
still needs review against the dev SDK that shipped it.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .register.build import KIND_TYPE, validate_card

CARD_TYPES = {"data", "model", "skill", "agent", "workflow", "knowledge", "compute"}


class RegistrationError(Exception):
    """Raised when a registration draft or profile cannot be produced."""

    status = 400


def safe_url(value: str) -> str:
    """Validate an HTTP(S) endpoint without credentials, query or fragment."""
    if not isinstance(value, str) or not value.strip():
        raise RegistrationError("服务地址不能为空")
    if any(ord(char) < 33 for char in value):
        raise RegistrationError("服务地址不能包含空白或控制字符")
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise RegistrationError("请使用 HTTP/HTTPS 服务地址")
    if parts.username or parts.password:
        raise RegistrationError("服务地址不能包含账号或密码")
    if parts.query or parts.fragment:
        raise RegistrationError("服务地址不能包含查询参数或片段")
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


def _identity(metadata: dict[str, Any]) -> str:
    raw = metadata.get("id") or metadata.get("name") or "draft"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(raw)).strip("-").lower() or "draft"
    return slug[:120]


def make_draft(metadata: dict[str, Any], kind: str, operation: str | None = None) -> dict[str, Any]:
    """Build ``{card, binding}`` from the wizard metadata (provisional)."""
    if not isinstance(metadata, dict):
        raise RegistrationError("登记元数据必须是对象")
    if not isinstance(kind, str) or kind not in KIND_TYPE:
        raise RegistrationError(f"未知资源类型: {kind!r}")
    card_type = KIND_TYPE[kind]
    if card_type not in CARD_TYPES:
        card_type = "data"
    name = metadata.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RegistrationError("请填写资源名称")
    card: dict[str, Any] = {
        "id": _identity(metadata),
        "type": card_type,
        "name": name.strip()[:200],
        "description": str(metadata.get("description") or "")[:2000],
        "provenance": {"provider": metadata.get("provider") or "本地登记"},
    }
    for key in ("spatial", "temporal", "bands", "license", "compliance", "tags"):
        if metadata.get(key) is not None:
            card[key] = metadata[key]
    binding: dict[str, Any] = {"kind": kind}
    if operation:
        binding["operation"] = operation
    if metadata.get("node_url"):
        binding["node_url"] = safe_url(metadata["node_url"])
    return {"card": card, "binding": binding}


class ProfileReport:
    """Result of ``validate_profile`` (``.valid`` + list of findings)."""

    def __init__(self, findings: list[dict[str, str]]) -> None:
        self.findings = findings
        self.valid = not any(item.get("severity") == "error" for item in findings)

    def to_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "findings": self.findings}


def validate_profile(card: dict[str, Any], binding: dict[str, Any] | None = None) -> ProfileReport:
    """Validate a draft card and its node binding (provisional)."""
    findings: list[dict[str, str]] = []
    if not isinstance(card, dict):
        raise RegistrationError("卡片必须是对象")
    for message in validate_card(card):
        findings.append({"code": "card_invalid", "message": message, "severity": "error"})
    binding = binding or {}
    node_url = binding.get("node_url")
    if node_url:
        try:
            safe_url(node_url)
        except RegistrationError as error:
            findings.append({"code": "node_url_invalid", "message": str(error), "severity": "error"})
    else:
        findings.append(
            {"code": "node_url_missing", "message": "请提供所有者 GeoNode 地址", "severity": "warning"}
        )
    return ProfileReport(findings)
