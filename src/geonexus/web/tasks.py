"""Asynchronous task management for the Web layer.

:class:`TaskManager` runs blocking SDK calls (``run_goal``, GeoMCP
``execute``, registry search) in a background thread pool so HTTP requests
return immediately with a ``task_id``; clients poll ``GET /api/tasks/{id}``
or stream progress over SSE (``GET /api/tasks/{id}/stream``).

The state machine is::

    queued -> running -> done | failed | cancelled

## 这是 GeoTask 的适配器视图

底层是 :mod:`geonexus.geotask`——**状态只有一份**（``TaskStore``），本模块负责在
边界做词表投影。Web 层用的是 ``done``，GeoTask 用的是 ``succeeded``；两者必须
分开，因为 `web` 的状态字符串是对外契约（16 条 HTTP 路由与前端都读它），而
GeoTask 的状态词是跨后端统一的内部契约。映射表见 :data:`WEB_TO_STATE` 与
:data:`STATE_TO_WEB`（对应 ``docs/GEOTASK.md`` §8.2）。

这样做的好处是 Web 层立刻获得了 GeoTask 的能力：可插拔持久化（内存/JSON）、
带 ``seq`` 的可重放事件、租约与重启对账，而**对外行为一字未改**。

## 一处必须记住的约束

``TaskManager.submit`` 接受**任意可调用对象**，而 ``GeoTask.spec`` 要能 JSON
序列化（要落盘、要进事件）。所以可调用对象与它的参数放在侧表 ``self._callables``
里，``spec`` 只留一个处理器名。把函数塞进 ``spec`` 会让 ``to_dict()`` 直接崩。
"""

from __future__ import annotations

import contextlib
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from inspect import signature
from typing import Any

from ..geotask import (
    GeoTask,
    IllegalTransition,
    LocalBackend,
    TaskError,
    TaskKind,
    TaskNotFound,
    TaskResult,
    TaskState,
    TaskStore,
)

# Task states (stable strings for the API surface).
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

_TERMINAL = {DONE, FAILED, CANCELLED}

#: 侧表里给"任意可调用对象"用的处理器名。
_CALLABLE_HANDLER = "__web_callable__"

#: ``TaskResult.outputs`` 里放可调用对象返回值的那把键。
_RESULT_KEY = "result"

#: Web 词表 → GeoTask 状态（契约 §8.2）。
WEB_TO_STATE: dict[str, TaskState] = {
    QUEUED: TaskState.QUEUED,
    RUNNING: TaskState.RUNNING,
    DONE: TaskState.SUCCEEDED,
    FAILED: TaskState.FAILED,
    CANCELLED: TaskState.CANCELLED,
}

#: GeoTask 状态 → Web 词表。Web 只有五个词，所以几个"尚未开始"的内部状态要
#: 合并：``created``/``scheduled``/``blocked`` 对外一律是 ``queued``（对调用方来说
#: 都是"还没跑"）。``skipped`` 投影成 ``cancelled`` 而不是 ``done``——它没有产出，
#: 说成功是撒谎。这些状态在本模块的使用场景（互不依赖的后台调用）里不会出现，
#: 保留只为让映射是全函数。
STATE_TO_WEB: dict[TaskState, str] = {
    TaskState.CREATED: QUEUED,
    TaskState.QUEUED: QUEUED,
    TaskState.SCHEDULED: QUEUED,
    TaskState.BLOCKED: QUEUED,
    TaskState.RUNNING: RUNNING,
    TaskState.SUCCEEDED: DONE,
    TaskState.FAILED: FAILED,
    TaskState.CANCELLED: CANCELLED,
    TaskState.SKIPPED: CANCELLED,
}


class TaskNotFoundError(Exception):
    """Raised when a task id is unknown."""


class TaskNotCancellableError(Exception):
    """Raised when a task is already terminal."""


@dataclass
class Task:
    """A tracked background execution.

    这是**投影目标**：由 :class:`GeoTask` 转换而来，字段与形状保持不变（对外契约）。
    """

    id: str
    status: str = QUEUED
    created_at: float = field(default_factory=lambda: time.time())
    started_at: float | None = None
    finished_at: float | None = None
    progress: float = 0.0  # 0..1
    message: str = ""
    result: Any = None
    error: str | None = None
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at) if self.started_at else None,
            "finished_at": _iso(self.finished_at) if self.finished_at else None,
            "progress": self.progress,
            "message": self.message,
            "cancelled": self.cancelled,
            "error": self.error,
            # Omit `result` here: it is included by `get()` on request so
            # list endpoints stay light.
        }


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def project(task: GeoTask) -> Task:
    """``GeoTask`` → Web 层视图（词表投影发生在这里）。"""
    result = None
    if task.result is not None:
        result = task.result.outputs.get(_RESULT_KEY, task.result.outputs)
    return Task(
        id=task.task_id,
        status=STATE_TO_WEB[task.state],
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
        progress=task.progress,
        message=task.message,
        result=result,
        error=task.error.message if task.error else None,
        cancelled=task.cancel_requested or task.state is TaskState.CANCELLED,
    )


class TaskManager:
    """Background execution backed by :class:`~geonexus.geotask.TaskStore`.

    Args:
        max_workers: 线程池大小；默认按当前线程数推导（与旧实现一致）。
        persist: 可选的持久化回调，每次状态/进度变化都会收到一份 **Web 视图**
            （即 ``Task.to_dict()``，终态含 ``result``）。回调抛异常不会影响任务。
    """

    def __init__(
        self,
        max_workers: int | None = None,
        persist: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        workers = max_workers or min(8, max(2, (threading.active_count() + 2)))
        self.store = TaskStore.memory()
        self._backend = LocalBackend(
            handlers={_CALLABLE_HANDLER: self._worker}, max_workers=workers
        )
        self._callables: dict[str, tuple[Callable[..., Any], tuple, dict]] = {}
        self._persist = persist
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # Submission
    # ------------------------------------------------------------------ #
    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        task_id: str | None = None,
        message: str = "",
        **kwargs: Any,
    ) -> str:
        """Schedule ``fn(*args, **kwargs)`` and return its task id.

        When ``fn`` declares a first parameter named ``task_id``, the task id
        is injected as the first positional argument — the canonical way for
        a worker to report progress via :meth:`update_progress` /
        :meth:`should_cancel`.
        """
        tid = task_id or uuid.uuid4().hex
        params = list(signature(fn).parameters)
        inject = bool(params and params[0] == "task_id")

        with self._lock:
            # 可调用对象进侧表，不进 spec —— spec 要能 JSON 序列化（落盘/事件）
            self._callables[tid] = (fn, (tid, *args) if inject else args, kwargs)
            task = GeoTask(
                task_id=tid,
                kind=TaskKind.SKILL,
                spec={"handler": _CALLABLE_HANDLER},
                description=message,
            )
            # put 是幂等的；状态变更走 store 的唯一入口
            self.store.put(task)
            self.store.transition(tid, TaskState.QUEUED)
            self._persist_view(tid)
            # 排队信息先落定，再交给线程池：否则一个极快的任务可能在
            # QUEUED 被记录之前就跑完了（旧实现用同一把锁保证这点）
            self._backend.submit(self.store.require(tid), self._context(tid))
        return tid

    def _context(self, task_id: str) -> Any:
        from ..geotask import ExecutionContext

        return ExecutionContext(
            task=self.store.require(task_id),
            _on_progress=lambda p, m: self.update_progress(task_id, p, m),
            _should_cancel=lambda: self.should_cancel(task_id),
        )

    def _worker(self, task: GeoTask, _ctx: Any) -> dict[str, Any]:
        """线程池里的执行体：驱动一次任务从 RUNNING 到终态。

        **签名是 ``(task, ctx)``**，不是 ``(task_id)``——执行后端的 ``Handler``
        协议如此（``LocalBackend._run`` 用两个参数调用）。旧 ``TaskManager`` 自己有
        "首参名 task_id 就注入"的约定，那是本类的 ``submit`` 做的，与后端无关；
        把两者混起来会让执行体永远不被正确调用（症状是任务卡在 queued）。
        """
        task_id = task.task_id
        with self._lock:
            entry = self._callables.get(task_id)
            if entry is None:
                return {}
            fn, args, kwargs = entry
            current = self.store.require(task_id).state
            if current in (TaskState.CANCELLED, TaskState.SUCCEEDED, TaskState.FAILED):
                # 排队期间已被取消：不要白跑（旧实现会白跑，测试允许两者）
                self._callables.pop(task_id, None)
                return {}
            for target in (TaskState.SCHEDULED, TaskState.RUNNING):
                if self.store.require(task_id).state is not target:
                    self.store.transition(task_id, target)
            self._persist_view(task_id)

        try:
            result = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the API
            with self._lock:
                self.store.transition(
                    task_id, TaskState.FAILED, error=TaskError.of("execution_error", str(exc))
                )
                self._persist_view(task_id)
            return {}
        finally:
            with self._lock:
                self._callables.pop(task_id, None)

        with self._lock:
            payload = TaskResult(outputs={_RESULT_KEY: result})
            if self.should_cancel(task_id):
                self.store.transition(task_id, TaskState.CANCELLED, result=payload)
            else:
                self.store.transition(task_id, TaskState.SUCCEEDED, result=payload)
            self._persist_view(task_id)
        return {}

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def get(self, task_id: str, include_result: bool = True) -> dict[str, Any]:
        task = self._require(task_id)
        view = project(task)
        data = view.to_dict()
        if include_result and view.status in (DONE, CANCELLED):
            data["result"] = view.result
        return data

    def list(self, include_result: bool = False) -> list[dict[str, Any]]:
        views = [project(t) for t in self.store.list(limit=10_000)]
        views.sort(key=lambda t: t.created_at, reverse=True)
        out = []
        for view in views:
            data = view.to_dict()
            if include_result and view.status in (DONE, CANCELLED):
                data["result"] = view.result
            out.append(data)
        return out

    def cancel(self, task_id: str) -> dict[str, Any]:
        """Request cancellation of a queued/running task.

        The running callable cooperates by checking
        :meth:`should_cancel` (the manager cannot interrupt a thread).
        """
        try:
            self.store.request_cancel(task_id)
        except TaskNotFound as exc:
            raise TaskNotFoundError(f"Unknown task: {task_id}") from exc
        except IllegalTransition as exc:
            current = STATE_TO_WEB[self.store.require(task_id).state]
            raise TaskNotCancellableError(f"Task {task_id} already {current}") from exc
        self._persist_view(task_id)
        return self.get(task_id, include_result=False)

    def should_cancel(self, task_id: str) -> bool:
        """Cooperatation hook for running callables."""
        task = self.store.get(task_id)
        return bool(task and task.cancel_requested)

    def update_progress(self, task_id: str, progress: float, message: str = "") -> None:
        """Allow a running callable to report progress (0..1).

        非 ``running`` 状态的上报会被忽略（旧实现如此）：否则一个迟到的上报会把
        终态的消息覆盖掉。
        """
        task = self.store.get(task_id)
        if task is None or task.state is not TaskState.RUNNING:
            return
        self.store.report_progress(task_id, progress, message)
        self._persist_view(task_id)

    def wait(self, task_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Block until the task reaches a terminal state."""
        self._require(task_id)
        deadline = None if timeout is None else time.time() + timeout
        while True:
            task = self.store.require(task_id)
            if STATE_TO_WEB[task.state] in _TERMINAL:
                return self.get(task_id)
            if deadline is not None and time.time() >= deadline:
                raise TimeoutError(f"Task {task_id} did not finish within {timeout}s")
            time.sleep(0.005)

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #
    def _require(self, task_id: str) -> GeoTask:
        task = self.store.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"Unknown task: {task_id}")
        return task

    def _persist_view(self, task_id: str) -> None:
        """把 **Web 视图**交给 persist 回调（终态含 result，与旧快照一致）。"""
        if self._persist is None:
            return
        # 持久化绝不能拖垮它正在报告的任务
        with contextlib.suppress(Exception):
            self._persist(self.get(task_id))

    def close(self) -> None:
        """Shut the worker pool down (waits for running tasks)."""
        self._backend.close()

    def __enter__(self) -> TaskManager:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


__all__ = [
    "QUEUED",
    "RUNNING",
    "DONE",
    "FAILED",
    "CANCELLED",
    "WEB_TO_STATE",
    "STATE_TO_WEB",
    "Task",
    "TaskManager",
    "TaskNotFoundError",
    "TaskNotCancellableError",
    "project",
]
