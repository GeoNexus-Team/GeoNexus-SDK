"""CAFE CentralServer —— 任务路由到最合适的 worker。

**这是全 SDK 唯一的下推路由实现。** 合并前有两套：``federation`` 按 GeoCard 归属
查表（已接线），``cafe`` 按 metadata 打分（没人用）。合并的收益是双向的——CAFE 拿到
了它缺的传输（见 :mod:`geonexus.cafe.remote`），federation 拿到了它缺的策略与租约。

## 路由策略（三个阶段，顺序有意义）

1. **硬过滤**：能不能执行这种载荷（:meth:`WorkerCapabilities.accepts`）、节点是否
   健康。不满足的直接出局——失败要在**路由阶段**带原因说明，而不是执行到一半报错。
2. **打分**：数据本地性（能解析多少输入）→ 算力是否够（复用
   :func:`geonexus.resource.match_resource`）→ region/compliance 标签。
3. **兜底**：剩下的第一个；都没有就返回"没有可用 worker"。

:class:`RoutingDecision` 会带上 ``reason`` 与 ``candidates``——"为什么选它"必须是
可回答的，否则多目标路由就是个黑盒。

## 租约与回收

``dispatch`` 在执行前登记租约（``task_id → worker``，带到期时间），执行完释放。
:meth:`reclaim_expired` 返回过期租约，让上层决定重试——这正是"杀掉一个 worker 后
任务能重试到健康节点"所依赖的机制（``dispatch`` 的 ``retries`` 参数直接用它）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..resource import ComputeCapability, match_resource, parse_memory
from .protocol import PushdownRequest, PushdownResult, TaskStatus

logger = logging.getLogger(__name__)

__all__ = ["CentralServer", "TaskRouter", "RoutingDecision", "Lease", "capacity_of"]

#: 失败后把 worker 标记为不可用的时长（秒）。短，因为多数失败是瞬时的。
DEFAULT_UNHEALTHY_TTL = 30.0


def capacity_of(worker: Any) -> ComputeCapability:
    """把一个 worker 广告出来的 ``tags`` 变成 :class:`ComputeCapability`。

    约定（与 :class:`~geonexus.cafe.worker.WorkerNode` 的 tags 文档一致）::

        {"cpu": 8, "memory": "16Gi", "gpu": "cuda", "region": "mekong"}

    内存字符串复用 :func:`geonexus.resource.parse_memory`（返回 GiB），不另写解析。
    """
    tags = getattr(worker, "tags", {}) or {}
    return ComputeCapability(
        node_url=getattr(worker, "worker_id", "?"),
        cpu=tags.get("cpu"),
        memory_gb=parse_memory(tags.get("memory")),
        gpu=tags.get("gpu"),
    )


@dataclass
class _RuntimeShim:
    cpu: Any = None
    memory: Any = None
    gpu: Any = None


@dataclass
class _RequestShim:
    """给 :func:`match_resource` 用的最小"模型卡"。

    它只读 ``card.id`` 与 ``card.runtime``，所以没必要造一张真 GeoCard——复用函数
    本身是为了复用它的**比较语义与理由文案**，而不是复用它读卡的方式。
    """

    id: str
    runtime: _RuntimeShim | None = None


@dataclass
class Lease:
    """一次下推的占用登记。"""

    task_id: str
    worker_id: str
    expires_at: float
    attempt: int = 0

    def expired(self, now: float | None = None) -> bool:
        return self.expires_at <= (time.time() if now is None else now)


@dataclass
class RoutingDecision:
    """Result of routing a task to workers."""

    chosen_worker: str | None
    candidates: list[str]
    reason: str
    #: 被硬过滤掉的 worker 及原因——"为什么没选它"同样要可回答
    excluded: dict[str, str] = field(default_factory=dict)


class TaskRouter:
    """Backward-compatible alias for CentralServer (router behaviour)."""

    def __init__(self, central: CentralServer) -> None:
        self.central = central

    def route(self, request: PushdownRequest) -> RoutingDecision:
        return self.central.route(request)


class CentralServer:
    """Central Server for CAFE pushdown tasks.

    Attributes:
        server_id: Unique identifier.
        workers: Dict of worker_id -> worker (local :class:`WorkerNode` or
            :class:`~geonexus.cafe.remote.RemoteWorker`).
    """

    def __init__(self, server_id: str = "central",
                 *, unhealthy_ttl: float = DEFAULT_UNHEALTHY_TTL) -> None:
        self.server_id = server_id
        self.workers: dict[str, Any] = {}
        self.unhealthy_ttl = unhealthy_ttl
        self._unhealthy_until: dict[str, float] = {}
        self._leases: dict[str, Lease] = {}

    # ------------------------------------------------------------------ #
    # Worker management
    # ------------------------------------------------------------------ #
    def register_worker(self, worker: Any) -> CentralServer:
        """Register a worker (local or remote) so tasks can be routed to it."""
        self.workers[worker.worker_id] = worker
        logger.info("Central registered worker %s (%d assets)",
                    worker.worker_id, len(getattr(worker, "data_assets", {})))
        return self

    def available_workers(self) -> list[Any]:
        return list(self.workers.values())

    def mark_unhealthy(self, worker_id: str, ttl: float | None = None) -> None:
        """把某个 worker 暂时踢出候选池（失败后调用）。

        这是"杀掉一个 worker 后任务重试到健康节点"的关键：失败不是终止条件，
        而是把该节点短期降级、换一个候选。
        """
        self._unhealthy_until[worker_id] = time.time() + (
            self.unhealthy_ttl if ttl is None else ttl)

    def is_healthy(self, worker_id: str, now: float | None = None) -> bool:
        until = self._unhealthy_until.get(worker_id)
        if until is None:
            return True
        if until <= (time.time() if now is None else now):
            self._unhealthy_until.pop(worker_id, None)   # TTL 过期，恢复正常
            return True
        return False

    def close(self) -> None:
        """关闭所有能关闭的 worker（远端持有 httpx 客户端）。"""
        for worker in self.workers.values():
            closer = getattr(worker, "close", None)
            if callable(closer):
                closer()

    # ------------------------------------------------------------------ #
    # Routing
    # ------------------------------------------------------------------ #
    def _capacity_ok(self, worker: Any,
                     request: PushdownRequest) -> tuple[bool, list[str]]:
        need = request.metadata.get("resources")
        if not need:
            return True, ["未声明资源需求"]
        shim = _RequestShim(
            id=request.task_id,
            runtime=_RuntimeShim(cpu=need.get("cpu"), memory=need.get("memory"),
                                 gpu=need.get("gpu")),
        )
        match = match_resource(shim, [capacity_of(worker)])
        return match.matched, match.reasons

    def route(self, request: PushdownRequest) -> RoutingDecision:
        """Choose the best worker for a pushdown request.

        Strategy：
        1. 硬过滤 —— 载荷类型兼容 + 健康（不可用/被降级的一律出局）
        2. 数据本地性 —— 能解析最多输入数据的优先
        3. 算力 —— 声明了 ``metadata["resources"]`` 时必须满足
        4. region / compliance 标签
        5. 兜底
        """
        region_hint = request.metadata.get("region")
        excluded: dict[str, str] = {}

        capable: list[Any] = []
        for worker in self.workers.values():
            caps = getattr(worker, "capabilities", None)
            if callable(caps) and not caps().accepts(request):
                excluded[worker.worker_id] = f"不支持 {request.payload_kind} 载荷"
                continue
            if not self.is_healthy(worker.worker_id):
                excluded[worker.worker_id] = "近期失败，暂时降级"
                continue
            capable.append(worker)

        if not capable:
            reason = "no worker available"
            if excluded:
                reason += "：" + "；".join(f"{k} {v}" for k, v in excluded.items())
            return RoutingDecision(None, [], reason, excluded)

        # 数据本地性 + 算力
        scored: list[tuple[int, str, list[str]]] = []
        for worker in capable:
            local = sum(1 for value in request.inputs.values()
                        if worker.resolve_input(value) is not None)
            ok, reasons = self._capacity_ok(worker, request)
            if not ok:
                excluded[worker.worker_id] = "算力不足：" + "；".join(reasons[:2])
                continue
            scored.append((local, worker.worker_id, reasons))

        if not scored:
            return RoutingDecision(
                None, [], "没有满足算力需求的 worker", excluded)

        scored.sort(key=lambda item: item[0], reverse=True)
        if scored[0][0] > 0:
            candidates = [wid for _, wid, _ in scored]
            return RoutingDecision(
                scored[0][1], candidates,
                f"data locality ({scored[0][0]} inputs local)", excluded)

        # Region / compliance 标签兜底
        region_workers = [wid for _, wid, _ in scored
                          if (self.workers[wid].tags or {}).get("region") == region_hint]
        if region_workers:
            others = [wid for _, wid, _ in scored if wid not in region_workers]
            return RoutingDecision(region_workers[0], region_workers + others,
                                   f"region tag {region_hint}", excluded)

        candidates = [wid for _, wid, _ in scored]
        return RoutingDecision(candidates[0], candidates,
                               "fallback (no locality hints)", excluded)

    # ------------------------------------------------------------------ #
    # Task dispatch
    # ------------------------------------------------------------------ #
    def dispatch(self, request: PushdownRequest, *,
                 retries: int | None = None,
                 lease_seconds: float = 300.0) -> PushdownResult:
        """Route and execute a pushdown task, retrying on other candidates.

        ``retries=None`` 表示"试完所有候选"——这正是运维要的语义：一个 worker 挂了，
        任务应该在健康节点上重来，而不是直接失败。

        只有 ``result.retryable`` 的失败才换节点：节点拒绝/连不上可以换，输入缺失
        换谁都没用。
        """
        attempted: set[str] = set()
        last: PushdownResult | None = None
        budget = len(self.workers) if retries is None else max(0, retries)

        for _ in range(budget + 1):
            decision = self._route_excluding(request, attempted)
            if decision.chosen_worker is None:
                break
            worker = self.workers[decision.chosen_worker]
            attempted.add(worker.worker_id)
            logger.info("Central dispatching %s -> %s (%s)",
                        request.task_id, worker.worker_id, decision.reason)
            self._leases[request.task_id] = Lease(
                task_id=request.task_id, worker_id=worker.worker_id,
                expires_at=time.time() + lease_seconds,
                attempt=len(attempted) - 1)
            result = worker.execute(request)
            self._leases.pop(request.task_id, None)
            if result.status is TaskStatus.SUCCEEDED:
                return result
            last = result
            if not result.retryable:
                return result
            self.mark_unhealthy(worker.worker_id)
            logger.warning("Worker %s failed %s (%s) -> trying another candidate",
                           worker.worker_id, request.task_id, result.error)

        if last is not None:
            # 所有候选都试过了：把"试过哪些"写进错误，便于定位
            last.error = f"{last.error}（已尝试 {sorted(attempted)}）"
            return last
        decision = self.route(request)
        return PushdownResult(
            task_id=request.task_id,
            status=TaskStatus.FAILED,
            error=decision.reason,
            retryable=False,
        )

    def _route_excluding(self, request: PushdownRequest,
                         attempted: set[str]) -> RoutingDecision:
        """路由，但跳过已经试过的 worker。"""
        if not attempted:
            return self.route(request)
        saved = {wid: self.workers.pop(wid) for wid in list(attempted)
                 if wid in self.workers}
        try:
            return self.route(request)
        finally:
            self.workers.update(saved)

    def dispatch_to(self, worker_id: str, request: PushdownRequest) -> PushdownResult:
        """Dispatch a task to an explicitly named worker."""
        worker = self.workers.get(worker_id)
        if worker is None:
            return PushdownResult(
                task_id=request.task_id,
                status=TaskStatus.FAILED,
                error=f"worker not found: {worker_id}",
            )
        return worker.execute(request)

    # ------------------------------------------------------------------ #
    # Leases
    # ------------------------------------------------------------------ #
    def active_leases(self) -> list[Lease]:
        return list(self._leases.values())

    def reclaim_expired(self, now: float | None = None) -> list[Lease]:
        """回收过期租约并返回它们。

        租约过期意味着"派下去了但没回来"——进程可能崩了、网络可能断了。返回给上层
        而不是自己重试：重试策略属于调度层（GeoTask），CAFE 只负责报告事实。
        """
        expired = [lease for lease in self._leases.values() if lease.expired(now)]
        for lease in expired:
            self._leases.pop(lease.task_id, None)
            logger.warning("Reclaiming expired lease %s (worker %s)",
                           lease.task_id, lease.worker_id)
        return expired

    # ------------------------------------------------------------------ #
    # Health & info
    # ------------------------------------------------------------------ #
    def health(self) -> dict[str, Any]:
        return {
            "central": self.server_id,
            "workers": [
                {
                    "worker_id": w.worker_id,
                    "assets": list(getattr(w, "data_assets", {}).keys()),
                    "tags": getattr(w, "tags", {}),
                    "healthy": self.is_healthy(w.worker_id),
                }
                for w in self.workers.values()
            ],
            "leases": [{"task_id": ls.task_id, "worker_id": ls.worker_id}
                       for ls in self._leases.values()],
        }
