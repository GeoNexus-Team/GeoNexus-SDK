"""CAFE 作为**唯一**下推引擎的路由、租约与故障转移测试（Phase 2 验收）。

合并前的状态是两套下推：``federation`` 有自己的 HTTP 传输（已接线），``cafe`` 有
``TaskRouter`` 但没人用。合并后传输、执行、租约归属 CAFE，federation 只保留"每张卡
归哪个节点"的归属分组。

这个文件钉住合并后的四条能力，其中第 2 条是路线图的验收原话：
1. 载荷类型与健康是**硬过滤**（不兼容/不健康的根本不进候选，且理由可读）；
2. **杀掉一个 worker 后任务重试到健康节点并成功**；
3. 租约在执行期间登记、可回收；
4. 全仓库只有一处下推实现（源码级断言，防止又长出一套）。
"""

from __future__ import annotations

import pathlib
import threading
import time

from test_registry import _start_server

from geonexus.cafe import (
    CentralServer,
    PushdownRequest,
    PushdownResult,
    RemoteWorker,
    TaskStatus,
    WorkerNode,
    capacity_of,
)
from geonexus.geocard import GeoCardBuilder
from geonexus.geonode import GeoNode


def _card(card_id: str):
    return (GeoCardBuilder(id=card_id, type="data", name=card_id, description="t")
            .spatial(bbox=[-73.9, -15.0, -44.0, 5.0], crs="EPSG:4326", resolution=10)
            .temporal(start="2015-01-01", end="2025-12-31")
            .capability("echo")
            .build())


def _echo_handler(params, context):
    return {"echo": params, "cards": [c.id for c in context.geocards],
            "ran_on": context.node_name if hasattr(context, "node_name") else "node"}


def _live_node(name: str, *, cards: list[str] | None = None):
    """起一个真 GeoNode，返回 (url, handle, node)。"""
    node = GeoNode(name=name, port=0)
    for card_id in cards or []:
        node.register_geocard(_card(card_id))
    node.register_skill(name="echo", handler=_echo_handler, input_schema={"required": []})
    handle = _start_server(node.create_app(), 0)
    return f"http://127.0.0.1:{handle.port}", handle, node


def _dead_url() -> str:
    """一个确定连不上的地址（先占端口再关掉，拿到一个空闲端口号）。"""
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


# --------------------------------------------------------------------------- #
# 1. 硬过滤
# --------------------------------------------------------------------------- #
class TestHardFilters:
    def test_payload_kind_is_filtered_before_scoring(self) -> None:
        """代码下推不进远端候选，技能调用不进本地候选——理由要写下来。"""
        central = CentralServer()
        central.register_worker(WorkerNode("local-1"))
        central.register_worker(RemoteWorker("http://127.0.0.1:1"))

        code_decision = central.route(PushdownRequest(code="result = 1"))
        assert code_decision.chosen_worker == "local-1"
        assert "http://127.0.0.1:1" in code_decision.excluded
        assert "不支持 code 载荷" in code_decision.excluded["http://127.0.0.1:1"]

        skill_decision = central.route(PushdownRequest(skill="echo"))
        assert skill_decision.chosen_worker == "http://127.0.0.1:1"
        assert "local-1" in skill_decision.excluded
        assert "不支持 skill 载荷" in skill_decision.excluded["local-1"]

    def test_unhealthy_worker_is_skipped(self) -> None:
        central = CentralServer()
        central.register_worker(WorkerNode("a"))
        central.register_worker(WorkerNode("b"))
        central.mark_unhealthy("a")

        decision = central.route(PushdownRequest(code="result = 1"))
        assert decision.chosen_worker == "b"
        assert decision.excluded["a"] == "近期失败，暂时降级"

        # TTL 过后自动恢复
        assert central.is_healthy("a", now=time.time() + 10_000) is True

    def test_insufficient_capacity_is_excluded_with_reasons(self) -> None:
        """声明了 metadata["resources"] 时算力必须满足，不够的出局并说明原因。"""
        central = CentralServer()
        central.register_worker(WorkerNode("cpu-only", tags={"region": "x"}))
        central.register_worker(WorkerNode("gpu-node", tags={"gpu": "cuda"}))

        request = PushdownRequest(code="result = 1",
                                 metadata={"resources": {"gpu": "cuda"}})
        decision = central.route(request)
        assert decision.chosen_worker == "gpu-node"
        assert "算力不足" in decision.excluded["cpu-only"]
        assert any("gpu" in reason for reason in [decision.excluded["cpu-only"]])

    def test_no_candidate_reports_every_exclusion(self) -> None:
        central = CentralServer()
        central.register_worker(WorkerNode("local-1"))
        decision = central.route(PushdownRequest(skill="echo"))
        assert decision.chosen_worker is None
        assert "不支持 skill 载荷" in decision.reason

    def test_capacity_of_reads_the_documented_tags(self) -> None:
        cap = capacity_of(WorkerNode("w", tags={"cpu": "8", "memory": "16Gi", "gpu": "cuda"}))
        assert cap.node_url == "w"
        assert cap.cpu == "8"
        assert cap.memory_gb == 16.0        # parse_memory 返回 GiB
        assert cap.gpu == "cuda"


# --------------------------------------------------------------------------- #
# 2. 验收：杀掉一个 worker，任务重试到健康节点并成功
# --------------------------------------------------------------------------- #
class TestFailoverToHealthyWorker:
    def test_failed_worker_falls_back_to_a_live_node(self) -> None:
        """路线图 Phase 2 的验收原话。

        布置：一个连不上的"死"worker 持有本地资产（因此数据本地性把它排在第一），
        一个真 GeoNode 什么资产都不持（排在后面）。第一个必然失败，且失败是
        ``retryable``（连不上），于是调度换点——任务应该落在活着的节点上并成功。
        """
        url, handle, _node = _live_node("alive-node")
        try:
            central = CentralServer()
            dead = RemoteWorker(_dead_url(), data_assets={"dataset-x": "remote-x"},
                                timeout=3.0)
            alive = RemoteWorker(url, timeout=10.0)
            central.register_worker(dead)
            central.register_worker(alive)

            request = PushdownRequest(skill="echo", params={"hello": "failover"},
                                      inputs={"data": "dataset-x"})

            # 先确认路由**确实**选中了那个死节点（否则这条测试什么都没验证）
            assert central.route(request).chosen_worker == dead.worker_id

            result = central.dispatch(request)

            assert result.status is TaskStatus.SUCCEEDED, result.error
            assert result.worker_id == url, "应当换到活着的节点"
            assert result.result["outputs"]["echo"] == {"hello": "failover"}
            # 死节点被短期降级，避免每次都先撞它
            assert central.is_healthy(dead.worker_id) is False
        finally:
            handle.stop()

    def test_non_retryable_failure_does_not_change_worker(self) -> None:
        """输入缺失换节点也没用：不该白试一轮，也不该把节点标记为不健康。"""
        central = CentralServer()
        bad = WorkerNode("code-only")            # 本地 worker 只认 code
        bad.execute = lambda req: __import__("geonexus.cafe", fromlist=["x"]).PushdownResult(  # type: ignore[assignment]
            task_id=req.task_id, status=TaskStatus.FAILED,
            error="cannot resolve inputs", retryable=False)
        central.register_worker(bad)

        result = central.dispatch(PushdownRequest(code="result = 1"))
        assert result.status is TaskStatus.FAILED
        assert central.is_healthy("code-only") is True, "不可重试的失败不该降级节点"

    def test_all_candidates_exhausted_reports_what_was_tried(self) -> None:
        central = CentralServer(unhealthy_ttl=0.0)
        for wid in ("a", "b"):
            worker = WorkerNode(wid)
            worker.execute = (  # type: ignore[assignment]
                lambda req, _w=wid: __import__("geonexus.cafe", fromlist=["x"]).PushdownResult(
                    task_id=req.task_id, status=TaskStatus.FAILED,
                    error=f"{_w} down", retryable=True))
            central.register_worker(worker)

        result = central.dispatch(PushdownRequest(code="result = 1"))
        assert result.status is TaskStatus.FAILED
        assert "已尝试" in (result.error or "")
        assert "a" in result.error and "b" in result.error


# --------------------------------------------------------------------------- #
# 3. 租约与回收
# --------------------------------------------------------------------------- #
class TestLeases:
    def test_lease_is_held_while_executing_and_released_after(self) -> None:
        gate = threading.Event()
        central = CentralServer()
        worker = WorkerNode("slow")
        def blocking_execute(req):        # noqa: ANN001, ANN202
            gate.wait(10)                 # 注意：wait() 返回 True，不能用 `or` 串起来
            return PushdownResult(task_id=req.task_id, status=TaskStatus.SUCCEEDED,
                                  result={"ok": True}, worker_id="slow")

        worker.execute = blocking_execute  # type: ignore[assignment]
        central.register_worker(worker)

        done: list[object] = []
        thread = threading.Thread(
            target=lambda: done.append(
                central.dispatch(PushdownRequest(code="result = 1", task_id="t-lease"),
                                 lease_seconds=30.0)))
        thread.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline and not central.active_leases():
                time.sleep(0.01)
            leases = central.active_leases()
            assert [ls.task_id for ls in leases] == ["t-lease"]
            assert leases[0].worker_id == "slow"
            assert leases[0].expired() is False
        finally:
            gate.set()
            thread.join(timeout=10)

        assert not central.active_leases(), "执行结束后租约必须释放"
        assert central.reclaim_expired() == []
        assert done and done[0].status is TaskStatus.SUCCEEDED

    def test_expired_lease_is_reclaimable(self) -> None:
        central = CentralServer()
        central.register_worker(WorkerNode("w"))
        # 直接构造一个过期租约（模拟"派下去但进程崩了"）
        from geonexus.cafe import Lease

        central._leases["t-lost"] = Lease(task_id="t-lost", worker_id="w",
                                          expires_at=time.time() - 1)
        reclaimed = central.reclaim_expired()
        assert [ls.task_id for ls in reclaimed] == ["t-lost"]
        assert central.active_leases() == []
        assert central.reclaim_expired() == []      # 幂等


# --------------------------------------------------------------------------- #
# 4. 只有一处下推实现
# --------------------------------------------------------------------------- #
class TestSinglePushdownImplementation:
    """路线图验收："全仓库只有一处下推实现"。

    这两条断言**用 AST 而不是字符串搜索**。第一版用字符串搜索，结果被文档字符串里
    的解释性文字绊倒（"httpx.Client 可并发使用"、"Bridges GeoMCP execution to OGC"
    这类句子），报出两个假阳性。按代码里的标识符判断才说明问题。
    """

    def test_federation_has_no_transport_of_its_own(self) -> None:
        import ast

        src = pathlib.Path("src/geonexus/federation/client.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        bases = {node.value.id for node in ast.walk(tree)
                 if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)}
        funcs = {node.name for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

        assert "GeoMCPClient" not in names, "federation 又自己持有了 GeoMCP 客户端"
        assert "httpx" not in names and "httpx" not in bases, "federation 又直接用了 httpx"
        assert "_node_client" not in funcs, "federation 又长出了自己的传输层"
        # 传输必须来自 CAFE
        assert "RemoteWorker" in names and "CentralServer" in names
        assert "dispatch_to" in attrs

    def test_only_cafe_holds_a_pushdown_client(self) -> None:
        """谁可以 import GeoMCPClient——白名单即"下推客户端"的边界。

        白名单是**实测出来的精确集合**，不是凭印象列的：

        * ``cafe/remote.py`` —— 唯一的**下推客户端**（RemoteWorker）
        * ``geomcp/`` —— 协议本身的实现（client/server）
        * ``web/router.py``、``cli.py`` —— 面向人的入口，直接连单个节点

        注意 ``adapters/`` 不在名单里：``ogc_exec.py`` 桥的是 **OGC API - Processes**，
        是另一种协议；``oge_skill.py`` 是技能**提供方**。两者都在文档字符串里提到
        GeoMCP，但都不持有 GeoMCP 客户端——第一版用字符串搜索时正是被这些说明文字
        误报的。``geonode/`` 也不在：它是被下推的服务端，用的是内部的 server 对象。
        """
        import ast

        allowed = {
            "cafe/remote.py",
            "geomcp/__init__.py", "geomcp/server.py", "geomcp/client.py",
            "web/router.py", "cli.py",
        }
        holders: set[str] = set()
        for path in pathlib.Path("src/geonexus").rglob("*.py"):
            rel = str(path.relative_to("src/geonexus"))
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and \
                        any(a.name == "GeoMCPClient" for a in node.names) or isinstance(node, ast.Import) and \
                        any(a.name.endswith("GeoMCPClient") for a in node.names):
                    holders.add(rel)

        unexpected = holders - allowed
        assert not unexpected, f"这些模块新持有了下推客户端（应走 CAFE）: {sorted(unexpected)}"
        assert "cafe/remote.py" in holders, "CAFE 应当持有下推客户端"

    def test_worker_killed_after_a_successful_run_fails_over(self) -> None:
        """字面意义的"杀掉一个 worker"：先成功跑一次，再把它停掉，然后重试。

        与上一条（端口从未监听）的区别在于：这里节点**曾经健康**，它的失败是
        "原来好好的、现在没了"——真实故障的样子（进程崩了、机器下线）。
        """
        url_a, handle_a, _ = _live_node("node-a")
        url_b, handle_b, _ = _live_node("node-b")
        try:
            central = CentralServer()
            a = RemoteWorker(url_a, data_assets={"dataset-x": "x"}, timeout=3.0)
            b = RemoteWorker(url_b, timeout=10.0)
            central.register_worker(a)
            central.register_worker(b)
            request = PushdownRequest(skill="echo", params={"run": 1},
                                      inputs={"data": "dataset-x"})

            first = central.dispatch(request)
            assert first.status is TaskStatus.SUCCEEDED
            assert first.worker_id == url_a, "第一次应落在持有数据的 node-a"

            handle_a.stop()                      # ← 杀掉它
            time.sleep(0.2)

            second = central.dispatch(
                PushdownRequest(skill="echo", params={"run": 2},
                                inputs={"data": "dataset-x"}))
            assert second.status is TaskStatus.SUCCEEDED, second.error
            assert second.worker_id == url_b, "第二次应换到还活着的 node-b"
            assert second.result["outputs"]["echo"] == {"run": 2}
        finally:
            handle_b.stop()
