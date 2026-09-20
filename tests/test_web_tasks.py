"""Tests for the Web-layer TaskManager (geonexus.web.tasks)."""

from __future__ import annotations

import threading
import time

import pytest

from geonexus.web.tasks import (
    CANCELLED,
    DONE,
    FAILED,
    QUEUED,
    RUNNING,
    TaskManager,
    TaskNotCancellableError,
    TaskNotFoundError,
)


class TestTaskManager:
    def test_successful_task(self) -> None:
        with TaskManager() as tm:
            tid = tm.submit(lambda: {"answer": 42})
            state = tm.wait(tid, timeout=10)
            assert state["status"] == DONE
            assert state["result"] == {"answer": 42}

    def test_failed_task_records_error(self) -> None:
        def boom() -> None:
            raise ValueError("bad input")

        with TaskManager() as tm:
            tid = tm.submit(boom)
            state = tm.wait(tid, timeout=10)
            assert state["status"] == FAILED
            assert "bad input" in (state["error"] or "")
            assert "result" not in state

    def test_cancel_queued_task(self) -> None:
        with TaskManager() as tm:
            # A task that blocks on an event; cancel it while queued/running.
            import threading

            gate = threading.Event()

            def worker() -> str:
                gate.wait(10)
                return "done"

            tid = tm.submit(worker)
            # Give the pool a moment to pick it up.
            time.sleep(0.1)
            state = tm.cancel(tid)
            assert state["cancelled"] is True
            gate.set()
            final = tm.wait(tid, timeout=10)
            assert final["status"] in (CANCELLED, DONE)

    def test_cancel_unknown_task(self) -> None:
        with TaskManager() as tm, pytest.raises(TaskNotFoundError):
            tm.cancel("nope")

    def test_cancel_terminal_task_rejected(self) -> None:
        with TaskManager() as tm:
            tid = tm.submit(lambda: 1)
            tm.wait(tid, timeout=10)
            with pytest.raises(TaskNotCancellableError):
                tm.cancel(tid)

    def test_get_unknown_task(self) -> None:
        with TaskManager() as tm, pytest.raises(TaskNotFoundError):
            tm.get("nope")

    def test_progress_updates(self) -> None:
        events: list[dict] = []

        def worker(task_id: str) -> str:
            tm.update_progress(task_id, 0.5, "halfway")
            time.sleep(0.05)
            tm.update_progress(task_id, 1.0, "done step")
            return "ok"

        # max_workers=1 plus a gate task holds `worker` in QUEUED until we have
        # observed that state, so the assertion cannot race the worker pool.
        gate = threading.Event()
        with TaskManager(max_workers=1, persist=events.append) as tm:
            blocking = tm.submit(lambda: gate.wait(10))
            tid = tm.submit(worker, message="start")
            assert tm.get(tid)["status"] == QUEUED
            gate.set()
            tm.wait(blocking, timeout=10)
            state = tm.wait(tid, timeout=10)

        assert state["status"] == DONE
        assert state["result"] == "ok"
        assert state["progress"] == 1.0
        assert state["message"] == "done step"

        # Every transition is reported, including the intermediate progress
        # update. Ordering is not asserted: it is not guaranteed across threads.
        seen = [e for e in events if e["id"] == tid]
        assert any(e["status"] == QUEUED for e in seen)
        assert any(e["progress"] == 0.5 for e in seen)
        assert any(e["status"] == DONE for e in seen)

    def test_should_cancel_cooperation(self) -> None:
        def worker(task_id: str) -> str:
            for _ in range(50):
                if tm.should_cancel(task_id):
                    return "cancelled-cooperatively"
                time.sleep(0.01)
            return "finished"

        with TaskManager() as tm:
            tid = tm.submit(worker)
            time.sleep(0.1)
            tm.cancel(tid)
            state = tm.wait(tid, timeout=10)
            assert state["result"] == "cancelled-cooperatively"

    def test_list_sorted_newest_first(self) -> None:
        with TaskManager() as tm:
            tm.submit(lambda: 1)
            time.sleep(0.01)
            tm.submit(lambda: 2)
            tasks = tm.list()
            assert tasks[0]["created_at"] >= tasks[1]["created_at"]
            assert "result" not in tasks[0]

    def test_persist_callback_receives_events(self) -> None:
        events: list[dict] = []
        with TaskManager(persist=lambda data: events.append(data)) as tm:
            tid = tm.submit(lambda: "x")
            tm.wait(tid, timeout=10)
        assert any(e["id"] == tid and e["status"] == QUEUED for e in events)
        assert any(e["id"] == tid and e["status"] == DONE for e in events)

    def test_queued_status_before_run(self) -> None:
        with TaskManager(max_workers=1) as tm:
            first = tm.submit(lambda: time.sleep(0.3) or "a")
            second = tm.submit(lambda: "b")
            # The second task is queued behind the first.
            assert tm.get(second)["status"] == QUEUED
            tm.wait(first, timeout=10)
            tm.wait(second, timeout=10)


# --------------------------------------------------------------------------- #
# 适配器契约：Web 层用的确实是 GeoTask
# --------------------------------------------------------------------------- #
class TestGeoTaskAdapter:
    """`web.TaskManager` 是 GeoTask 的适配器视图，不是另一套实现。

    对外行为（11 项旧测试）不变，但内部必须真的走 GeoTask——否则"统一任务模型"
    只是说法。这里用 GeoTask 自己的事件流与状态机来证明这一点。
    """

    def test_transitions_are_recorded_as_seq_events(self) -> None:
        """每次状态迁移都产生带 seq 的事件——这是 GeoTask 事件契约的形态。"""
        with TaskManager() as tm:
            tid = tm.submit(lambda: "x")
            tm.wait(tid, timeout=10)
            events, latest = tm.store.events_since(tid)

        assert [e["seq"] for e in events] == list(range(1, latest + 1)), "seq 必须无洞"
        kinds = [e["event"] for e in events]
        assert kinds[0] == "task.created"
        assert "task.state_changed" in kinds
        states = [e["data"]["to"] for e in events if e["event"] == "task.state_changed"]
        assert states == ["queued", "scheduled", "running", "succeeded"], states

    def test_store_is_the_single_source_of_truth(self) -> None:
        """``get()`` 只是投影：store 里的状态与它必须一致。"""
        with TaskManager() as tm:
            tid = tm.submit(lambda: 1)
            tm.wait(tid, timeout=10)
            stored = tm.store.require(tid)
            view = tm.get(tid)

        assert stored.state.value == "succeeded"
        assert view["status"] == DONE

    def test_cancel_flag_lives_on_the_geotask(self) -> None:
        """协作式取消的标志放在 GeoTask 上，`should_cancel` 读的就是它。"""
        import threading as _t

        gate = _t.Event()
        with TaskManager() as tm:
            tid = tm.submit(lambda: gate.wait(10))
            time.sleep(0.1)
            tm.cancel(tid)
            assert tm.store.require(tid).cancel_requested is True
            assert tm.should_cancel(tid) is True
            gate.set()
            tm.wait(tid, timeout=10)

    def test_callables_never_enter_the_serializable_spec(self) -> None:
        """可调用对象必须在侧表里。

        ``spec`` 要能 JSON 序列化（落盘、进事件）；把函数塞进去会让 to_dict() 崩。
        """
        import json

        with TaskManager() as tm:
            tid = tm.submit(lambda: "x")
            task = tm.store.require(tid)
            json.dumps(task.to_dict())      # 抛异常即失败
            assert task.spec == {"handler": "__web_callable__"}


class TestVocabularyProjection:
    """§8.2 的词表投影：Web 用 done/queued，GeoTask 用 succeeded/…。"""

    def test_mapping_is_total_and_inverse(self) -> None:
        from geonexus.geotask import TaskState
        from geonexus.web.tasks import STATE_TO_WEB, WEB_TO_STATE

        # 每个 GeoTask 状态都要有 Web 投影（全函数），否则投影时会 KeyError
        assert set(STATE_TO_WEB) == set(TaskState)
        # 每个 Web 词都要有反向映射
        assert set(WEB_TO_STATE) == {QUEUED, RUNNING, DONE, FAILED, CANCELLED}
        # 五个 Web 词都能往返
        for word, state in WEB_TO_STATE.items():
            assert STATE_TO_WEB[state] == word

    def test_internal_states_merge_into_queued(self) -> None:
        """created/scheduled/blocked 对外都是 queued：调用方只关心"还没跑"。"""
        from geonexus.geotask import TaskState
        from geonexus.web.tasks import STATE_TO_WEB

        for state in (TaskState.CREATED, TaskState.SCHEDULED, TaskState.BLOCKED):
            assert STATE_TO_WEB[state] == QUEUED

    def test_skipped_projects_to_cancelled_not_done(self) -> None:
        """跳过没有产出，投影成 done 就是撒谎。"""
        from geonexus.geotask import TaskState
        from geonexus.web.tasks import STATE_TO_WEB

        assert STATE_TO_WEB[TaskState.SKIPPED] == CANCELLED
