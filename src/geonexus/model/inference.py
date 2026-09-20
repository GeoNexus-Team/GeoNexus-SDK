"""InferenceProvider —— 对**独立推理服务**的同步调用（决策 D4）。

模型不跑在 GeoNode 进程里（那会占进程、难隔离、显存不可管），而是跑在自己的推理
服务后面；这里用一次 HTTP 请求把输入送过去、把产物取回来。

约定（最小、可由任何服务实现）::

    POST {endpoint}
    {"model": "...", "inputs": {...}, "params": {...}, "request_id": "..."}

    200 → {"outputs": {...}}        或直接 {"<output_name>": ...}
    4xx → 判定为不可重试（请求本身有问题）
    5xx / 超时 / 连不上 → 可重试（服务或网络的问题）
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .invocation import ModelInvocation, ModelOutcome

logger = logging.getLogger(__name__)

__all__ = ["InferenceProvider"]


class InferenceProvider:
    """一次请求一次响应的推理服务。"""

    def __init__(self, endpoint: str, *, timeout: float = 300.0,
                 api_key: str | None = None, headers: dict[str, str] | None = None,
                 client: httpx.Client | None = None) -> None:
        self.name = "inference"
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._owns_client = client is None
        self._extra_headers = dict(headers or {})

    def capabilities(self) -> dict[str, Any]:
        return {"name": self.name, "endpoint": self.endpoint, "transport": "http",
                "modes": ["sync"]}

    def retarget(self, endpoint: str) -> InferenceProvider:
        """改指向另一台算力（算力门禁选中了别的节点时）。

        只换地址，复用同一个 httpx 客户端——门禁结论要能真的改变调用目标。
        """
        clone = InferenceProvider(
            endpoint, timeout=self.timeout, api_key=self.api_key,
            headers=self._extra_headers, client=self._client)
        clone._owns_client = False
        return clone

    # ------------------------------------------------------------------ #
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self._extra_headers}
        if self.api_key:
            headers.setdefault("Authorization", f"Bearer {self.api_key}")
        return headers

    def invoke(self, invocation: ModelInvocation) -> ModelOutcome:
        payload = {
            "model": invocation.model_id,
            "inputs": invocation.inputs,
            "params": invocation.params,
            "request_id": invocation.request_id,
        }
        try:
            response = self._client.post(self.endpoint, json=payload,
                                         headers=self._headers(),
                                         timeout=invocation.timeout)
        except httpx.TimeoutException as exc:
            return ModelOutcome.failure("timeout", f"推理服务超时：{exc}",
                                        retryable=True, reasons=[str(exc)])
        except httpx.HTTPError as exc:
            return ModelOutcome.failure("executor_lost", f"连不上推理服务：{exc}",
                                        retryable=True, reasons=[str(exc)])

        if response.status_code >= 500:
            return ModelOutcome.failure(
                "resource_unavailable",
                f"推理服务 {response.status_code}：{response.text[:200]}",
                retryable=True)
        if response.status_code >= 400:
            # 4xx 是请求本身的问题，换一台机器也一样
            return ModelOutcome.failure(
                "invalid_spec",
                f"推理服务拒绝请求 {response.status_code}：{response.text[:200]}",
                retryable=False)

        try:
            body = response.json()
        except ValueError as exc:
            return ModelOutcome.failure(
                "execution_error", f"推理服务返回的不是 JSON：{exc}", retryable=False)

        outputs = body.get("outputs") if isinstance(body, dict) and "outputs" in body else body
        if not isinstance(outputs, dict):
            return ModelOutcome.failure(
                "invalid_spec",
                f"推理服务返回的产物不是对象：{type(outputs).__name__}",
                retryable=False)
        return ModelOutcome(status="succeeded", outputs=outputs, provider=self.name)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> InferenceProvider:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
