"""任务执行器 —— 把"就绪集"真的并行跑起来。

这是路线图 Phase 1 的核心验收点：``agent.PlanExecutor`` 现在用
``for step_id in order:`` 串行执行，DAG 里可并行的部分被浪费。这里的循环是：

1. ``graph.ready()`` 取**所有**可跑节点；
2. 全部提交（线程池里并发跑），并立刻 ``mark_running`` 防止重复提交；
3. 等**最早**完成的那个回来，落状态、触发 fail-fast 级联；
4. 回到 1 —— 依赖刚满足的节点这一轮就会被带进来。

状态只有一份：``run()`` 开头 ``graph.bind(store)``，此后所有迁移经由 store
（校验与事件统一产生）。取消是**协作式**的：请求取消只置标志，执行体在步骤边界
自行结束后状态才落 ``cancelled``。这也是 ``LocalBackend.cancel()`` 返回 ``False``
的原因——它没有能力撤销一个正在跑的线程，假装成功会让上层以为停住了。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .backend import ExecutionBackend, ExecutionContext, LocalBackend, TaskOutcome
from .graph import TaskGraph
from .model import TaskResult, TaskState
from .store import TaskStore

__all__ = ["TaskRunner", "RunReport", "DEFAULT_LEASE_SECONDS"]

#: 租约时长。执行体在跑就说明有主，超过这个时间没续租才算"执行者丢了"。
DEFAULT_LEASE_SECONDS = 60.0


@dataclass
class RunReport:
    """一次图执行的结果摘要（给调用方与测试断言用）。"""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    skipped: int = 0
    max_parallel: int = 1
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total, "succeeded": self.succeeded, "failed": self.failed,
            "cancelled": self.cancelled, "skipped": self.skipped,
            "max_parallel": self.max_parallel,
            "elapsed_seconds": round(self.elapsed_seconds, 4),
        }


class TaskRunner:
    """驱动一个 :class:`TaskGraph` 直到它全部进入终态。"""

    def __init__(self, store: TaskStore, backend: ExecutionBackend,
                 *, executor_id: str = "local",
                 lease_seconds: float = DEFAULT_LEASE_SECONDS) -> None:
        self.store = store
        self.backend = backend
        self.executor_id = executor_id
        self.lease_seconds = lease_seconds

    # ------------------------------------------------------------------ #
    def run(self, graph: TaskGraph, *,
            should_stop: Callable[[], bool] | None = None) -> RunReport:
        # 状态只有一份：把图节点换成 store 里的对象（store 里缺的会被登记）
        graph.bind(self.store)

        started = time.time()
        report = RunReport(total=len(graph.topological_order()))
        handles: dict[str, Any] = {}
        in_flight: set[str] = set()

        while True:
            if should_stop is not None and should_stop():
                break

            # 1. 一轮能起多少起多少 —— 这就是并行
            for task_id in graph.ready():
                task = graph.task(task_id)
                if task.cancel_requested:
                    graph.mark_cancelled(task_id)
                    continue
                self.store.set_lease(
                    task_id, expires_at=time.time() + self.lease_seconds,
                    executor=self.executor_id)
                ctx = self._context(task_id)
                handles[task_id] = self.backend.submit(task, ctx)
                graph.mark_running(task_id)   # 立刻标 RUNNING，否则会被重复提交
                in_flight.add(task_id)

            report.max_parallel = max(report.max_parallel, len(in_flight))

            # 2. 没有在跑也没有可跑的 → 收工
            if not in_flight:
                break

            # 3. 等**一个**完成就回去重算就绪集（不是等全部完成）
            done_id = self._wait_any(handles, in_flight)
            if done_id is None:
                break
            outcome = self.backend.poll(handles.pop(done_id))
            in_flight.discard(done_id)
            self._apply(graph, done_id, outcome)

        self._summarize(graph, report)
        report.elapsed_seconds = time.time() - started
        return report

    def run_tasks(self, tasks: list[Any], **kwargs: Any) -> RunReport:
        """便捷入口：直接给一批 ``GeoTask``。"""
        return self.run(TaskGraph.from_tasks(tasks), **kwargs)

    # ------------------------------------------------------------------ #
    def _context(self, task_id: str) -> ExecutionContext:
        return ExecutionContext(
            task=self.store.require(task_id),
            _on_progress=lambda p, m: self.store.report_progress(task_id, p, m),
            _should_cancel=lambda: bool(
                (t := self.store.get(task_id)) and t.cancel_requested),
        )

    def _wait_any(self, handles: dict[str, Any], in_flight: set[str],
                  poll_interval: float = 0.005) -> str | None:
        """等到**任意一个**在跑的任务完成，返回它的 id。

        逐个轮询而不是 ``concurrent.futures.wait``，是为了保持 ``ExecutionBackend``
        接口的通用性（远端/批作业后端没有 future 可等）。
        """
        while in_flight:
            for task_id in list(in_flight):
                outcome = self.backend.poll(handles[task_id])
                if outcome.state is not TaskState.RUNNING:
                    return task_id
            time.sleep(poll_interval)
        return None

    def _apply(self, graph: TaskGraph, task_id: str, outcome: TaskOutcome) -> None:
        if outcome.state is TaskState.SUCCEEDED:
            graph.mark_done(task_id, result=TaskResult(
                outputs=dict(outcome.outputs), meta={"message": outcome.message}))
            return
        if outcome.state is TaskState.CANCELLED:
            graph.mark_cancelled(task_id)
            return

        error = outcome.error
        task = graph.task(task_id)
        if error is not None and error.retryable and \
                task.retry.allows_another_attempt(task.attempt):
            task.attempt += 1
            graph.mark_retry(task_id)          # running → queued，重新进就绪集
            self.store.set_lease(task_id, expires_at=0.0)   # 交回调度器
            return
        graph.mark_failed(task_id,
                          code=error.code if error else "execution_error",
                          message=error.message if error else "")

    def _summarize(self, graph: TaskGraph, report: RunReport) -> None:
        for task_id in graph.topological_order():
            state = graph.state_of(task_id)
            if state is TaskState.SUCCEEDED:
                report.succeeded += 1
            elif state is TaskState.FAILED:
                report.failed += 1
            elif state is TaskState.CANCELLED:
                report.cancelled += 1
            elif state is TaskState.SKIPPED:
                report.skipped += 1


def make_local_runner(store: TaskStore | None = None,
                      handlers: dict[str, Any] | None = None,
                      max_workers: int = 4) -> tuple[TaskRunner, LocalBackend]:
    """Phase 1 的默认装配：内存 store + 本地线程池后端。"""
    store = store or TaskStore.memory()
    backend = LocalBackend(handlers=handlers, max_workers=max_workers)
    return TaskRunner(store, backend), backend
