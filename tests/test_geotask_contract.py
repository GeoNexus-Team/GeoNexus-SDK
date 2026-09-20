"""GeoTask 契约测试 —— ``docs/GEOTASK.md`` 的可执行形式。

两类，目的不同：

1. **文档自洽性**：``GEOTASK.md`` 里那段 ``geotask-state-machine`` JSON 是权威契约，
   但它如果自相矛盾（迁移指向未声明的状态、终态还能往外走），实现者会照着错的写。
   所以先让文档被机器检查。

2. **实现契约**：状态机与文档**逐条相等**、非法迁移被拒、DAG 就绪集可并行、
   fail-fast 级联、重启对账不撒谎、事件 seq 无洞可重放、幂等。

Phase 0 时第 2 类曾用 ``xfail(strict=True)`` 标记（契约先写、实现后做），
Phase 1 落地后标记已全部移除——**这 16 项现在必须真的通过**，它们就是实现的门禁。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

DOC = Path(__file__).resolve().parent.parent / "docs" / "GEOTASK.md"

#: 实现未落地前的统一标记；Phase 1 完成后逐条移除


def _state_machine_block() -> dict:
    """从 GEOTASK.md 里取出权威状态机定义。"""
    text = DOC.read_text(encoding="utf-8")
    match = re.search(r"```json geotask-state-machine\n(.*?)\n```", text, re.S)
    assert match, "GEOTASK.md 里找不到 geotask-state-machine 代码块"
    return json.loads(match.group(1))


def _import_geotask():
    """延迟导入：模块还不存在时，让失败发生在测试体内而不是收集期。"""
    from geonexus import geotask

    return geotask


# --------------------------------------------------------------------------- #
# 第一类：文档自洽性（今天必须通过）
# --------------------------------------------------------------------------- #
class TestDocumentedContract:
    """契约文档先被机器检查，再拿去要求实现。"""

    def test_doc_exists_and_has_core_sections(self) -> None:
        assert DOC.exists(), f"缺少契约文档: {DOC}"
        text = DOC.read_text(encoding="utf-8")
        for section in (
            "## 二、数据模型",
            "## 三、状态机",
            "## 四、事件契约",
            "## 五、失败语义",
            "## 六、持久化与重启对账",
            "## 七、执行后端协议",
            "## 八、迁移映射",
        ):
            assert section in text, f"契约文档缺少章节: {section}"

    def test_state_machine_is_self_consistent(self) -> None:
        m = _state_machine_block()
        states = set(m["states"])
        terminal = set(m["terminal"])

        assert m["initial"] in states, "initial 不在 states 里"
        assert terminal <= states, f"terminal 里有未声明的状态: {terminal - states}"
        assert m["initial"] not in terminal, "initial 不能是终态"
        assert len(states) == len(m["states"]), "states 有重复"

        pairs = [tuple(t) for t in m["transitions"]]
        assert len(pairs) == len(set(pairs)), "transitions 有重复"

        for src, dst in pairs:
            assert src in states, f"迁移起点未声明: {src}"
            assert dst in states, f"迁移终点未声明: {dst}"
            assert src != dst, f"不允许自环: {src}"

        # 终态必须吸收：没有任何出边
        for t in terminal:
            outs = [d for s, d in pairs if s == t]
            assert not outs, f"终态 {t} 仍有出边: {outs}"

        # 非终态必须至少有一条出边，否则任务会卡死
        for s in states - terminal:
            outs = [d for src, d in pairs if src == s]
            assert outs, f"非终态 {s} 没有出边，任务会永久卡住"

        # 每个状态都必须可达（从 initial 出发），否则是死定义
        reachable, frontier = {m["initial"]}, [m["initial"]]
        while frontier:
            cur = frontier.pop()
            for src, dst in pairs:
                if src == cur and dst not in reachable:
                    reachable.add(dst)
                    frontier.append(dst)
        assert reachable == states, f"从 initial 到不了的状态: {states - reachable}"

    def test_terminal_states_match_the_documented_intent(self) -> None:
        """终态集合是刻意的：skipped 是终态，timeout 不是状态。"""
        m = _state_machine_block()
        assert set(m["terminal"]) == {"succeeded", "failed", "cancelled", "skipped"}
        # cafe 的 TIMEOUT 被刻意降级成 error.code，不作为状态
        assert "timeout" not in m["states"]
        assert "timed_out" not in m["states"]

    def test_events_contract_documents_sequence_and_replay(self) -> None:
        """seq 与 since 是本契约对 cafe.events 的修正，必须写在文档里。"""
        text = DOC.read_text(encoding="utf-8")
        assert '"seq"' in text and "严格递增" in text
        assert "since=" in text and "410" in text


# --------------------------------------------------------------------------- #
# 第二类：实现契约（strict xfail，今天为红）
# --------------------------------------------------------------------------- #
class TestStateMachineContract:
    def test_implementation_matches_documented_machine(self) -> None:
        """实现导出的状态机必须与文档**逐条相等**，防止文档与代码漂移。"""
        geotask = _import_geotask()
        m = _state_machine_block()
        assert {s.value for s in geotask.TaskState} == set(m["states"])
        assert {s.value for s in geotask.TERMINAL_STATES} == set(m["terminal"])
        assert geotask.INITIAL_STATE.value == m["initial"]
        assert {(s.value, d.value) for s, d in geotask.TRANSITIONS} == {
            tuple(t) for t in m["transitions"]
        }

    def test_illegal_transition_is_rejected(self) -> None:
        """状态机必须是代码里的关卡，不是文档约定。"""
        geotask = _import_geotask()
        with pytest.raises(geotask.IllegalTransition):
            geotask.assert_transition(geotask.TaskState.SUCCEEDED, geotask.TaskState.RUNNING)
        with pytest.raises(geotask.IllegalTransition):
            geotask.assert_transition(geotask.TaskState.CANCELLED, geotask.TaskState.QUEUED)

    def test_terminal_states_are_absorbing(self) -> None:
        geotask = _import_geotask()
        for state in geotask.TERMINAL_STATES:
            for target in geotask.TaskState:
                with pytest.raises(geotask.IllegalTransition):
                    geotask.assert_transition(state, target)

    def test_store_transition_enforces_the_machine(self) -> None:
        """`transition()` 是唯一改状态的入口，非法迁移要被拒。"""
        geotask = _import_geotask()
        store = geotask.TaskStore.memory()
        task = geotask.GeoTask(
            task_id="t1", kind=geotask.TaskKind.SKILL, spec={"skill": "ndvi-analysis"}
        )
        store.put(task)
        store.transition("t1", geotask.TaskState.QUEUED)
        with pytest.raises(geotask.IllegalTransition):
            store.transition("t1", geotask.TaskState.RUNNING)  # 必须先 scheduled


class TestDagContract:
    """DAG 就绪集：路线图点名要求"独立步骤真并行"，这是它的契约。"""

    def test_ready_set_exposes_parallelism(self) -> None:
        """a → c, b → c：a 与 b 必须同时可跑，而不是排队。"""
        geotask = _import_geotask()
        graph = geotask.TaskGraph.from_tasks(
            [
                geotask.GeoTask(task_id="a", kind=geotask.TaskKind.SKILL, spec={"skill": "x"}),
                geotask.GeoTask(task_id="b", kind=geotask.TaskKind.SKILL, spec={"skill": "x"}),
                geotask.GeoTask(
                    task_id="c",
                    kind=geotask.TaskKind.SKILL,
                    spec={"skill": "x"},
                    depends_on=["a", "b"],
                ),
            ]
        )
        assert set(graph.ready()) == {"a", "b"}
        graph.mark_done("a")
        assert set(graph.ready()) == {"b"}  # c 还等 b，不能提前跑
        graph.mark_done("b")
        assert set(graph.ready()) == {"c"}

    def test_cycle_is_rejected(self) -> None:
        geotask = _import_geotask()
        with pytest.raises(geotask.TaskGraphError):
            geotask.TaskGraph.from_tasks(
                [
                    geotask.GeoTask(
                        task_id="a",
                        kind=geotask.TaskKind.SKILL,
                        spec={"skill": "x"},
                        depends_on=["b"],
                    ),
                    geotask.GeoTask(
                        task_id="b",
                        kind=geotask.TaskKind.SKILL,
                        spec={"skill": "x"},
                        depends_on=["a"],
                    ),
                ]
            )

    def test_missing_dependency_is_rejected(self) -> None:
        geotask = _import_geotask()
        with pytest.raises(geotask.TaskGraphError):
            geotask.TaskGraph.from_tasks(
                [
                    geotask.GeoTask(
                        task_id="a",
                        kind=geotask.TaskKind.SKILL,
                        spec={"skill": "x"},
                        depends_on=["nope"],
                    ),
                ]
            )

    def test_upstream_failure_cascades_fail_fast(self) -> None:
        """契约 5.3：上游失败，下游 failed(upstream_failed)，且递归传播。"""
        geotask = _import_geotask()
        graph = geotask.TaskGraph.from_tasks(
            [
                geotask.GeoTask(task_id="a", kind=geotask.TaskKind.SKILL, spec={"skill": "x"}),
                geotask.GeoTask(
                    task_id="b", kind=geotask.TaskKind.SKILL, spec={"skill": "x"}, depends_on=["a"]
                ),
                geotask.GeoTask(
                    task_id="c", kind=geotask.TaskKind.SKILL, spec={"skill": "x"}, depends_on=["b"]
                ),
            ]
        )
        graph.mark_failed("a", code="execution_error")
        assert graph.state_of("b") is geotask.TaskState.FAILED
        assert graph.error_of("b").code == "upstream_failed"
        assert graph.state_of("c") is geotask.TaskState.FAILED

    def test_skipped_upstream_does_not_cascade_failure(self) -> None:
        """契约 5.3：跳过是策略决定，下游仍可跑。"""
        geotask = _import_geotask()
        graph = geotask.TaskGraph.from_tasks(
            [
                geotask.GeoTask(task_id="a", kind=geotask.TaskKind.SKILL, spec={"skill": "x"}),
                geotask.GeoTask(
                    task_id="b", kind=geotask.TaskKind.SKILL, spec={"skill": "x"}, depends_on=["a"]
                ),
            ]
        )
        graph.mark_skipped("a")
        assert graph.ready() == ["b"]


class TestRestartReconciliation:
    """契约第六节："重启后不许撒谎"。"""

    def test_expired_running_task_is_not_reported_running(self) -> None:
        geotask = _import_geotask()
        store = geotask.TaskStore.memory()
        task = geotask.GeoTask(
            task_id="t1",
            kind=geotask.TaskKind.SKILL,
            spec={"skill": "x"},
            retry=geotask.RetryPolicy(max_attempts=1),
        )
        store.put(task)
        for target in (
            geotask.TaskState.QUEUED,
            geotask.TaskState.SCHEDULED,
            geotask.TaskState.RUNNING,
        ):
            store.transition("t1", target)
        store.set_lease("t1", expires_at=0.0)  # 早已过期

        report = store.reconcile(now=1_000.0)
        assert store.get("t1").state is not geotask.TaskState.RUNNING, (
            "重启后不得把无执行者的任务报成 running"
        )
        assert store.get("t1").state is geotask.TaskState.FAILED
        assert store.get("t1").error.code == "executor_lost"
        assert report["requeued"] == 0 and report["failed"] == 1

    def test_expired_task_with_retries_left_is_requeued(self) -> None:
        geotask = _import_geotask()
        store = geotask.TaskStore.memory()
        store.put(
            geotask.GeoTask(
                task_id="t1",
                kind=geotask.TaskKind.SKILL,
                spec={"skill": "x"},
                retry=geotask.RetryPolicy(max_attempts=3),
            )
        )
        for target in (
            geotask.TaskState.QUEUED,
            geotask.TaskState.SCHEDULED,
            geotask.TaskState.RUNNING,
        ):
            store.transition("t1", target)
        store.set_lease("t1", expires_at=0.0)

        store.reconcile(now=1_000.0)
        assert store.get("t1").state is geotask.TaskState.QUEUED
        assert store.get("t1").attempt == 1

    def test_terminal_tasks_are_untouched_by_reconcile(self) -> None:
        geotask = _import_geotask()
        store = geotask.TaskStore.memory()
        store.put(geotask.GeoTask(task_id="t1", kind=geotask.TaskKind.SKILL, spec={"skill": "x"}))
        store.transition("t1", geotask.TaskState.QUEUED)
        store.transition("t1", geotask.TaskState.CANCELLED)
        before = store.get("t1").state
        store.reconcile(now=1_000.0)
        assert store.get("t1").state is before


class TestEventContract:
    """契约第四节：同一任务内 seq 严格递增、可重放、无洞。"""

    def test_sequence_is_per_task_and_monotonic(self) -> None:
        geotask = _import_geotask()
        store = geotask.TaskStore.memory()
        a = store.append_event("t1", "task.created", {})
        b = store.append_event("t1", "task.state_changed", {"to": "queued"})
        c = store.append_event("t2", "task.created", {})
        assert (a, b) == (1, 2), "同一任务内 seq 从 1 起严格递增"
        assert c == 1, "seq 是**每个任务内**的，不是全局的"

    def test_replay_since_is_gap_free_and_duplicate_free(self) -> None:
        geotask = _import_geotask()
        store = geotask.TaskStore.memory()
        for i in range(5):
            store.append_event("t1", "task.progress", {"progress": i / 5})
        events, latest = store.events_since("t1", since=2)
        assert [e["seq"] for e in events] == [3, 4, 5]
        assert latest == 5
        assert all(e["task_id"] == "t1" for e in events)

    def test_state_change_emits_an_event_automatically(self) -> None:
        """状态迁移必须自带事件，不能靠调用方记得发。"""
        geotask = _import_geotask()
        store = geotask.TaskStore.memory()
        store.put(geotask.GeoTask(task_id="t1", kind=geotask.TaskKind.SKILL, spec={"skill": "x"}))
        store.transition("t1", geotask.TaskState.QUEUED)
        events, _ = store.events_since("t1", since=0)
        assert any(
            e["event"] == "task.state_changed" and e["data"]["to"] == "queued" for e in events
        )


class TestIdempotency:
    def test_resubmitting_same_task_id_does_not_restart_it(self) -> None:
        """契约 5.4：task_id 是幂等键——跨节点重投递不得重复执行。"""
        geotask = _import_geotask()
        store = geotask.TaskStore.memory()
        original = geotask.GeoTask(task_id="t1", kind=geotask.TaskKind.SKILL, spec={"skill": "x"})
        store.put(original)
        store.transition("t1", geotask.TaskState.QUEUED)
        store.transition("t1", geotask.TaskState.SCHEDULED)
        store.transition("t1", geotask.TaskState.RUNNING)

        again = store.put(
            geotask.GeoTask(task_id="t1", kind=geotask.TaskKind.SKILL, spec={"skill": "x"})
        )
        assert again.state is geotask.TaskState.RUNNING, "重复提交不得重启任务"
        assert store.get("t1").created_at == original.created_at
