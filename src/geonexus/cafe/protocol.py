"""CAFE pushdown protocol — task negotiation, code distribution, result return."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class PushdownRequest:
    """A task to be pushed down to a Worker for local execution.

    Attributes:
        task_id: Unique task identifier.
        code: Python source code executed on the worker (the "pushdown").
        inputs: Local data references (paths or URIs the worker resolves locally).
        returns: The name of the variable the code must assign as its result.
        timeout_seconds: Execution timeout on the worker.
        metadata: Optional routing hints (e.g. {"region": "mekong"}).
    """

    code: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    returns: str = "result"
    task_id: str = field(default_factory=lambda: f"task-{uuid.uuid4().hex[:12]}")
    timeout_seconds: float = 60.0
    metadata: dict[str, Any] = field(default_factory=dict)

    # ── 远端技能调用（RemoteWorker 用）─────────────────────────────────
    # CAFE 原本只有"下推代码"一种载荷（``code``）。远端 GeoNode 的接口是技能调用，
    # 不是"执行一段代码"，所以下推内容必须能表达两种形态。谁执行哪一种由 worker
    # **声明**（见 worker.capabilities()），router 在选点前先做兼容性硬过滤，
    # 不会把一个代码任务派给只会跑技能的远端。
    skill: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    geocards: list[str] = field(default_factory=list)
    spatial: dict[str, Any] | None = None
    temporal: dict[str, Any] | None = None

    @property
    def payload_kind(self) -> str:
        """这份下推要什么：``code``（本地沙箱跑代码）或 ``skill``（远端技能调用）。"""
        return "skill" if self.skill else "code"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "code": self.code,
            "inputs": self.inputs,
            "returns": self.returns,
            "timeout_seconds": self.timeout_seconds,
            "metadata": self.metadata,
            "skill": self.skill,
            "params": self.params,
            "geocards": self.geocards,
            "spatial": self.spatial,
            "temporal": self.temporal,
            "payload_kind": self.payload_kind,
        }


@dataclass
class PushdownResult:
    """Outcome of a pushdown task executed on a worker."""

    task_id: str
    status: TaskStatus
    result: Any = None
    error: str | None = None
    worker_id: str | None = None
    started_at: float = field(default_factory=time.time)
    elapsed_seconds: float = 0.0
    #: 远端返回的协议错误码（如 GeoMCP 的 2002），供上层做精细判断
    error_code: int | None = None
    #: 这个失败值不值得换一个 worker 重试：节点拒绝/连不上可以，输入缺失不行
    retryable: bool = False

    def __post_init__(self) -> None:
        """把 ``status`` 归一成 :class:`TaskStatus`。

        之前 ``CentralServer`` 的"没有 worker""worker 不存在"两条错误路径直接塞了
        字符串 ``"failed"``，而 :meth:`to_dict` 读 ``self.status.value``——于是
        **任何序列化这两个结果的调用都会 AttributeError**。归一化让这类错误在构造时
        就消失，而不是等某个下游去读 ``.value``。
        """
        if not isinstance(self.status, TaskStatus):
            self.status = TaskStatus(self.status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "worker_id": self.worker_id,
            "elapsed_seconds": round(self.elapsed_seconds, 4),
            "error_code": self.error_code,
            "retryable": self.retryable,
        }


#: Signature of a pushdown function: (inputs: dict) -> Any
PushdownFunction = Callable[[dict[str, Any]], Any]
