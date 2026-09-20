"""GeoTask 数据模型 —— 见 ``docs/GEOTASK.md``（契约已冻结）。

本模块只放**数据结构与取值**，不放状态迁移（那是 :mod:`geonexus.geotask.machine`）
也不放调度（那是 :mod:`geonexus.geotask.runner`）。

字段命名刻意与 GeoCard 对齐：``inputs``/``outputs`` 用引用而非值（数据主权：产物可以
只留在原地），``provenance`` 沿用同一套强制字段（``origin``/``source``/``license``/
``retrieved``）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "TaskKind",
    "TaskState",
    "TaskError",
    "ERROR_RETRYABLE",
    "RetryPolicy",
    "TaskIO",
    "GpuRequirement",
    "ResourceRequirement",
    "TaskResult",
    "GeoTask",
]


class TaskKind(str, Enum):
    """任务种类 —— 决定用哪个执行后端。"""

    SKILL = "skill"  # 本地/下推执行一个技能
    MODEL = "model"  # 调用模型（独立推理服务或批作业）
    BATCH = "batch"  # 批/云作业：提交 → 轮询 → 回收
    COMPOSITE = "composite"  # 本身不执行，只组织子任务


class TaskState(str, Enum):
    """**唯一**的任务状态词表。

    取代原先四套：``web``(done/cancelled) / ``cafe``(succeeded/timeout) /
    ``agent``(pending/done/skipped) / ``OgeTaskPoller``(远端自有)。
    其中 ``timeout`` 不是状态而是 ``failed`` + ``code=timeout``：状态描述"走到哪了"，
    错误码描述"为什么"（见 GEOTASK §3.2）。
    """

    CREATED = "created"
    QUEUED = "queued"
    BLOCKED = "blocked"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


#: 错误码 → 是否可重试。这张表是契约 §5.1 的机器可读形式：
#: 「不可重试」的语义是"重试只会再失败一次，并掩盖真正的问题"。
ERROR_RETRYABLE: dict[str, bool] = {
    "input_missing": False,
    "invalid_spec": False,
    "no_compatible_compute": False,
    "resource_unavailable": True,
    "executor_lost": True,
    "timeout": True,
    "execution_error": True,
    "upstream_failed": False,
    "cancelled": False,
}


@dataclass
class TaskError:
    """失败原因。``retryable`` 默认由 :data:`ERROR_RETRYABLE` 决定，不是随手填的。"""

    code: str
    message: str = ""
    retryable: bool = False

    @classmethod
    def of(cls, code: str, message: str = "", *, retryable: bool | None = None) -> TaskError:
        if retryable is None:
            retryable = ERROR_RETRYABLE.get(code, True)
        return cls(code=code, message=message, retryable=retryable)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


@dataclass
class RetryPolicy:
    """重试策略。

    ``max_attempts`` 是**总尝试次数**（含首次）：1 = 不重试。
    判定式是 ``(attempt + 1) < max_attempts``，即 attempt 从 0 开始计数、表示
    "当前是第几次尝试"。
    """

    max_attempts: int = 1
    backoff_seconds: float = 1.0
    multiplier: float = 2.0
    max_backoff_seconds: float = 60.0

    def allows_another_attempt(self, attempt: int) -> bool:
        return (attempt + 1) < self.max_attempts

    def backoff_for(self, attempt: int) -> float:
        """第 ``attempt`` 次失败后的退避秒数（退避只影响何时重新入队）。"""
        if self.backoff_seconds <= 0:
            return 0.0
        raw = self.backoff_seconds * (self.multiplier ** max(0, attempt))
        return float(min(raw, self.max_backoff_seconds))

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "backoff_seconds": self.backoff_seconds,
            "multiplier": self.multiplier,
            "max_backoff_seconds": self.max_backoff_seconds,
        }


@dataclass
class TaskIO:
    """输入/输出的**引用**，不是值。

    ``kind`` 为 ``task_output`` 时表示取自上游任务的某个字段（等价于 agent 现有的
    ``${stepN.outputs.key}`` 模板，但用引用表达，避免把值内联进参数）。
    """

    ref: str
    kind: str = "asset"  # asset | literal | task_output
    from_task: str | None = None
    field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "kind": self.kind,
            "from_task": self.from_task,
            "field": self.field,
        }


@dataclass
class GpuRequirement:
    kind: str = "cuda"  # 与 GeoCard runtime.gpu 同一套取值
    count: int = 1
    memory_mb: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "count": self.count, "memory_mb": self.memory_mb}


@dataclass
class ResourceRequirement:
    """**预约**需求（区别于 GeoCard ``runtime`` 的**声明**）。

    记账单位见契约 §2.2：调度器按 ``cpu_cores`` / ``memory_mb`` / ``gpu_count``
    (+``gpu_kind``) 记预约之和，不记实际用量。
    """

    cpu: float = 0.0
    memory_mb: int = 0
    gpu: GpuRequirement | None = None

    @classmethod
    def from_geocard_runtime(cls, runtime: dict[str, Any] | None) -> ResourceRequirement:
        """把 GeoCard 的 ``runtime.{cpu,memory,gpu}`` 换算成预约需求。

        **单位边界**：``geonexus.resource.parse_memory`` 返回 **GiB**，而本类的记账
        单位是 **MB**（契约 §2.2），所以这里必须 ×1024。字符串解析复用既有实现，
        不另写一份。
        """
        from ..resource import parse_memory

        runtime = runtime or {}
        cpu_raw = runtime.get("cpu")
        try:
            cpu = float(cpu_raw) if cpu_raw not in (None, "") else 0.0
        except (TypeError, ValueError):
            cpu = 0.0
        memory_gib = parse_memory(runtime.get("memory"))
        memory_mb = int(round(memory_gib * 1024)) if memory_gib is not None else 0
        gpu_raw = runtime.get("gpu")
        gpu = None
        if gpu_raw and str(gpu_raw).lower() not in ("none", "false", "0"):
            gpu = GpuRequirement(kind=str(gpu_raw))
        return cls(cpu=cpu, memory_mb=memory_mb, gpu=gpu)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu": self.cpu,
            "memory_mb": self.memory_mb,
            "gpu": self.gpu.to_dict() if self.gpu else None,
        }


@dataclass
class TaskResult:
    """产物：名称 → 引用（与 ``outputs`` 的声明对应）。"""

    outputs: dict[str, Any] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"outputs": self.outputs, "elapsed_seconds": self.elapsed_seconds, "meta": self.meta}


@dataclass
class GeoTask:
    """一个可调度、可观测、可重放的任务。``task_id`` 同时是**幂等键**（契约 §5.4）。"""

    task_id: str
    kind: TaskKind
    spec: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, TaskIO] = field(default_factory=dict)
    outputs: dict[str, TaskIO] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    priority: int = 0
    resources: ResourceRequirement = field(default_factory=ResourceRequirement)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    owner: str | None = None
    tenant: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 300.0
    description: str = ""

    # -- 运行时状态：由状态机维护，调用方不要直接改 -------------------------- #
    state: TaskState = TaskState.CREATED
    attempt: int = 0
    scheduled_to: str | None = None
    lease_expires_at: float | None = None
    cancel_requested: bool = False
    progress: float = 0.0
    message: str = ""
    result: TaskResult | None = None
    error: TaskError | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    # ------------------------------------------------------------------ #
    def is_terminal(self) -> bool:
        from .machine import TERMINAL_STATES

        return self.state in TERMINAL_STATES

    def to_dict(self, *, include_result: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "task_id": self.task_id,
            "kind": self.kind.value,
            "spec": self.spec,
            "inputs": {k: v.to_dict() for k, v in self.inputs.items()},
            "outputs": {k: v.to_dict() for k, v in self.outputs.items()},
            "depends_on": list(self.depends_on),
            "priority": self.priority,
            "resources": self.resources.to_dict(),
            "retry": self.retry.to_dict(),
            "owner": self.owner,
            "tenant": self.tenant,
            "labels": self.labels,
            "provenance": self.provenance,
            "timeout_seconds": self.timeout_seconds,
            "description": self.description,
            "state": self.state.value,
            "attempt": self.attempt,
            "scheduled_to": self.scheduled_to,
            "lease_expires_at": self.lease_expires_at,
            "cancel_requested": self.cancel_requested,
            "progress": self.progress,
            "message": self.message,
            "error": self.error.to_dict() if self.error else None,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        if include_result:
            out["result"] = self.result.to_dict() if self.result else None
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GeoTask:
        task = cls(
            task_id=data["task_id"],
            kind=TaskKind(data["kind"]),
            spec=dict(data.get("spec") or {}),
            depends_on=list(data.get("depends_on") or []),
            priority=int(data.get("priority") or 0),
            owner=data.get("owner"),
            tenant=data.get("tenant"),
            labels=dict(data.get("labels") or {}),
            provenance=dict(data.get("provenance") or {}),
            timeout_seconds=float(data.get("timeout_seconds") or 300.0),
            description=data.get("description") or "",
        )
        task.inputs = {k: TaskIO(**v) for k, v in (data.get("inputs") or {}).items()}
        task.outputs = {k: TaskIO(**v) for k, v in (data.get("outputs") or {}).items()}
        task.resources = ResourceRequirement(
            cpu=(data.get("resources") or {}).get("cpu", 0.0),
            memory_mb=(data.get("resources") or {}).get("memory_mb", 0),
            gpu=(
                GpuRequirement(**(data["resources"]["gpu"]))
                if (data.get("resources") or {}).get("gpu")
                else None
            ),
        )
        task.retry = RetryPolicy(**(data.get("retry") or {}))
        task.state = TaskState(data.get("state", TaskState.CREATED.value))
        task.attempt = int(data.get("attempt") or 0)
        task.scheduled_to = data.get("scheduled_to")
        task.lease_expires_at = data.get("lease_expires_at")
        task.cancel_requested = bool(data.get("cancel_requested"))
        task.progress = float(data.get("progress") or 0.0)
        task.message = data.get("message") or ""
        task.error = TaskError(**data["error"]) if data.get("error") else None
        task.created_at = float(data.get("created_at") or time.time())
        task.started_at = data.get("started_at")
        task.finished_at = data.get("finished_at")
        if data.get("result"):
            task.result = TaskResult(**data["result"])
        return task
