"""geonexus.geotask —— 统一任务模型与执行（契约见 ``docs/GEOTASK.md``）。

分层：

::

    model.py     数据：GeoTask / TaskState / 资源与重试策略
    machine.py   状态机：唯一的迁移关卡
    events.py    事件契约（带 seq，可重放）
    store.py     状态与事件的唯一入口（内存 + JSON）
    graph.py     依赖 DAG：**就绪集并行** + fail-fast 级联
    backend.py   执行后端协议（Phase 1：LocalBackend）
    runner.py    执行器：把就绪集真的并行跑起来

存在意义：把 SDK 里原先**四套互不相干的任务/状态概念**（``web.TaskManager``、
``cafe.PushdownRequest``、``agent.Plan``、``OgeTaskPoller``）收口成一套。收编与
迁移映射见契约 §8。
"""

from __future__ import annotations

from .backend import (
    BackendCapabilities,
    ExecutionBackend,
    ExecutionContext,
    Handle,
    LocalBackend,
    TaskOutcome,
)
from .events import (
    EVENT_TYPES,
    TASK_CANCELLED,
    TASK_CREATED,
    TASK_FAILED,
    TASK_OUTPUT,
    TASK_PROGRESS,
    TASK_STATE_CHANGED,
)
from .graph import TaskGraph, TaskGraphError
from .machine import (
    INITIAL_STATE,
    TERMINAL_STATES,
    TRANSITIONS,
    IllegalTransition,
    assert_transition,
    can_transition,
)
from .model import (
    ERROR_RETRYABLE,
    GeoTask,
    GpuRequirement,
    ResourceRequirement,
    RetryPolicy,
    TaskError,
    TaskIO,
    TaskKind,
    TaskResult,
    TaskState,
)
from .runner import DEFAULT_LEASE_SECONDS, RunReport, TaskRunner, make_local_runner
from .store import EventHistoryGone, JsonTaskStore, TaskNotFound, TaskStore

__all__ = [
    # 模型
    "GeoTask", "TaskKind", "TaskState", "TaskError", "TaskIO", "TaskResult",
    "RetryPolicy", "ResourceRequirement", "GpuRequirement", "ERROR_RETRYABLE",
    # 状态机
    "INITIAL_STATE", "TERMINAL_STATES", "TRANSITIONS", "IllegalTransition",
    "assert_transition", "can_transition",
    # 事件
    "EVENT_TYPES", "TASK_CREATED", "TASK_STATE_CHANGED", "TASK_PROGRESS",
    "TASK_OUTPUT", "TASK_FAILED", "TASK_CANCELLED",
    # 存储
    "TaskStore", "JsonTaskStore", "TaskNotFound", "EventHistoryGone",
    # 图
    "TaskGraph", "TaskGraphError",
    # 执行
    "ExecutionBackend", "ExecutionContext", "BackendCapabilities",
    "TaskOutcome", "Handle", "LocalBackend",
    "TaskRunner", "RunReport", "make_local_runner", "DEFAULT_LEASE_SECONDS",
]
