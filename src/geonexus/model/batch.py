"""BatchProvider —— 批 / 云作业：提交 → 轮询 → 回收（决策 D4）。

大训练、大批推理不能用同步请求扛（一次几十分钟，连接会被中间设备掐掉）。这类作业
的通用交互是三步：提交拿到作业 id、轮询状态、完成后取产物。`adapters.OgeTaskPoller`
就是这种形状，这里把它抽象成模型调用的一种 provider。

约定（同样最小）::

    POST {endpoint}                 → {"job_id": "...", "status": "queued"}
    GET  {endpoint}/{job_id}        → {"status": "running"|"succeeded"|"failed", ...}
                                        succeeded → {"outputs": {...}}
                                        failed    → {"error": "..."}
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .invocation import ModelInvocation, ModelOutcome

logger = logging.getLogger(__name__)

__all__ = ["BatchProvider"]

#: 远端作业状态 → 我们这边的判断。``queued``/``running`` 都不是终态。
_PENDING = frozenset({"queued", "pending", "running", "submitted", "accepted", "in_progress"})
_SUCCEEDED = frozenset({"succeeded", "success", "done", "completed", "finished"})
_FAILED = frozenset({"failed", "error", "cancelled", "canceled", "timeout", "rejected"})


class BatchProvider:
    """提交作业后轮询直到终态。"""

    def __init__(
        self,
        endpoint: str,
        *,
        timeout: float = 3600.0,
        poll_interval: float = 1.0,
        api_key: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.name = "batch"
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.api_key = api_key
        self._client = client or httpx.Client(timeout=60.0, follow_redirects=True)
        self._owns_client = client is None
        #: 提交过的作业，便于外部查询与"回收"（进程重启后由调用方按 id 续查）
        self.jobs: dict[str, dict[str, Any]] = {}

    def capabilities(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "endpoint": self.endpoint,
            "transport": "http",
            "modes": ["submit", "poll", "collect"],
        }

    def retarget(self, endpoint: str) -> BatchProvider:
        clone = BatchProvider(
            endpoint,
            timeout=self.timeout,
            poll_interval=self.poll_interval,
            api_key=self.api_key,
            client=self._client,
        )
        clone._owns_client = False
        return clone

    # ------------------------------------------------------------------ #
    def submit(self, invocation: ModelInvocation) -> str:
        """只提交，不等待。返回远端作业 id（调用方可持久化后另处续查）。"""
        response = self._client.post(
            self.endpoint,
            json={
                "model": invocation.model_id,
                "inputs": invocation.inputs,
                "params": invocation.params,
                "request_id": invocation.request_id,
            },
            headers=self._headers(),
            timeout=invocation.timeout,
        )
        response.raise_for_status()
        body = response.json()
        job_id = body.get("job_id") or body.get("id")
        if not job_id:
            raise ValueError(f"批服务未返回 job_id: {body!r}")
        self.jobs[str(job_id)] = {"invocation": invocation, "submitted_at": time.time()}
        return str(job_id)

    def poll(self, job_id: str) -> dict[str, Any]:
        response = self._client.get(
            f"{self.endpoint}/{job_id}", headers=self._headers(), timeout=60.0
        )
        response.raise_for_status()
        return response.json()

    def collect(self, job_id: str) -> ModelOutcome:
        """轮询到终态并取回产物。"""
        state = self.poll(job_id)
        status = str(state.get("status", "")).lower()
        if status in _SUCCEEDED:
            outputs = state.get("outputs")
            if outputs is None:
                outputs = {k: v for k, v in state.items() if k not in ("status", "job_id", "id")}
            if not isinstance(outputs, dict):
                return ModelOutcome.failure(
                    "invalid_spec", f"批作业产物不是对象：{type(outputs).__name__}"
                )
            return ModelOutcome(status="succeeded", outputs=outputs, provider=self.name)
        if status in _FAILED:
            retryable = status in ("timeout", "cancelled", "canceled")
            return ModelOutcome.failure(
                "timeout" if status == "timeout" else "execution_error",
                str(state.get("error") or f"批作业状态 {status}"),
                retryable=retryable,
            )
        return ModelOutcome.failure("execution_error", f"批作业仍在 {status or '未知'} 状态")

    def invoke(self, invocation: ModelInvocation) -> ModelOutcome:
        """提交 → 轮询 → 回收，直到终态或超时。"""
        try:
            job_id = self.submit(invocation)
        except httpx.HTTPError as exc:
            return ModelOutcome.failure(
                "executor_lost", f"批服务不可达：{exc}", retryable=True, reasons=[str(exc)]
            )
        except ValueError as exc:
            return ModelOutcome.failure("invalid_spec", str(exc))

        deadline = time.time() + invocation.timeout
        while time.time() < deadline:
            try:
                state = self.poll(job_id)
            except httpx.HTTPError as exc:
                logger.warning("批作业 %s 轮询失败：%s", job_id, exc)
                time.sleep(self.poll_interval)
                continue
            status = str(state.get("status", "")).lower()
            if status in _SUCCEEDED or status in _FAILED:
                logger.info("批作业 %s 到达终态 %s", job_id, status)
                return self.collect(job_id)
            time.sleep(self.poll_interval)

        return ModelOutcome.failure(
            "timeout",
            f"批作业 {job_id} 在 {invocation.timeout}s 内未完成",
            retryable=True,
            reasons=[f"job_id={job_id}"],
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
