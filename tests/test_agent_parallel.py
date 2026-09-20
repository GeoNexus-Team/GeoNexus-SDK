"""PlanExecutor 的"就绪集并行"行为测试 —— 本轮改动的验收。

改动的起因：``PlanExecutor.run`` 原来是 ``for step_id in order:``——即使计划里
有 ``depends_on`` 形成的 DAG，也**严格串行**，可并行的分支被浪费。现在改为按
就绪集执行：一轮把所有"依赖已满足"的步骤一起提交。

并行用 ``threading.Barrier`` 证明，不用计时：三个步骤必须**同时**在跑才会放行，
串行执行会超时并失败。计时断言会随机器负载飘，栅栏不会。

同时钉住两个兼容性要点（改并行最容易悄悄弄坏的地方）：

* **隐式依赖**：``${stepN.outputs.k}`` 隐含"先跑 N"。旧实现靠串行顺序让它"碰巧
  能用"，并行后必须把这条边显式化——否则并行分支会读到空值；
* **依赖失败的步骤**仍是 ``failed`` + ``a dependency step failed``（逐字保留）。
"""

from __future__ import annotations

import threading
import time

from geonexus.agent import Goal, GoalStep, Plan, PlanExecutor, PlanStep


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class _FakeClient:
    """替代 FederatedGeoMCPClient：按技能名返回可控结果，并记录调用。"""

    def __init__(self, handler=None) -> None:
        self.calls: list[str] = []
        #: 每次调用收到的**已解析**参数——模板解析的可观测结果。
        #: 注意不能去读 ``step.params``：解析结果只落在 ``_run_step`` 的局部变量里，
        #: ``step.params`` 始终是原始模板（第一版测试就栽在这里）。
        self.params: list[tuple[str, dict]] = []
        self._lock = threading.Lock()
        self._handler = handler

    def _run(self, skill: str, params: dict) -> dict:
        with self._lock:
            self.calls.append(skill)
            self.params.append((skill, params))
        if self._handler is not None:
            return self._handler(skill)
        return {"outputs": {f"{skill}_out": skill}}

    def execute(self, skill: str, **kw: object) -> dict:
        return self._run(skill, dict(kw.get("params") or {}))

    def execute_skill(self, skill: str, **kw: object) -> dict:
        return self._run(skill, dict(kw.get("params") or {}))

    def close(self) -> None:
        """PlanExecutor.__exit__ 会关闭 client，替身也要能接住。"""


def _plan(*steps: GoalStep) -> Plan:
    """直接构造计划（不走 registry；client 会被替换掉）。"""
    goal = Goal(capability="x", steps=list(steps))
    plan = Plan(goal=goal.model_dump(exclude_none=True), registry_url="http://r:8790")
    for i, gs in enumerate(steps, start=1):
        plan.steps.append(
            PlanStep(
                step_id=i,
                kind="skill-only",
                skill=gs.skill,
                node_url="http://node:8787",
                geocards=list(gs.geocards),
                spatial=None,
                temporal=gs.temporal,
                params=dict(gs.params),
                description=gs.label or gs.skill,
                depends_on=list(gs.depends_on),
            )
        )
    return plan


def _executor(client: _FakeClient, **kw) -> PlanExecutor:
    ex = PlanExecutor("http://r:8790", timeout=5.0, **kw)
    ex.client = client  # type: ignore[assignment]
    return ex


# --------------------------------------------------------------------------- #
# 1. 真并行
# --------------------------------------------------------------------------- #
class TestReadySetParallelism:
    def test_independent_steps_run_concurrently(self) -> None:
        """三个互不依赖的步骤必须同时在跑。

        栅栏要求三个技能调用同时到达才放行；串行执行时第一个会超时抛
        ``BrokenBarrierError`` → 该步失败 → 断言不成立。这是一个**确定性**证明。
        """
        barrier = threading.Barrier(3, timeout=5)

        def handler(skill: str) -> dict:
            barrier.wait()
            return {"outputs": {"value": skill}}

        plan = _plan(GoalStep(skill="a"), GoalStep(skill="b"), GoalStep(skill="c"))
        with _executor(_FakeClient(handler)) as ex:
            plan = ex.run(plan)

        assert plan.done == 3, [(s.step_id, s.status, s.error) for s in plan.steps]
        assert plan.failed == 0

    def test_dependent_step_waits_for_all_upstreams(self) -> None:
        """``a→c``、``b→c``：c 必须等 a 与 b 都完成。"""
        started: list[str] = []
        lock = threading.Lock()

        def handler(skill: str) -> dict:
            with lock:
                started.append(skill)
            time.sleep(0.03)
            return {"outputs": {"value": skill}}

        plan = _plan(
            GoalStep(skill="a"),
            GoalStep(skill="b"),
            GoalStep(
                skill="c",
                depends_on=[1, 2],
                params={"x": "${step1.outputs.value}", "y": "${step2.outputs.value}"},
            ),
        )
        with _executor(_FakeClient(handler)) as ex:
            plan = ex.run(plan)

        assert plan.done == 3, [(s.step_id, s.status, s.error) for s in plan.steps]
        assert started[-1] == "c", f"c 应当在 a/b 之后: {started}"

    def test_each_step_runs_exactly_once(self) -> None:
        """防止"提交后忘了标记 RUNNING 导致重复提交"这类回归。"""
        client = _FakeClient()
        plan = _plan(
            GoalStep(skill="root"),
            GoalStep(skill="l1", depends_on=[1]),
            GoalStep(skill="l2", depends_on=[1]),
            GoalStep(skill="l3", depends_on=[1]),
            GoalStep(skill="join", depends_on=[2, 3, 4]),
        )
        with _executor(client) as ex:
            plan = ex.run(plan)

        assert plan.done == 5
        assert sorted(client.calls) == ["join", "l1", "l2", "l3", "root"], client.calls

    def test_max_workers_one_restores_serial_order(self) -> None:
        """``max_workers=1`` 退回旧的串行语义（结果必须一致）。"""
        client = _FakeClient()
        plan = _plan(GoalStep(skill="a"), GoalStep(skill="b"), GoalStep(skill="c"))
        with _executor(client, max_workers=1) as ex:
            plan = ex.run(plan)

        assert plan.done == 3
        # 无依赖时串行按计划顺序执行
        assert client.calls == ["a", "b", "c"], client.calls


# --------------------------------------------------------------------------- #
# 2. 隐式依赖（兼容性关键）
# --------------------------------------------------------------------------- #
class TestImplicitDependency:
    def test_template_reference_creates_a_dependency_edge(self) -> None:
        """只写模板、不写 ``depends_on``：仍必须先跑被引用的步骤。

        旧执行器是串行拓扑序，所以这种写法"碰巧能用"；并行之后若不把模板里隐含的
        依赖变成图边，两个步骤会同时开跑、模板取到空值。
        """

        def handler(skill: str) -> dict:
            if skill == "produce":
                return {"outputs": {"value": "42"}}
            return {"outputs": {"echo": "consumed"}}

        # 步骤 2 引用步骤 1 的输出，但**没有** depends_on
        plan = _plan(
            GoalStep(skill="produce", params={"value": "42"}),
            GoalStep(skill="consume", params={"n": "${step1.outputs.value}"}),
        )
        client = _FakeClient(handler)
        with _executor(client) as ex:
            plan = ex.run(plan)

        assert plan.done == 2, [(s.step_id, s.status, s.error) for s in plan.steps]
        # 客户端**收到**的 n 必须是解析后的值；若依赖没被推断出来、两步并行，
        # 这里会是未解析的 "${step1.outputs.value}"
        consume_params = dict(client.params)["consume"]
        assert consume_params["n"] == "42", (
            f"模板应在提交时解析出真实值，实际 {consume_params['n']!r}"
        )

    def test_explicit_and_implicit_dependencies_are_merged(self) -> None:
        """显式 ``depends_on`` 与模板推断出的依赖取并集，不互相覆盖。"""
        from geonexus.agent.planner import _effective_deps, _to_geotask

        plan = _plan(
            GoalStep(skill="a"),
            GoalStep(skill="b"),
            GoalStep(skill="c", depends_on=[2], params={"x": "${step1.outputs.v}"}),
        )
        by_id = {s.step_id: s for s in plan.steps}
        assert _effective_deps(by_id[3], by_id) == [1, 2]
        task = _to_geotask(by_id[3], by_id)
        assert task.depends_on == ["step-1", "step-2"]


# --------------------------------------------------------------------------- #
# 3. 失败语义不变
# --------------------------------------------------------------------------- #
class TestFailureSemanticsUnchanged:
    def test_failed_step_cascades_with_the_legacy_message(self) -> None:
        """依赖失败的步骤仍是 ``failed`` + ``a dependency step failed``。"""

        def handler(skill: str) -> dict:
            if skill == "boom":
                raise RuntimeError("上游炸了")
            return {"outputs": {"value": skill}}

        plan = _plan(
            GoalStep(skill="boom"),
            GoalStep(skill="middle", depends_on=[1]),
            GoalStep(skill="tail", depends_on=[2]),
        )
        with _executor(_FakeClient(handler)) as ex:
            plan = ex.run(plan)

        assert plan.failed == 3 and plan.done == 0
        assert plan.steps[0].status == "failed"
        assert "上游炸了" in (plan.steps[0].error or "")
        # 下面两步是被级联的，不是自己跑的——文案逐字保留旧实现
        assert plan.steps[1].status == "failed"
        assert plan.steps[1].error == "a dependency step failed"
        assert plan.steps[2].error == "a dependency step failed"

    def test_downstream_of_a_failure_never_executes(self) -> None:
        calls: list[str] = []

        def handler(skill: str) -> dict:
            calls.append(skill)
            if skill == "boom":
                raise RuntimeError("nope")
            return {"outputs": {"value": 1}}

        plan = _plan(
            GoalStep(skill="boom"),
            GoalStep(skill="child", depends_on=[1]),
        )
        with _executor(_FakeClient(handler)) as ex:
            plan = ex.run(plan)

        assert calls == ["boom"], f"下游不得被执行: {calls}"

    def test_sibling_failure_does_not_affect_independent_branch(self) -> None:
        """一个分支失败不影响另一个独立分支——并行带来的实际好处。"""

        def handler(skill: str) -> dict:
            if skill == "boom":
                raise RuntimeError("nope")
            return {"outputs": {"value": skill}}

        plan = _plan(
            GoalStep(skill="boom"),
            GoalStep(skill="healthy"),
            GoalStep(skill="dependent", depends_on=[1]),
        )
        with _executor(_FakeClient(handler)) as ex:
            plan = ex.run(plan)

        assert plan.steps[1].status == "done", "独立分支应当照常完成"
        assert plan.done == 1 and plan.failed == 2


# --------------------------------------------------------------------------- #
# 4. 反思执行器共用同一套执行
# --------------------------------------------------------------------------- #
class TestReflectiveUsesReadySet:
    def test_reflective_first_pass_is_parallel_too(self) -> None:
        """ReflectiveExecutor 的确定性第一遍与基类共用就绪集（因此也并行）。"""
        from geonexus.agent import ReflectiveExecutor

        barrier = threading.Barrier(3, timeout=5)

        def handler(skill: str) -> dict:
            barrier.wait()
            return {"outputs": {"value": skill}}

        plan = _plan(GoalStep(skill="a"), GoalStep(skill="b"), GoalStep(skill="c"))
        ex = ReflectiveExecutor("http://r:8790", reflector=None, max_reflections=1)  # type: ignore[arg-type]
        ex.client = _FakeClient(handler)  # type: ignore[assignment]
        try:
            plan = ex.run(plan)
        finally:
            ex.close()

        assert plan.done == 3, [(s.step_id, s.status, s.error) for s in plan.steps]
        assert plan.failed == 0

    def test_plan_projection_keeps_agent_vocabulary(self) -> None:
        """投影回 agent 的四值词汇表，而不是 GeoTask 的 ``succeeded``。"""
        client = _FakeClient()
        plan = _plan(GoalStep(skill="a"), GoalStep(skill="b"))
        with _executor(client) as ex:
            plan = ex.run(plan)

        statuses = {s.status for s in plan.steps}
        assert statuses == {"done"}, statuses
        assert "succeeded" not in statuses
