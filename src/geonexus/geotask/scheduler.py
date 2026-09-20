"""算力调度与占用管理 —— 契约 §2.2 / §5.2，路线图 Phase 4。

## 为什么需要它

没有调度器时 ``TaskGraph.ready()`` 返回什么就跑什么：N 个并发请求会**自由竞争**
同一份 CPU / 内存 / 显存，``running`` 的个数与节点真实容量毫无关系——"能起"变成了
"线程池愿意起"。P1 的并发测试证明了"能并行"，代价是"不设上限"。

## 三类判定必须分开

混在一起是运维事故的常见来源，所以这里刻意分成三条路径：

===============================  ==========================================
情况                              结果
===============================  ==========================================
需求 > 节点**总**容量（装不下）      直接 ``no_compatible_compute`` 拒绝，急早失败
空闲容量不足（暂时没空）            ``blocked`` + 理由，等别人释放后重新准入
能装下且有空                      发租约、扣预约、交给后端
===============================  ==========================================

第一种如果也按"排队"处理，就会永远排队——一个要求 64GiB 的任务排在只有 16GiB 的
节点上，看起来是"资源紧张"，实际是**部署错误**。

## 记账口径

按**预约**之和记，不记实际用量（契约 §2.2）。因此"占用记账 == 实际并发"是可以
断言的：``Scheduler.occupancy_held()`` 应该恰好等于所有 RUNNING 任务的预约之和。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .model import GeoTask, ResourceRequirement

__all__ = [
    "Reservation",
    "Capacity",
    "Occupancy",
    "AdmissionResult",
    "Scheduler",
]

logger = logging.getLogger(__name__)

#: 准入拒绝的原因码（进事件流，便于按原因统计）
REASON_IMPOSSIBLE = "no_compatible_compute"
REASON_RESOURCE = "resource_unavailable"
REASON_CONCURRENCY = "concurrency_limit"
REASON_BACKOFF = "backoff"


@dataclass(frozen=True)
class Reservation:
    """一份预约（也是容量与占用的通用表示）。"""

    cpu_cores: float = 0.0
    memory_mb: int = 0
    gpu_count: int = 0
    gpu_kind: str | None = None

    @classmethod
    def of(cls, requirement: ResourceRequirement | None) -> Reservation:
        if requirement is None:
            return cls()
        gpu = requirement.gpu
        return cls(
            cpu_cores=float(requirement.cpu or 0.0),
            memory_mb=int(requirement.memory_mb or 0),
            gpu_count=int(getattr(gpu, "count", 0) or 0),
            gpu_kind=(str(gpu.kind).lower() if gpu is not None and gpu.kind else None),
        )

    @classmethod
    def default_capacity(cls) -> Reservation:
        """本机容量。

        内存用 ``os.sysconf`` 读物理内存——不引入 psutil 这种重量级依赖，
        而且调度只需要一个**上界**：真正的约束来自部署方显式传入的容量。
        """
        cpus = float(os.cpu_count() or 1)
        total_mb = 0
        try:
            total_mb = int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 * 1024))
        except (ValueError, OSError, AttributeError):  # pragma: no cover - 平台差异
            logger.warning(
                "无法读取物理内存，容量按 0 处理（宁少放行，不多放行）；请显式传入 capacity"
            )
        # gpu_count 保持 0：不能凭猜测把 GPU 任务放行到一台可能没有 GPU 的机器上
        return cls(cpu_cores=cpus, memory_mb=total_mb, gpu_count=0, gpu_kind=None)

    def fits_within(self, other: Reservation) -> list[str]:
        """本预约能否装在 ``other`` 里；返回装不下的**理由**（空 = 能装）。

        **``0`` 就是 0，不代表"未知"**。这一点是刻意的，也是踩过的坑：最早写成
        ``need > capacity > 0``（"容量为 0 视为未知"），结果容量用尽时剩余为 0，
        于是所有任务都被判定"装得下"——调度器形同虚设，N 个任务照旧自由竞争。
        资源调度器宁可"少放行并说明理由"，也不能"多放行且不吭声"。
        """
        reasons: list[str] = []
        if self.cpu_cores > other.cpu_cores:
            reasons.append(f"cpu: 需要 {self.cpu_cores:g} 核，可用 {other.cpu_cores:g} 核")
        if self.memory_mb > other.memory_mb:
            reasons.append(f"memory: 需要 {self.memory_mb}MB，可用 {other.memory_mb}MB")
        if self.gpu_count > other.gpu_count:
            reasons.append(f"gpu: 需要 {self.gpu_count} 张，可用 {other.gpu_count} 张")
        elif (
            self.gpu_count
            and other.gpu_count
            and self.gpu_kind
            and other.gpu_kind
            and self.gpu_kind != other.gpu_kind
        ):
            reasons.append(f"gpu: 需要 {self.gpu_kind}，可用的是 {other.gpu_kind}")
        return reasons

    def after_using(self, used: Reservation) -> Reservation:
        """扣除 ``used`` 后的剩余（下限为 0）。"""
        return Reservation(
            cpu_cores=max(0.0, self.cpu_cores - used.cpu_cores),
            memory_mb=max(0, self.memory_mb - used.memory_mb),
            gpu_count=max(0, self.gpu_count - used.gpu_count),
            gpu_kind=self.gpu_kind,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu_cores": self.cpu_cores,
            "memory_mb": self.memory_mb,
            "gpu_count": self.gpu_count,
            "gpu_kind": self.gpu_kind,
        }


#: 语义别名：容量与预约是同一个数据结构。
Capacity = Reservation


class Occupancy:
    """当前占用（预约之和）。**这是唯一的账本**，调度器不另记一份。"""

    def __init__(self, capacity: Reservation | None = None) -> None:
        self.capacity = capacity or Reservation.default_capacity()
        self._held: dict[str, Reservation] = {}

    # ------------------------------------------------------------------ #
    def reserve(self, task_id: str, reservation: Reservation) -> None:
        self._held[task_id] = reservation

    def release(self, task_id: str) -> Reservation | None:
        return self._held.pop(task_id, None)

    def holds(self, task_id: str) -> bool:
        return task_id in self._held

    def held(self) -> Reservation:
        cpu = sum(r.cpu_cores for r in self._held.values())
        mem = sum(r.memory_mb for r in self._held.values())
        gpu = sum(r.gpu_count for r in self._held.values())
        kinds = {r.gpu_kind for r in self._held.values() if r.gpu_count and r.gpu_kind}
        return Reservation(
            cpu_cores=cpu,
            memory_mb=mem,
            gpu_count=gpu,
            gpu_kind=(sorted(kinds)[0] if len(kinds) == 1 else None),
        )

    def available(self) -> Reservation:
        return self.capacity.after_using(self.held())

    @property
    def active(self) -> int:
        return len(self._held)

    def detail(self) -> dict[str, Any]:
        held = self.held()
        return {
            "capacity": self.capacity.to_dict(),
            "held": held.to_dict(),
            "available": self.available().to_dict(),
            "active": self.active,
        }


@dataclass
class AdmissionResult:
    """一次准入的结论。三分类，不是"能跑/不能跑"。"""

    admitted: list[str] = field(default_factory=list)
    blocked: dict[str, list[str]] = field(default_factory=dict)
    rejected: dict[str, list[str]] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.admitted or self.blocked or self.rejected)


class Scheduler:
    """按容量、优先级与公平性决定"现在起哪些"。

    Args:
        capacity: 本节点可用容量；默认取本机 CPU 与物理内存。
        max_concurrent: 并发**任务数**上限（与容量记账正交，两个都要满足）。
        allow_gpu_oversubscribe: 显存/卡数不足时是否仍允许起（默认否）。
    """

    def __init__(
        self,
        capacity: Reservation | None = None,
        *,
        max_concurrent: int | None = None,
    ) -> None:
        self.occupancy = Occupancy(capacity)
        self.max_concurrent = max_concurrent
        #: 入队序号：同一优先级内"先到先服务"的依据，跨轮次稳定
        self._seq: dict[str, int] = {}
        self._next_seq = 0
        #: 每个优先级一个属主轮转游标 —— 公平性的实现细节
        self._cursor: dict[int, int] = {}
        #: task_id → 最早可再次准入的时间（退避）
        self._not_before: dict[str, float] = {}
        #: 观测面：最近一次被拒/被卡的理由
        self.blocked_reasons: dict[str, list[str]] = {}
        self.rejected_reasons: dict[str, list[str]] = {}
        self.rejected_codes: dict[str, str] = {}
        #: 峰值并发与峰值占用（用于"记账 == 实际并发"的断言）
        self.peak_parallel = 0
        self.peak_held = Reservation()
        #: 峰值队列深度：某一刻"想跑但没跑上"的任务数
        self.peak_queue_depth = 0

    # ------------------------------------------------------------------ #
    # 队列
    # ------------------------------------------------------------------ #
    def enqueue_seq(self, task_id: str) -> int:
        """第一次见到就分配序号；之后不变（重试不会插队）。"""
        if task_id not in self._seq:
            self._seq[task_id] = self._next_seq
            self._next_seq += 1
        return self._seq[task_id]

    @staticmethod
    def owner_of(task: GeoTask) -> str:
        return task.owner or task.tenant or "_default"

    def _fair_order(self, candidates: list[GeoTask]) -> list[GeoTask]:
        """优先级降序，层内**按属主轮转**。

        只看优先级的话，一个提交了 100 个任务的属主会把同优先级的其他属主饿死。
        轮转保证：同一优先级下，各属主轮流各取一个。
        """
        out: list[GeoTask] = []
        by_priority: dict[int, list[GeoTask]] = {}
        for task in candidates:
            self.enqueue_seq(task.task_id)
            by_priority.setdefault(int(task.priority), []).append(task)

        for priority in sorted(by_priority, reverse=True):
            groups: dict[str, list[GeoTask]] = {}
            for task in by_priority[priority]:
                groups.setdefault(self.owner_of(task), []).append(task)
            for items in groups.values():
                items.sort(key=lambda t: self._seq[t.task_id])
            owners = sorted(groups, key=lambda o: self._seq[groups[o][0].task_id])
            start = self._cursor.get(priority, 0) % len(owners)
            rotated = owners[start:] + owners[:start]
            self._cursor[priority] = (start + 1) % len(owners)

            index = 0
            while any(groups[owner] for owner in rotated):
                owner = rotated[index % len(rotated)]
                if groups[owner]:
                    out.append(groups[owner].pop(0))
                index += 1
        return out

    # ------------------------------------------------------------------ #
    # 退避
    # ------------------------------------------------------------------ #
    def defer(self, task_id: str, seconds: float) -> None:
        """把任务压到 ``seconds`` 秒之后才允许再次准入。"""
        if seconds > 0:
            self._not_before[task_id] = time.time() + seconds

    def next_wakeup(self, now: float | None = None) -> float | None:
        """还要等多久才有任务解禁（用于让执行器睡着而不是空转）。"""
        now = time.time() if now is None else now
        pending = [t - now for t in self._not_before.values() if t > now]
        return min(pending) if pending else None

    def forget(self, task_id: str) -> None:
        """任务终态后清理它的调度痕迹（退避、理由）。"""
        self._not_before.pop(task_id, None)
        self.blocked_reasons.pop(task_id, None)
        self.rejected_reasons.pop(task_id, None)
        self.rejected_codes.pop(task_id, None)

    # ------------------------------------------------------------------ #
    # 准入
    # ------------------------------------------------------------------ #
    def _rejection_for(self, task: GeoTask) -> tuple[str, list[str]] | None:
        """装不下就早退：返回 (原因码, 理由)。"""
        need = Reservation.of(task.resources)
        reasons = need.fits_within(self.occupancy.capacity)
        if reasons:
            return REASON_IMPOSSIBLE, reasons
        return None

    def admit(self, ready: list[GeoTask], *, now: float | None = None) -> AdmissionResult:
        """对就绪集做准入。

        ``ready`` 是 ``TaskGraph.ready()`` 的候选（已满足依赖、当前可跑）。
        返回**分类**结果而不是一个布尔集合——调用方需要知道"为什么没起"。
        """
        now = time.time() if now is None else now
        result = AdmissionResult()
        candidates: list[GeoTask] = []

        for task in ready:
            rejection = self._rejection_for(task)
            if rejection is not None:
                code, reasons = rejection
                result.rejected[task.task_id] = reasons
                result.reasons[task.task_id] = code
                self.rejected_reasons[task.task_id] = reasons
                self.rejected_codes[task.task_id] = code
                continue
            if self._not_before.get(task.task_id, 0.0) > now:
                wait = self._not_before[task.task_id] - now
                result.blocked[task.task_id] = [f"退避中，{wait:.2f}s 后可重试"]
                result.reasons[task.task_id] = REASON_BACKOFF
            else:
                candidates.append(task)

        limit = self.max_concurrent
        running = self.occupancy.active
        for task in self._fair_order(candidates):
            if limit is not None and running >= limit:
                result.blocked[task.task_id] = [f"并发上限 {limit} 已达到（当前 {running}）"]
                result.reasons[task.task_id] = REASON_CONCURRENCY
                continue
            need = Reservation.of(task.resources)
            # 用**剩余**容量判：已被本轮前几个任务扣掉的要算进去
            reasons = need.fits_within(self.occupancy.available())
            if reasons:
                result.blocked[task.task_id] = reasons
                result.reasons[task.task_id] = REASON_RESOURCE
                continue
            self.occupancy.reserve(task.task_id, need)
            running += 1
            result.admitted.append(task.task_id)

        for task_id, reasons in result.blocked.items():
            self.blocked_reasons[task_id] = reasons
        # 队列深度 = 这一刻"想跑但没跑上"的任务数（被拒的已终结，不算排队）
        self.peak_queue_depth = max(self.peak_queue_depth, len(result.blocked))
        return result

    def release(self, task_id: str) -> None:
        """任务离开执行态时释放预约。**必须**调用，否则容量会泄漏。"""
        self.occupancy.release(task_id)

    def note_peak(self) -> None:
        self.peak_parallel = max(self.peak_parallel, self.occupancy.active)
        held = self.occupancy.held()
        # 峰值占用按"任一维度创了新高"记整份预约快照——这样断言时能直接比对
        if (
            held.cpu_cores > self.peak_held.cpu_cores
            or held.memory_mb > self.peak_held.memory_mb
            or held.gpu_count > self.peak_held.gpu_count
        ):
            self.peak_held = held

    # ------------------------------------------------------------------ #
    # 观测
    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, Any]:
        return {
            "max_concurrent": self.max_concurrent,
            "peak_parallel": self.peak_parallel,
            "peak_queue_depth": self.peak_queue_depth,
            "peak_held": self.peak_held.to_dict(),
            "blocked": {k: list(v) for k, v in self.blocked_reasons.items()},
            "rejected": {k: list(v) for k, v in self.rejected_reasons.items()},
            **self.occupancy.detail(),
        }

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        held = self.occupancy.held()
        return (
            f"<Scheduler active={self.occupancy.active} "
            f"cpu={held.cpu_cores:g}/{self.occupancy.capacity.cpu_cores:g} "
            f"mem={held.memory_mb}/{self.occupancy.capacity.memory_mb}MB>"
        )
