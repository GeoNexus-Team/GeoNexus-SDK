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
from dataclasses import dataclass, field
from typing import Any

from .backend import ExecutionBackend, ExecutionContext, LocalBackend, TaskOutcome
from .graph import TaskGraph
from .model import TaskResult, TaskState
from .scheduler import Scheduler
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
    #: 收工时仍被资源卡在 ``blocked`` 的任务数（>0 表示这一轮没跑完）
    blocked: int = 0
    #: 因"需求超过节点总容量"被**拒绝**的任务数（不可重试的部署错误）
    rejected: int = 0
    max_parallel: int = 1
    elapsed_seconds: float = 0.0
    #: 调度观测快照（容量/占用/峰值/卡住理由）。没有调度器时为空。
    occupancy: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "skipped": self.skipped,
            "blocked": self.blocked,
            "rejected": self.rejected,
            "max_parallel": self.max_parallel,
            "elapsed_seconds": round(self.elapsed_seconds, 4),
            "occupancy": self.occupancy,
        }


class TaskRunner:
    """驱动一个 :class:`TaskGraph` 直到它全部进入终态。"""

    def __init__(
        self,
        store: TaskStore,
        backend: ExecutionBackend,
        *,
        executor_id: str = "local",
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        scheduler: Scheduler | None = None,
    ) -> None:
        self.store = store
        self.backend = backend
        self.executor_id = executor_id
        self.lease_seconds = lease_seconds
        #: 没有调度器就是"有不限量的容量"——P1 的行为，保持向后兼容
        self.scheduler = scheduler

    # ------------------------------------------------------------------ #
    def run(self, graph: TaskGraph, *, should_stop: Callable[[], bool] | None = None) -> RunReport:
        # 状态只有一份：把图节点换成 store 里的对象（store 里缺的会被登记）
        graph.bind(self.store)

        started = time.time()
        report = RunReport(total=len(graph.topological_order()))
        handles: dict[str, Any] = {}
        in_flight: set[str] = set()

        while True:
            if should_stop is not None and should_stop():
                break

            # 1. 一轮能起多少起多少 —— 这就是并行（有调度器时先过准入）
            for task_id in self._admit(graph, report):
                task = graph.task(task_id)
                if task.cancel_requested:
                    graph.mark_cancelled(task_id)
                    self._release(task_id)
                    continue
                self.store.set_lease(
                    task_id, expires_at=time.time() + self.lease_seconds, executor=self.executor_id
                )
                ctx = self._context(task_id)
                try:
                    handles[task_id] = self.backend.submit(task, ctx)
                except Exception as exc:  # noqa: BLE001 - 故障隔离：一个任务炸掉不拖垮整张图
                    self._release(task_id)
                    self._apply(
                        graph,
                        task_id,
                        TaskOutcome.failure(
                            "execution_error", f"提交失败: {type(exc).__name__}: {exc}"
                        ),
                    )
                    continue
                graph.mark_running(task_id)  # 立刻标 RUNNING，否则会被重复提交
                in_flight.add(task_id)

            report.max_parallel = max(report.max_parallel, len(in_flight))
            if self.scheduler is not None:
                self.scheduler.note_peak()

            # 2. 没有在跑：要么真的没得跑，要么还在退避窗口里——后者睡醒后**回到准入**。
            #    第一版这里直接往下走，于是"等一个完成"拿到空集合就收工，
            #    重试中的任务永远留在 blocked（测试 test_retry_* 抓到的）。
            if not in_flight:
                if self._wait_for_backoff():
                    continue
                break

            # 3. 等**一个**完成就回去重算就绪集（不是等全部完成）
            done_id = self._wait_any(handles, in_flight)
            if done_id is None:
                break
            outcome = self._poll(handles.pop(done_id))
            in_flight.discard(done_id)
            self._release(done_id)
            self._apply(graph, done_id, outcome)

        self._summarize(graph, report)
        report.elapsed_seconds = time.time() - started
        return report

    def run_tasks(self, tasks: list[Any], **kwargs: Any) -> RunReport:
        """便捷入口：直接给一批 ``GeoTask``。"""
        return self.run(TaskGraph.from_tasks(tasks), **kwargs)

    # ------------------------------------------------------------------ #
    def _admit(self, graph: TaskGraph, report: RunReport) -> list[str]:
        """取这一轮真正要提交的任务。

        没有调度器：原样返回就绪集（P1 语义）。有调度器：过准入，把"装不下"的
        直接拒绝、把"暂时没空"的标成 ``blocked`` 并写清理由。
        """
        ready = graph.ready()
        if self.scheduler is None:
            return ready

        result = self.scheduler.admit([graph.task(tid) for tid in ready])
        for task_id, reasons in result.rejected.items():
            code = result.reasons.get(task_id, "no_compatible_compute")
            graph.mark_failed(task_id, code, "；".join(reasons))
            self.scheduler.forget(task_id)
            report.rejected += 1
        for task_id, reasons in result.blocked.items():
            graph.mark_blocked(
                task_id, reasons, code=result.reasons.get(task_id, "resource_unavailable")
            )
        return result.admitted

    def _release(self, task_id: str) -> None:
        """任务离开执行态：**必须**释放预约，否则容量会慢慢泄漏到 0。"""
        if self.scheduler is not None:
            self.scheduler.release(task_id)

    def _wait_for_backoff(self, cap: float = 5.0) -> bool:
        """睡到最近一个退避任务解禁。返回 ``False`` 表示没有人在等。"""
        if self.scheduler is None:
            return False
        delay = self.scheduler.next_wakeup()
        if delay is None:
            return False
        time.sleep(min(max(delay, 0.0), cap))
        return True

    def _context(self, task_id: str) -> ExecutionContext:
        return ExecutionContext(
            task=self.store.require(task_id),
            _on_progress=lambda p, m: self.store.report_progress(task_id, p, m),
            _should_cancel=lambda: bool((t := self.store.get(task_id)) and t.cancel_requested),
        )

    def _poll(self, handle: Any) -> TaskOutcome:
        """轮询**绝不抛异常**。

        后端连不上是这一轮的常见故障，不能让它把整张图带走——``_wait_any`` 也走
        这里，所以"等一个完成"的过程同样免疫。错误码用 ``executor_lost``
        （可重试），于是 ``RetryPolicy`` 仍然有机会救它。
        """
        try:
            return self.backend.poll(handle)
        except Exception as exc:  # noqa: BLE001 - 见 docstring
            return TaskOutcome.failure("executor_lost", f"轮询失败: {type(exc).__name__}: {exc}")

    def _wait_any(
        self, handles: dict[str, Any], in_flight: set[str], poll_interval: float = 0.005
    ) -> str | None:
        """等到**任意一个**在跑的任务完成，返回它的 id。

        逐个轮询而不是 ``concurrent.futures.wait``，是为了保持 ``ExecutionBackend``
        接口的通用性（远端/批作业后端没有 future 可等）。
        """
        while in_flight:
            for task_id in list(in_flight):
                outcome = self._poll(handles[task_id])
                if outcome.state is not TaskState.RUNNING:
                    return task_id
            time.sleep(poll_interval)
        return None

    def _apply(self, graph: TaskGraph, task_id: str, outcome: TaskOutcome) -> None:
        if outcome.state is TaskState.SUCCEEDED:
            graph.mark_done(
                task_id,
                result=TaskResult(outputs=dict(outcome.outputs), meta={"message": outcome.message}),
            )
            return
        if outcome.state is TaskState.CANCELLED:
            graph.mark_cancelled(task_id)
            return

        error = outcome.error
        task = graph.task(task_id)
        if (
            error is not None
            and error.retryable
            and task.retry.allows_another_attempt(task.attempt)
        ):
            task.attempt += 1
            graph.mark_retry(task_id)  # running → queued，重新进就绪集
            self.store.set_lease(task_id, expires_at=0.0)  # 交回调度器
            if self.scheduler is not None:
                # 退避：不是"立刻再抢一次"，那会把一次瞬时故障放大成忙等
                self.scheduler.defer(task_id, task.retry.backoff_for(task.attempt - 1))
            return
        graph.mark_failed(
            task_id,
            code=error.code if error else "execution_error",
            message=error.message if error else "",
        )
        if self.scheduler is not None:
            self.scheduler.forget(task_id)

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
            elif state is TaskState.BLOCKED:
                # 收工时还被资源卡着 = 这一轮没跑完（容量不足或退避未到）
                report.blocked += 1
        if self.scheduler is not None:
            report.occupancy = self.scheduler.stats()

    # ------------------------------------------------------------------ #
    def scheduling(self) -> dict[str, Any]:
        """观测面：容量 / 占用 / 峰值 / 卡住理由。没有调度器时为空。"""
        return self.scheduler.stats() if self.scheduler is not None else {}


def make_local_runner(
    store: TaskStore | None = None,
    handlers: dict[str, Any] | None = None,
    max_workers: int = 4,
    scheduler: Scheduler | None = None,
) -> tuple[TaskRunner, LocalBackend]:
    """默认装配：内存 store + 本地线程池后端（+ 可选调度器）。"""
    store = store or TaskStore.memory()
    backend = LocalBackend(handlers=handlers, max_workers=max_workers)
    return TaskRunner(store, backend, scheduler=scheduler), backend
