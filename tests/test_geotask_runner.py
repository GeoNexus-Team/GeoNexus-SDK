"""GeoTask 行为测试 —— 路线图 Phase 1 的四条验收判据。

与 ``test_geotask_contract.py`` 的分工：那个文件钉**契约形状**（状态机与文档逐条
相等、API 存在），这个文件钉**行为**：

1. 有依赖的任务图能**真并行**跑就绪步（不是"看起来并行"）；
2. 进程重启后**不撒谎**（无执行者的任务不得报 running）；
3. 协作式取消在步骤边界生效，终态是 ``cancelled`` 而非 ``succeeded``；
4. 失败级联、重试、幂等、事件无洞在真实执行下依然成立。

并行那一条用 ``threading.Barrier`` 而不是计时：三个任务同时到达栅栏才会放行，
串行执行会超时并失败。计时断言会随机器负载飘，栅栏不会。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from geonexus.geotask import (
    EventHistoryGone,
    GeoTask,
    JsonTaskStore,
    LocalBackend,
    RetryPolicy,
    TaskGraph,
    TaskKind,
    TaskRunner,
    TaskState,
    TaskStore,
)


def _task(task_id: str, skill: str = "echo", **kw) -> GeoTask:
    return GeoTask(task_id=task_id, kind=TaskKind.SKILL, spec={"skill": skill}, **kw)


def _runner(handlers: dict, store: TaskStore | None = None,
            max_workers: int = 4) -> tuple[TaskRunner, TaskStore, LocalBackend]:
    store = store or TaskStore.memory()
    backend = LocalBackend(handlers=handlers, max_workers=max_workers)
    return TaskRunner(store, backend), store, backend


# --------------------------------------------------------------------------- #
# 1. 就绪集真并行
# --------------------------------------------------------------------------- #
class TestParallelReadySet:
    def test_independent_tasks_run_concurrently(self) -> None:
        """三个互不依赖的任务必须**同时在跑**。

        栅栏要求三个执行体同时到达才放行。若调度是串行的，第一个会超时抛
        ``BrokenBarrierError`` → 任务失败 → 断言不成立。这比计时断言可靠得多。
        """
        barrier = threading.Barrier(3, timeout=5)
        seen: list[str] = []
        lock = threading.Lock()

        def handler(task, ctx):
            with lock:
                seen.append(task.task_id)
            barrier.wait()          # 只有真并行才会释放
            return {"ok": True}

        runner, store, backend = _runner({"echo": handler})
        try:
            report = runner.run(TaskGraph.from_tasks(
                [_task("a"), _task("b"), _task("c")]))
        finally:
            backend.close()

        assert report.succeeded == 3, report.to_dict()
        assert sorted(seen) == ["a", "b", "c"]
        assert report.max_parallel >= 3, report.to_dict()

    def test_dependent_step_waits_for_all_upstreams(self) -> None:
        """``a → c``、``b → c``：c 必须等 a 和 b 都完成才开跑。"""
        order: list[str] = []
        lock = threading.Lock()

        def handler(task, ctx):
            with lock:
                order.append(task.task_id)
            time.sleep(0.02)
            return {"ok": True}

        runner, store, backend = _runner({"echo": handler})
        try:
            graph = TaskGraph.from_tasks([
                _task("a"), _task("b"),
                _task("c", depends_on=["a", "b"]),
            ])
            report = runner.run(graph)
        finally:
            backend.close()

        assert report.succeeded == 3, report.to_dict()
        assert order.index("c") == 2, f"c 应当在 a/b 之后: {order}"
        assert report.max_parallel >= 2, "a 与 b 应当并行"

    def test_fan_out_fan_in_executes_every_node_once(self) -> None:
        """防止"提交后忘了标记 RUNNING 导致重复提交"这类回归。"""
        counts: dict[str, int] = {}
        lock = threading.Lock()

        def handler(task, ctx):
            with lock:
                counts[task.task_id] = counts.get(task.task_id, 0) + 1
            time.sleep(0.01)
            return {"ok": True}

        runner, store, backend = _runner({"echo": handler})
        try:
            graph = TaskGraph.from_tasks([
                _task("root"),
                _task("l1", depends_on=["root"]),
                _task("l2", depends_on=["root"]),
                _task("l3", depends_on=["root"]),
                _task("join", depends_on=["l1", "l2", "l3"]),
            ])
            report = runner.run(graph)
        finally:
            backend.close()

        assert report.succeeded == 5, report.to_dict()
        assert counts == {"root": 1, "l1": 1, "l2": 1, "l3": 1, "join": 1}, counts


# --------------------------------------------------------------------------- #
# 2. 重启对账
# --------------------------------------------------------------------------- #
class TestRestartReconciliationEndToEnd:
    def test_json_store_survives_reload_and_never_reports_running(
            self, tmp_path: Path) -> None:
        """真实重启场景：文件落盘 → 新进程载入 → 对账。

        这是"不撒谎"原则的端到端验证：旧进程在 running 状态被杀掉，新进程载入后
        该任务**必须**被判定为执行者丢失，而不是继续显示 running。
        """
        path = tmp_path / "geotask.json"

        first = JsonTaskStore(path)
        first.put(_task("t1", retry=RetryPolicy(max_attempts=1)))
        for state in (TaskState.QUEUED, TaskState.SCHEDULED, TaskState.RUNNING):
            first.transition("t1", state)
        first.set_lease("t1", expires_at=time.time() - 1, executor="dead-worker")
        assert first.get("t1").state is TaskState.RUNNING

        # ── 模拟进程重启 ──
        second = JsonTaskStore(path)
        assert second.get("t1").state is TaskState.RUNNING, "载入时如实反映磁盘状态"
        report = second.reconcile()

        assert second.get("t1").state is TaskState.FAILED
        assert second.get("t1").error.code == "executor_lost"
        assert report["failed"] == 1 and report["requeued"] == 0

    def test_retryable_task_is_requeued_not_failed(self, tmp_path: Path) -> None:
        path = tmp_path / "geotask.json"
        store = JsonTaskStore(path)
        store.put(_task("t1", retry=RetryPolicy(max_attempts=3)))
        for state in (TaskState.QUEUED, TaskState.SCHEDULED, TaskState.RUNNING):
            store.transition("t1", state)
        store.set_lease("t1", expires_at=time.time() - 1)

        reopened = JsonTaskStore(path)
        reopened.reconcile()
        assert reopened.get("t1").state is TaskState.QUEUED
        assert reopened.get("t1").attempt == 1

    def test_live_lease_is_left_alone(self, tmp_path: Path) -> None:
        """租约没过期的任务是有执行者的，对账不能动它。"""
        store = JsonTaskStore(tmp_path / "g.json")
        store.put(_task("t1"))
        for state in (TaskState.QUEUED, TaskState.SCHEDULED, TaskState.RUNNING):
            store.transition("t1", state)
        store.set_lease("t1", expires_at=time.time() + 3600)

        report = store.reconcile()
        assert store.get("t1").state is TaskState.RUNNING
        assert report == {"requeued": 0, "failed": 0, "checked": 1}

    def test_events_survive_reload(self, tmp_path: Path) -> None:
        path = tmp_path / "g.json"
        store = JsonTaskStore(path)
        store.put(_task("t1"))
        store.transition("t1", TaskState.QUEUED)
        before, latest = store.events_since("t1")

        reopened = JsonTaskStore(path)
        after, latest2 = reopened.events_since("t1")
        assert latest2 == latest
        assert [e["seq"] for e in after] == [e["seq"] for e in before]
        assert [e["event"] for e in after] == [e["event"] for e in before]


# --------------------------------------------------------------------------- #
# 3. 协作式取消
# --------------------------------------------------------------------------- #
class TestCooperativeCancellation:
    def test_cancel_takes_effect_at_step_boundary(self) -> None:
        """执行体在步骤边界看到取消标志 → 自行结束 → 终态 cancelled。"""
        started = threading.Event()
        passed_boundaries = 0

        def handler(task, ctx):
            nonlocal passed_boundaries
            for i in range(500):
                if ctx.should_cancel():
                    return {"stopped_at": i}
                passed_boundaries = i
                started.set()
                time.sleep(0.005)
            return {"stopped_at": -1}

        runner, store, backend = _runner({"echo": handler})
        graph = TaskGraph.from_tasks([_task("slow")])
        result: dict[str, object] = {}

        def drive():
            result["report"] = runner.run(graph)

        thread = threading.Thread(target=drive)
        try:
            thread.start()
            assert started.wait(5), "执行体没有启动"
            store.request_cancel("slow")
            thread.join(timeout=10)
        finally:
            backend.close()

        assert not thread.is_alive(), "取消后执行体没有停下"
        assert store.get("slow").state is TaskState.CANCELLED
        assert store.get("slow").state is not TaskState.SUCCEEDED, \
            "取消不得被记成成功（GeoKG 的 refresh 踩过这个坑）"
        assert passed_boundaries < 499, "应当在中途停下，而不是跑完"

    def test_cancel_before_start_is_immediate(self) -> None:
        store = TaskStore.memory()
        store.put(_task("t1"))
        store.request_cancel("t1")
        assert store.get("t1").state is TaskState.CANCELLED, \
            "尚未开始执行的任务应立即取消"

    def test_cancel_of_terminal_task_is_rejected(self) -> None:
        from geonexus.geotask import IllegalTransition

        store = TaskStore.memory()
        store.put(_task("t1"))
        store.transition("t1", TaskState.QUEUED)
        store.transition("t1", TaskState.CANCELLED)
        with pytest.raises(IllegalTransition):
            store.request_cancel("t1")

    def test_local_backend_admits_it_cannot_cancel(self) -> None:
        """后端必须**诚实**：线程杀不掉，就返回 False，而不是假装成功。"""
        backend = LocalBackend(handlers={}, max_workers=1)
        try:
            caps = backend.capabilities()
            assert caps.supports_cancel is False
            assert backend.cancel(type("H", (), {"task_id": "x", "token": None})()) is False
        finally:
            backend.close()


# --------------------------------------------------------------------------- #
# 4. 失败、重试、幂等、事件
# --------------------------------------------------------------------------- #
class TestFailureAndRetry:
    def test_upstream_failure_cascades_and_downstream_never_runs(self) -> None:
        ran: list[str] = []

        def boom(task, ctx):
            ran.append(task.task_id)
            raise RuntimeError("上游炸了")

        def ok(task, ctx):
            ran.append(task.task_id)
            return {"ok": True}

        runner, store, backend = _runner({"boom": boom, "echo": ok})
        try:
            graph = TaskGraph.from_tasks([
                _task("a", skill="boom"),
                _task("b", depends_on=["a"]),
                _task("c", depends_on=["b"]),
            ])
            report = runner.run(graph)
        finally:
            backend.close()

        assert ran == ["a"], f"下游不得被执行: {ran}"
        assert report.failed == 3, report.to_dict()
        assert store.get("a").error.code == "execution_error"
        assert store.get("b").error.code == "upstream_failed"
        assert store.get("c").error.code == "upstream_failed"

    def test_retryable_failure_retries_then_succeeds(self) -> None:
        attempts: list[int] = []

        def flaky(task, ctx):
            attempts.append(task.attempt)
            if len(attempts) < 3:
                raise RuntimeError("还没好")
            return {"ok": True}

        runner, store, backend = _runner({"echo": flaky})
        try:
            graph = TaskGraph.from_tasks(
                [_task("t1", retry=RetryPolicy(max_attempts=3, backoff_seconds=0))])
            report = runner.run(graph)
        finally:
            backend.close()

        assert report.succeeded == 1, report.to_dict()
        assert len(attempts) == 3, f"应当重试到第 3 次: {attempts}"
        assert store.get("t1").attempt == 2

    def test_non_retryable_failure_stops_immediately(self) -> None:
        calls: list[int] = []

        def unknown(task, ctx):     # 没有注册处理器 → invalid_spec（不可重试）
            calls.append(1)
            return {}

        runner, store, backend = _runner({})
        try:
            graph = TaskGraph.from_tasks(
                [_task("t1", skill="missing", retry=RetryPolicy(max_attempts=5))])
            report = runner.run(graph)
        finally:
            backend.close()

        assert report.failed == 1
        assert store.get("t1").error.code == "invalid_spec"
        assert calls == [], "不可重试的失败不该被执行体跑第二遍"

    def test_resubmitting_a_completed_task_is_a_noop(self) -> None:
        calls: list[str] = []

        def handler(task, ctx):
            calls.append(task.task_id)
            return {"ok": True}

        runner, store, backend = _runner({"echo": handler})
        try:
            report = runner.run(TaskGraph.from_tasks([_task("t1")]))
            assert report.succeeded == 1
            again = store.put(_task("t1"))
        finally:
            backend.close()

        assert again.state is TaskState.SUCCEEDED, "重复提交不得重启已完成的任务"
        assert calls == ["t1"], "执行体只应跑一次"


class TestEventStream:
    def test_sequence_is_gap_free_across_a_real_run(self) -> None:
        runner, store, backend = _runner({"echo": lambda t, c: {"ok": True}})
        try:
            runner.run(TaskGraph.from_tasks([_task("t1")]))
        finally:
            backend.close()

        events, latest = store.events_since("t1")
        assert [e["seq"] for e in events] == list(range(1, latest + 1)), \
            "同一任务内 seq 必须无洞"
        kinds = [e["event"] for e in events]
        assert kinds[0] == "task.created"
        assert "task.state_changed" in kinds
        assert kinds[-1] == "task.state_changed"
        assert events[-1]["data"]["to"] == "succeeded"

    def test_replay_from_a_checkpoint_has_no_duplicates(self) -> None:
        runner, store, backend = _runner({"echo": lambda t, c: {"ok": True}})
        try:
            runner.run(TaskGraph.from_tasks([_task("t1")]))
        finally:
            backend.close()

        all_events, _ = store.events_since("t1", 0)
        tail, _ = store.events_since("t1", 2)
        assert [e["seq"] for e in tail] == [e["seq"] for e in all_events if e["seq"] > 2]
        assert not ({e["seq"] for e in tail} & {e["seq"] for e in all_events[:2]})

    def test_event_history_gap_is_reported_not_hidden(self) -> None:
        """历史被裁掉时必须报错，不能静默从头开始（否则客户端会重复处理）。"""
        store = TaskStore.memory(event_retention=3)
        store.put(_task("t1"))
        for i in range(6):
            store.append_event("t1", "task.progress", {"i": i})
        with pytest.raises(EventHistoryGone):
            store.events_since("t1", since=1)

    def test_event_payload_is_json_serializable(self) -> None:
        """事件要能直接走 SSE / 落到 JSON store，不能带不可序列化的东西。"""
        runner, store, backend = _runner({"echo": lambda t, c: {"ok": True}})
        try:
            runner.run(TaskGraph.from_tasks([_task("t1")]))
        finally:
            backend.close()

        events, _ = store.events_since("t1")
        json.dumps(events)   # 抛异常即失败


class TestProgressAndCapabilities:
    def test_progress_is_reported_and_ends_at_one(self) -> None:
        def handler(task, ctx):
            for i in (1, 2, 3):
                ctx.report_progress(i / 3, f"步骤 {i}")
                time.sleep(0.005)
            return {"ok": True}

        runner, store, backend = _runner({"echo": handler})
        try:
            runner.run(TaskGraph.from_tasks([_task("t1")]))
        finally:
            backend.close()

        events, _ = store.events_since("t1")
        progress = [e["data"]["progress"] for e in events if e["event"] == "task.progress"]
        assert progress == pytest.approx([1 / 3, 2 / 3, 1.0])

    def test_local_backend_declares_what_it_supports(self) -> None:
        backend = LocalBackend(handlers={}, max_workers=1)
        try:
            caps = backend.capabilities()
            assert caps.name == "local"
            assert "skill" in caps.kinds
            assert caps.supports_progress is True
            assert "cpu" in caps.resource_dims
        finally:
            backend.close()

    def test_resource_requirement_converts_from_geocard_runtime(self) -> None:
        """GeoCard 的 runtime 是**声明**，这里是换算成**预约**，复用 parse_memory。"""
        from geonexus.geotask import ResourceRequirement

        req = ResourceRequirement.from_geocard_runtime(
            {"cpu": 4, "memory": "16Gi", "gpu": "cuda"})
        assert req.cpu == 4.0
        assert req.memory_mb == 16 * 1024
        assert req.gpu is not None and req.gpu.kind == "cuda"

        plain = ResourceRequirement.from_geocard_runtime({"cpu": 2, "memory": "512Mi"})
        assert plain.gpu is None and plain.memory_mb == 512
