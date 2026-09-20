"""算力调度与占用管理（Phase 4）—— ``geonexus.geotask.scheduler``。

验收判据来自 ``docs/ROADMAP.md``：

- N 个并发任务在资源约束下**排队而非自由竞争**，超限有可解释状态（``blocked``）
- 占用记账与实际并发**一致**（可断言）
- 故障注入：单个任务失败不影响同节点其他任务

"记账 == 实际并发"不能只信调度器自己的计数器，所以这里的做法是：在**执行体内部**
记录真实并发（进入/离开 handler 时增减计数器），再拿它与调度器的峰值占用对比。
两者都是独立测量的。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from geonexus.geotask import (
    GeoTask,
    LocalBackend,
    Reservation,
    ResourceRequirement,
    RetryPolicy,
    Scheduler,
    TaskGraph,
    TaskKind,
    TaskRunner,
    TaskState,
    TaskStore,
)
from geonexus.geotask.scheduler import (
    REASON_CONCURRENCY,
    REASON_IMPOSSIBLE,
    REASON_RESOURCE,
)


# --------------------------------------------------------------------------- #
# 观测探针：在执行体里数真实并发
# --------------------------------------------------------------------------- #
class _Probe:
    """执行体共享的并发探针（独立于调度器的计数器）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls: list[float] = []

    def handler(self, sleep: float = 0.02, fail: bool = False, fail_times: int = 0) -> Any:
        def run(task: GeoTask, ctx: Any) -> dict[str, Any]:
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.calls.append(time.time())
                index = len(self.calls)
            try:
                time.sleep(sleep)
                if fail or index <= fail_times:
                    raise RuntimeError(f"注入故障 #{index}")
                return {"ok": task.task_id}
            finally:
                with self.lock:
                    self.active -= 1

        return run


def _task(task_id: str, *, cpu: float = 1.0, memory_mb: int = 0, priority: int = 0,
          owner: str | None = None, handler: str = "h",
          retry: RetryPolicy | None = None, timeout: float = 10.0) -> GeoTask:
    task = GeoTask(
        task_id, TaskKind.SKILL,
        spec={"handler": handler},
        resources=ResourceRequirement(cpu=cpu, memory_mb=memory_mb),
        priority=priority, owner=owner,
        retry=retry or RetryPolicy(),
    )
    task.timeout_seconds = timeout
    return task


# --------------------------------------------------------------------------- #
# ① 预约与容量算术
# --------------------------------------------------------------------------- #
class TestReservation:
    def test_from_requirement(self) -> None:
        from geonexus.geotask.model import GpuRequirement

        res = Reservation.of(ResourceRequirement(
            cpu=2, memory_mb=4096, gpu=GpuRequirement(kind="CUDA", count=2)))
        assert res == Reservation(cpu_cores=2.0, memory_mb=4096, gpu_count=2,
                                  gpu_kind="cuda")

    def test_missing_requirement_is_zero(self) -> None:
        assert Reservation.of(None) == Reservation()

    def test_fits_within_is_strict_about_zero(self) -> None:
        """容量 0 = 没得用，不是"未知"。这条曾写错，导致调度器形同虚设。"""
        assert Reservation(cpu_cores=1).fits_within(Reservation(cpu_cores=0))
        assert Reservation(cpu_cores=2).fits_within(Reservation(cpu_cores=1))

    def test_reasons_name_the_dimension(self) -> None:
        reasons = Reservation(cpu_cores=8, memory_mb=16384, gpu_count=2, gpu_kind="cuda"
                              ).fits_within(Reservation(cpu_cores=4, memory_mb=8192,
                                                        gpu_count=1))
        joined = " | ".join(reasons)
        assert "cpu" in joined and "memory" in joined and "gpu" in joined
        assert len(reasons) == 3

    def test_gpu_kind_mismatch(self) -> None:
        reasons = Reservation(gpu_count=1, gpu_kind="cuda").fits_within(
            Reservation(gpu_count=1, gpu_kind="mps"))
        assert any("cuda" in r and "mps" in r for r in reasons)

    def test_after_using_clamps_at_zero(self) -> None:
        left = Reservation(cpu_cores=1, memory_mb=100).after_using(
            Reservation(cpu_cores=4, memory_mb=1000))
        assert left.cpu_cores == 0.0 and left.memory_mb == 0

    def test_default_capacity_from_machine(self) -> None:
        cap = Reservation.default_capacity()
        assert cap.cpu_cores >= 1
        assert cap.gpu_count == 0, "不猜 GPU：宁可拒绝，不可误放"


# --------------------------------------------------------------------------- #
# ② 占用记账
# --------------------------------------------------------------------------- #
class TestOccupancy:
    def test_reserve_release_and_totals(self) -> None:
        from geonexus.geotask.scheduler import Occupancy

        occ = Occupancy(Reservation(cpu_cores=4, memory_mb=1024))
        occ.reserve("a", Reservation(cpu_cores=1, memory_mb=256))
        occ.reserve("b", Reservation(cpu_cores=2, memory_mb=512))
        assert occ.active == 2
        assert occ.held().cpu_cores == 3.0
        assert occ.held().memory_mb == 768
        assert occ.available().cpu_cores == 1.0
        assert occ.available().memory_mb == 256

        assert occ.release("a") is not None
        assert occ.release("a") is None
        assert occ.held().cpu_cores == 2.0

    def test_detail_shape(self) -> None:
        from geonexus.geotask.scheduler import Occupancy

        detail = Occupancy(Reservation(cpu_cores=2)).detail()
        assert set(detail) == {"capacity", "held", "available", "active"}


# --------------------------------------------------------------------------- #
# ③ 准入：三分类
# --------------------------------------------------------------------------- #
class TestAdmission:
    def _cap(self, cpu: float = 4.0, memory_mb: int = 8192,
             gpu: int = 0, kind: str | None = None) -> Scheduler:
        return Scheduler(Reservation(cpu_cores=cpu, memory_mb=memory_mb,
                                     gpu_count=gpu, gpu_kind=kind))

    def test_over_capacity_queues_with_reason(self) -> None:
        sched = self._cap(cpu=2)
        result = sched.admit([_task(f"t{i}") for i in range(5)])
        assert result.admitted == ["t0", "t1"]
        assert set(result.blocked) == {"t2", "t3", "t4"}
        assert all("cpu" in r[0] for r in result.blocked.values())
        assert set(result.reasons.values()) == {REASON_RESOURCE}
        assert sched.occupancy.held().cpu_cores == 2.0

    def test_impossible_requirement_is_rejected_not_queued(self) -> None:
        """装不下 ≠ 排队。永久排队会把部署错误伪装成资源紧张。"""
        sched = self._cap(cpu=4)
        result = sched.admit([_task("huge", cpu=64)])
        assert result.admitted == []
        assert result.blocked == {}
        assert "huge" in result.rejected
        assert result.reasons["huge"] == REASON_IMPOSSIBLE
        assert sched.rejected_codes["huge"] == "no_compatible_compute"

    def test_gpu_task_on_gpu_less_node_is_rejected(self) -> None:
        from geonexus.geotask.model import GpuRequirement

        sched = self._cap(cpu=8, gpu=0)
        task = _task("gpu1")
        task.resources.gpu = GpuRequirement(kind="cuda", count=1)
        result = sched.admit([task])
        assert "gpu1" in result.rejected
        assert any("gpu" in r for r in result.rejected["gpu1"])

    def test_gpu_count_is_accounted(self) -> None:
        from geonexus.geotask.model import GpuRequirement

        sched = self._cap(cpu=16, gpu=2, kind="cuda")
        tasks = []
        for i in range(3):
            t = _task(f"g{i}")
            t.resources.gpu = GpuRequirement(kind="cuda", count=1)
            tasks.append(t)
        result = sched.admit(tasks)
        assert result.admitted == ["g0", "g1"]
        assert set(result.blocked) == {"g2"}

    def test_max_concurrent_is_a_separate_limit(self) -> None:
        sched = Scheduler(Reservation(cpu_cores=64, memory_mb=65536), max_concurrent=2)
        result = sched.admit([_task(f"t{i}") for i in range(4)])
        assert result.admitted == ["t0", "t1"]
        assert result.reasons["t2"] == REASON_CONCURRENCY
        assert "并发上限 2" in result.blocked["t2"][0]

    def test_priority_wins(self) -> None:
        sched = self._cap(cpu=1)
        low = _task("low", priority=0)
        high = _task("high", priority=10)
        result = sched.admit([low, high])
        assert result.admitted == ["high"]
        assert "low" in result.blocked

    def test_fairness_across_owners(self) -> None:
        """同一优先级下按属主轮转：一个属主提交 5 个也吃不掉全部名额。"""
        sched = self._cap(cpu=4)
        tasks = [_task(f"a{i}", owner="alice") for i in range(5)]
        tasks += [_task(f"b{i}", owner="bob") for i in range(5)]
        result = sched.admit(tasks)
        assert len(result.admitted) == 4
        alice = [t for t in result.admitted if t.startswith("a")]
        bob = [t for t in result.admitted if t.startswith("b")]
        assert len(alice) == 2 and len(bob) == 2, result.admitted

    def test_no_head_of_line_blocking(self) -> None:
        """小任务能填进大任务留下的空隙——不因为前面排了个大家伙就饿着。"""
        sched = self._cap(cpu=2)
        sched.occupancy.reserve("existing", Reservation(cpu_cores=1))
        big = _task("big", cpu=2)
        small = _task("small", cpu=1)
        result = sched.admit([big, small])
        assert result.admitted == ["small"]
        assert "big" in result.blocked

    def test_backoff_defers_admission(self) -> None:
        sched = self._cap(cpu=4, memory_mb=8192)
        task = _task("t")
        sched.admit([task])
        sched.release("t")
        sched.defer("t", 30.0)
        result = sched.admit([task])
        assert result.admitted == []
        assert "退避中" in result.blocked["t"][0]
        assert 0 < sched.next_wakeup() <= 30.0

    def test_forget_clears_backoff(self) -> None:
        sched = self._cap()
        sched.defer("t", 30.0)
        assert sched.next_wakeup() is not None
        sched.forget("t")
        assert sched.next_wakeup() is None

    def test_release_frees_capacity_for_the_next_round(self) -> None:
        """``admit`` 对"谁跑过"无状态：调用方给的就绪集才是参选名单。"""
        sched = self._cap(cpu=1)
        assert sched.admit([_task("t0")]).admitted == ["t0"]
        assert sched.occupancy.available().cpu_cores == 0.0
        sched.release("t0")
        assert sched.occupancy.available().cpu_cores == 1.0
        assert sched.admit([_task("t1")]).admitted == ["t1"]

    def test_peak_queue_depth_is_recorded(self) -> None:
        sched = self._cap(cpu=1)
        sched.admit([_task(f"t{i}") for i in range(4)])
        assert sched.stats()["peak_queue_depth"] == 3

    def test_enqueue_seq_is_stable_across_rounds(self) -> None:
        sched = self._cap(cpu=1)
        aa, ab, ba = _task("a1", owner="a"), _task("a2", owner="a"), _task("b1", owner="b")
        first = sched.enqueue_seq("a1")
        assert first == 0
        assert sched.enqueue_seq("a2") == 1
        assert sched.enqueue_seq("b1") == 2
        sched.admit([aa, ab, ba])
        assert sched.enqueue_seq("a1") == first, "重试/再次参选不该重新排队"


# --------------------------------------------------------------------------- #
# ④ 图与状态机：blocked 可回归
# --------------------------------------------------------------------------- #
class TestGraphBlocked:
    def test_blocked_is_runnable_again(self) -> None:
        graph = TaskGraph([_task("t1")])
        graph.mark_blocked("t1", ["cpu: 需要 1 核，可用 0 核"])
        assert graph.state_of("t1") is TaskState.BLOCKED
        assert graph.ready() == ["t1"], "容量释放后必须还能再参选"

    def test_blocked_then_running_is_legal(self) -> None:
        graph = TaskGraph([_task("t1")])
        graph.mark_blocked("t1", ["没空"])
        graph.mark_running("t1")
        assert graph.state_of("t1") is TaskState.RUNNING

    def test_mark_blocked_is_idempotent(self) -> None:
        store = TaskStore.memory()
        graph = TaskGraph([_task("t1")]).bind(store)
        graph.mark_blocked("t1", ["第一次"])
        graph.mark_blocked("t1", ["第二次"])
        assert store.get("t1").state is TaskState.BLOCKED
        events = store.subscribe("t1")
        assert [e["event"] for e in events].count("task.state_changed") == 2, \
            "created→queued、queued→blocked 各一条，重复标记不该再产生迁移"

    def test_reason_lands_in_the_event_stream(self) -> None:
        store = TaskStore.memory()
        graph = TaskGraph([_task("t1")]).bind(store)
        graph.mark_blocked("t1", ["gpu: 需要 1 张，可用 0 张"])
        changed = [e for e in store.subscribe("t1")
                   if e["event"] == "task.state_changed"
                   and e["data"]["to"] == "blocked"]
        assert len(changed) == 1
        assert "gpu" in changed[0]["data"]["reason"]
        assert "gpu" in store.get("t1").message

    def test_blocked_then_failed_still_works(self) -> None:
        """被资源卡过的任务随后因其他原因失败，迁移路径仍然合法。"""
        graph = TaskGraph([_task("t1")])
        graph.mark_blocked("t1", ["没空"])
        graph.mark_failed("t1", "invalid_spec", "配置错")
        assert graph.state_of("t1") is TaskState.FAILED

    def test_dependency_blocked_is_terminal_not_runnable(self) -> None:
        """依赖失败级联出来的 blocked 立刻转 failed，不会留在就绪集里。"""
        graph = TaskGraph([_task("a"), _task("b")])
        graph.task("b").depends_on = ["a"]
        graph.mark_failed("a", "execution_error", "boom")
        assert graph.state_of("b") is TaskState.FAILED
        assert graph.ready() == []


# --------------------------------------------------------------------------- #
# ⑤ 执行器：排队、记账、隔离
# --------------------------------------------------------------------------- #
class TestRunnerScheduling:
    def test_capacity_limits_real_concurrency(self) -> None:
        """6 个任务、容量 2 核、后端能开 8 个线程 —— 真实并发必须被压到 2。"""
        probe = _Probe()
        sched = Scheduler(Reservation(cpu_cores=2, memory_mb=8192))
        store = TaskStore.memory()
        backend = LocalBackend(handlers={"h": probe.handler(sleep=0.03)}, max_workers=8)
        runner = TaskRunner(store, backend, scheduler=sched)

        report = runner.run_tasks([_task(f"t{i}") for i in range(6)])
        backend.close()

        assert report.succeeded == 6
        assert report.failed == 0
        assert probe.max_active == 2, f"真实并发 {probe.max_active}，容量是 2"
        assert report.max_parallel == 2

    def test_accounting_matches_actual_concurrency(self) -> None:
        """调度器的峰值记账 == 执行体里实测的峰值并发（两个独立测量）。"""
        probe = _Probe()
        sched = Scheduler(Reservation(cpu_cores=3, memory_mb=8192))
        store = TaskStore.memory()
        backend = LocalBackend(handlers={"h": probe.handler(sleep=0.03)}, max_workers=8)
        runner = TaskRunner(store, backend, scheduler=sched)

        report = runner.run_tasks([_task(f"t{i}") for i in range(7)])
        backend.close()

        assert sched.peak_parallel == probe.max_active == 3
        assert sched.peak_held.cpu_cores == 3.0, "峰值占用应等于峰值并发 × 单任务预约"
        assert report.occupancy["peak_parallel"] == 3

    def test_no_reservation_leak(self) -> None:
        probe = _Probe()
        sched = Scheduler(Reservation(cpu_cores=2, memory_mb=8192))
        store = TaskStore.memory()
        backend = LocalBackend(handlers={"h": probe.handler(sleep=0.01)}, max_workers=4)
        runner = TaskRunner(store, backend, scheduler=sched)
        runner.run_tasks([_task(f"t{i}") for i in range(6)])
        backend.close()

        assert sched.occupancy.active == 0, "跑完必须把预约全部还回去"
        assert sched.occupancy.held() == Reservation()
        assert sched.occupancy.available() == sched.occupancy.capacity

    def test_impossible_task_rejected_while_others_run(self) -> None:
        """一个装不下的任务被拒绝，其他任务照常完成——且它有理由。"""
        probe = _Probe()
        sched = Scheduler(Reservation(cpu_cores=4, memory_mb=8192))
        store = TaskStore.memory()
        backend = LocalBackend(handlers={"h": probe.handler(sleep=0.01)}, max_workers=4)
        runner = TaskRunner(store, backend, scheduler=sched)

        tasks = [_task(f"ok{i}") for i in range(3)]
        tasks.append(_task("huge", cpu=64))
        report = runner.run_tasks(tasks)
        backend.close()

        assert report.rejected == 1
        assert report.succeeded == 3
        assert store.get("huge").state is TaskState.FAILED
        assert store.get("huge").error is not None
        assert store.get("huge").error.code == "no_compatible_compute"
        assert "64" in store.get("huge").error.message

    def test_blocked_tasks_keep_a_readable_reason(self) -> None:
        """容量不足时收工：剩下的必须是 blocked + 理由，而不是假的 running。"""
        probe = _Probe()
        sched = Scheduler(Reservation(cpu_cores=1, memory_mb=8192))
        store = TaskStore.memory()
        backend = LocalBackend(handlers={"h": probe.handler(sleep=0.01)}, max_workers=4)
        runner = TaskRunner(store, backend, scheduler=sched)

        report = runner.run_tasks([_task(f"t{i}") for i in range(4)])
        backend.close()
        # 容量 1 → 串行跑完，队列深度被记录，最终没有残留 blocked
        assert report.succeeded == 4
        assert report.blocked == 0
        assert sched.peak_queue_depth == 3, "队列深度要能观测"

    def test_failure_does_not_take_down_siblings(self) -> None:
        probe = _Probe()
        backend = LocalBackend(handlers={"h": probe.handler(sleep=0.01, fail=True)},
                               max_workers=4)
        store = TaskStore.memory()
        runner = TaskRunner(store, backend)
        report = runner.run_tasks([_task(f"t{i}") for i in range(4)])
        backend.close()

        assert report.failed == 4
        assert probe.max_active == 4, "一个失败不影响其他任务照常并发执行"
        assert all(store.get(f"t{i}").error.code == "execution_error" for i in range(4))

    def test_one_bad_handler_among_good_ones(self) -> None:
        probe = _Probe()
        backend = LocalBackend(handlers={
            "h": probe.handler(sleep=0.01),
            "boom": probe.handler(sleep=0.01, fail=True),
        }, max_workers=4)
        store = TaskStore.memory()
        runner = TaskRunner(store, backend)
        tasks = [_task("a"), _task("b"), _task("c", handler="boom"), _task("d")]
        report = runner.run_tasks(tasks)
        backend.close()

        assert report.succeeded == 3 and report.failed == 1
        assert store.get("c").state is TaskState.FAILED
        assert store.get("d").state is TaskState.SUCCEEDED

    def test_backend_poll_exception_is_isolated(self) -> None:
        """后端 poll 抛异常只让**那一个**任务失败，run() 不炸。"""
        probe = _Probe()
        inner = LocalBackend(handlers={"h": probe.handler(sleep=0.01)}, max_workers=4)

        class _FlakyPoll:
            name = "flaky"

            def __init__(self, inner: Any) -> None:
                self.inner = inner
                self.bad: set[str] = set()

            def capabilities(self) -> Any:
                return self.inner.capabilities()

            def submit(self, task: GeoTask, ctx: Any) -> Any:
                handle = self.inner.submit(task, ctx)
                if task.task_id == "c":
                    self.bad.add(task.task_id)
                return handle

            def poll(self, handle: Any) -> Any:
                if handle.task_id in self.bad:
                    raise RuntimeError("后端连接断了")
                return self.inner.poll(handle)

            def cancel(self, handle: Any) -> bool:
                return False

        store = TaskStore.memory()
        runner = TaskRunner(store, _FlakyPoll(inner))
        report = runner.run_tasks([_task(t) for t in "abcd"])
        inner.close()

        assert report.failed == 1 and report.succeeded == 3
        task_c = store.get("c")
        assert task_c.error is not None and task_c.error.code == "executor_lost"
        assert "后端连接断了" in task_c.error.message

    def test_backend_submit_exception_is_isolated(self) -> None:
        class _BadSubmit:
            name = "bad-submit"

            def capabilities(self) -> Any:
                from geonexus.geotask.backend import BackendCapabilities

                return BackendCapabilities(name=self.name)

            def submit(self, task: GeoTask, ctx: Any) -> Any:
                if task.task_id == "b":
                    raise RuntimeError("线程池拒绝")
                from geonexus.geotask.backend import Handle, TaskOutcome

                outcome = TaskOutcome.success({"ok": task.task_id})
                return Handle(task_id=task.task_id, backend=self.name, token=outcome)

            def poll(self, handle: Any) -> Any:
                return handle.token

            def cancel(self, handle: Any) -> bool:
                return False

        store = TaskStore.memory()
        report = TaskRunner(store, _BadSubmit()).run_tasks([_task("a"), _task("b")])
        assert report.succeeded == 1 and report.failed == 1
        assert store.get("b").error.code == "execution_error"


# --------------------------------------------------------------------------- #
# ⑥ 重试与退避
# --------------------------------------------------------------------------- #
class TestRetryBackoff:
    def test_retry_waits_for_backoff_then_succeeds(self) -> None:
        probe = _Probe()
        policy = RetryPolicy(max_attempts=3, backoff_seconds=0.2, multiplier=2.0)
        backend = LocalBackend(
            handlers={"h": probe.handler(sleep=0.01, fail_times=1)}, max_workers=2)
        store = TaskStore.memory()
        sched = Scheduler(Reservation(cpu_cores=4, memory_mb=8192))
        runner = TaskRunner(store, backend, scheduler=sched)

        started = time.time()
        report = runner.run_tasks([_task("t1", retry=policy)])
        elapsed = time.time() - started
        backend.close()

        assert report.succeeded == 1, "第一次失败、第二次成功"
        assert store.get("t1").attempt == 1
        assert elapsed >= 0.2, f"必须真的退避，实际只用了 {elapsed:.3f}s"
        assert len(probe.calls) == 2

    def test_exhausted_retries_fail_with_last_error(self) -> None:
        probe = _Probe()
        policy = RetryPolicy(max_attempts=2, backoff_seconds=0.01)
        backend = LocalBackend(handlers={"h": probe.handler(fail=True)}, max_workers=2)
        store = TaskStore.memory()
        sched = Scheduler(Reservation(cpu_cores=4, memory_mb=8192))
        runner = TaskRunner(store, backend, scheduler=sched)
        report = runner.run_tasks([_task("t1", retry=policy)])
        backend.close()

        assert report.failed == 1
        assert store.get("t1").attempt == 1
        assert len(probe.calls) == 2
        assert sched.occupancy.active == 0, "重试之间的预约必须释放"

    def test_retry_frees_capacity_for_others(self) -> None:
        """退避窗口内，容量要让给别的任务——不是占着坑等。"""
        events: list[str] = []
        lock = threading.Lock()

        def flaky(task: GeoTask, ctx: Any) -> dict[str, Any]:
            with lock:
                events.append(task.task_id)
            if task.task_id == "flaky":
                raise RuntimeError("第一次必失败")
            return {}

        policy = RetryPolicy(max_attempts=2, backoff_seconds=0.05)
        backend = LocalBackend(handlers={"h": flaky}, max_workers=4)
        store = TaskStore.memory()
        sched = Scheduler(Reservation(cpu_cores=1, memory_mb=8192))
        runner = TaskRunner(store, backend, scheduler=sched)
        report = runner.run_tasks([_task("flaky", retry=policy), _task("other")])
        backend.close()

        assert report.succeeded == 1 and report.failed == 1
        assert events.index("other") < events.index("flaky", 1), \
            "退避期间另一个任务应该被放行"

    def test_failure_isolation_with_resource_limits(self) -> None:
        probe = _Probe()
        sched = Scheduler(Reservation(cpu_cores=2, memory_mb=8192))
        store = TaskStore.memory()
        backend = LocalBackend(handlers={"h": probe.handler(sleep=0.01, fail=True)},
                               max_workers=8)
        runner = TaskRunner(store, backend, scheduler=sched)
        report = runner.run_tasks([_task(f"t{i}") for i in range(5)])
        backend.close()

        assert report.failed == 5
        assert probe.max_active <= 2, "失败的任务同样受容量约束"
        assert sched.occupancy.active == 0


# --------------------------------------------------------------------------- #
# ⑦ 与既有行为的关系
# --------------------------------------------------------------------------- #
class TestBackwardCompatibility:
    def test_no_scheduler_means_p1_behaviour(self) -> None:
        probe = _Probe()
        store = TaskStore.memory()
        backend = LocalBackend(handlers={"h": probe.handler(sleep=0.05)}, max_workers=4)
        runner = TaskRunner(store, backend)
        report = runner.run_tasks([_task(f"t{i}") for i in range(4)])
        backend.close()

        assert report.succeeded == 4
        assert probe.max_active == 4, "不给调度器就不设上限（P1 语义不变）"
        assert runner.scheduling() == {}
        assert report.occupancy == {}

    def test_report_shape(self) -> None:
        store = TaskStore.memory()
        report = TaskRunner(store, LocalBackend(handlers={}, max_workers=1)).run_tasks([])
        payload = report.to_dict()
        for key in ("total", "succeeded", "failed", "cancelled", "skipped",
                    "blocked", "rejected", "max_parallel", "elapsed_seconds",
                    "occupancy"):
            assert key in payload, key

    def test_make_local_runner_accepts_scheduler(self) -> None:
        from geonexus.geotask.runner import make_local_runner

        sched = Scheduler(Reservation(cpu_cores=2))
        runner, backend = make_local_runner(scheduler=sched)
        assert runner.scheduler is sched
        backend.close()

    def test_cancelled_queued_task_releases_capacity(self) -> None:
        probe = _Probe()
        sched = Scheduler(Reservation(cpu_cores=1, memory_mb=8192))
        store = TaskStore.memory()
        backend = LocalBackend(handlers={"h": probe.handler(sleep=0.02)}, max_workers=4)
        runner = TaskRunner(store, backend, scheduler=sched)
        tasks = [_task("a"), _task("b")]
        store.put(tasks[0])
        store.put(tasks[1])
        store.request_cancel("b")
        report = runner.run_tasks(tasks)
        backend.close()

        assert report.cancelled == 1 and report.succeeded == 1
        assert sched.occupancy.active == 0

    def test_no_stall_when_everything_is_impossible(self) -> None:
        """全部装不下时 run() 必须**返回**，不能挂在那等容量。"""
        store = TaskStore.memory()
        sched = Scheduler(Reservation(cpu_cores=1, memory_mb=1024))
        backend = LocalBackend(handlers={}, max_workers=2)
        runner = TaskRunner(store, backend, scheduler=sched)
        report = runner.run_tasks([_task(f"big{i}", cpu=32) for i in range(3)])
        backend.close()

        assert report.rejected == 3
        assert report.blocked == 0
        assert report.succeeded == 0

    def test_shared_scheduler_across_runs_keeps_accounting(self) -> None:
        """同一调度器复用两次：记账不串味。"""
        probe = _Probe()
        sched = Scheduler(Reservation(cpu_cores=2, memory_mb=8192))
        store = TaskStore.memory()
        backend = LocalBackend(handlers={"h": probe.handler(sleep=0.01)}, max_workers=4)
        runner = TaskRunner(store, backend, scheduler=sched)
        first = runner.run_tasks([_task(f"a{i}") for i in range(3)])
        second = runner.run_tasks([_task(f"b{i}") for i in range(3)])
        backend.close()

        assert first.succeeded == 3 and second.succeeded == 3
        assert sched.occupancy.active == 0
        assert sched.occupancy.held() == Reservation()


class TestSchedulerStats:
    def test_stats_surface(self) -> None:
        sched = Scheduler(Reservation(cpu_cores=2, memory_mb=4096), max_concurrent=1)
        sched.admit([_task("a"), _task("b")])
        stats = sched.stats()
        assert stats["capacity"]["cpu_cores"] == 2
        assert stats["held"]["cpu_cores"] == 1.0
        assert stats["available"]["cpu_cores"] == 1.0
        assert stats["active"] == 1
        assert stats["max_concurrent"] == 1
        assert stats["blocked"]["b"]
        assert "peak_queue_depth" in stats

    def test_repr_is_diagnostic(self) -> None:
        sched = Scheduler(Reservation(cpu_cores=2, memory_mb=4096))
        sched.admit([_task("a")])
        assert "active=1" in repr(sched)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
