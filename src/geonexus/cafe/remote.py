"""CAFE RemoteWorker —— 把 GeoNode 当作远端 worker 的下推传输。

CAFE 原来只有进程内 :class:`~geonexus.cafe.worker.WorkerNode`（把代码下推到本地
沙箱子进程）。而"计算移动到数据"在真实部署里跨网络，所以需要一个走 HTTP 的
worker。这个模块就是把原本只存在于 ``geonexus.federation`` 里的那段传输搬进
CAFE——合并的收益是双向的：**CAFE 得到它缺的传输，federation 得到它缺的路由与租约**。

与本地 worker 的分工由 :meth:`capabilities` 声明：

======================  ==========================  ============================
worker                  能执行的载荷                执行方式
======================  ==========================  ============================
``WorkerNode``（本地）  ``code``                   当前解释器的子进程沙箱
``RemoteWorker``（远端）``skill``                   GeoMCP ``geo.execute`` over HTTP
======================  ==========================  ============================

router 在选点前按这个声明做**硬过滤**，所以不会把代码任务派给只会跑技能的远端
（反之亦然）——失败发生在路由阶段并带明确原因，而不是在执行到一半时报错。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from ..geomcp import GeoMCPClient, GeoMCPClientError
from .protocol import PushdownRequest, PushdownResult, TaskStatus

logger = logging.getLogger(__name__)

__all__ = ["RemoteWorker", "WorkerCapabilities"]


@dataclass(frozen=True)
class WorkerCapabilities:
    """一个 worker 能接什么活。router 用它做硬过滤。"""

    payload_kinds: frozenset[str]
    #: 能兑现的资源维度（"cpu" / "memory" / "gpu"）
    resource_dims: tuple[str, ...] = ()
    supports_cancel: bool = False

    def accepts(self, request: PushdownRequest) -> bool:
        return request.payload_kind in self.payload_kinds


class RemoteWorker:
    """一个远端 GeoNode，按 CAFE worker 的接口使用。

    刻意与 :class:`WorkerNode` 保持**同名接口**（``worker_id`` / ``data_assets`` /
    ``tags`` / ``resolve_input`` / ``execute``），这样 ``CentralServer`` 不需要
    为两种传输写两套路由逻辑——它只管 worker 的接口，不管背后是子进程还是 HTTP。

    Args:
        node_url: GeoNode 基址（同时用作 ``worker_id``）。
        data_assets: 该节点拥有的资产名（通常是 GeoCard id）→ 远端标识。
            有它数据本地性打分才有意义——这正是 federation 的归属查表在做的事。
        tags: 广告出来的能力，如 ``{"region": "mekong", "cpu": 8, "memory": "16Gi"}``。
        timeout: 单次调用超时（秒）。
        api_key: 节点要求 ``X-API-Key`` 时使用。
    """

    def __init__(
        self,
        node_url: str,
        data_assets: dict[str, str] | None = None,
        tags: dict[str, Any] | None = None,
        timeout: float = 30.0,
        api_key: str | None = None,
    ) -> None:
        self.node_url = node_url.rstrip("/")
        self.worker_id = self.node_url
        self.data_assets: dict[str, str] = dict(data_assets or {})
        self.tags: dict[str, Any] = dict(tags or {})
        self.timeout = timeout
        self.api_key = api_key
        self._client: GeoMCPClient | None = None
        # 加锁是因为并行执行：两个线程同时首次访问会各建一个客户端并泄漏其一
        # （httpx.Client 持有连接池）。锁只保护这个缓存，不串行化请求本身。
        self._lock = threading.Lock()
        self._last_seen: float | None = None
        self._jobs: dict[str, PushdownResult] = {}

    # ------------------------------------------------------------------ #
    def capabilities(self) -> WorkerCapabilities:
        return WorkerCapabilities(
            payload_kinds=frozenset({"skill"}),
            resource_dims=("cpu", "memory", "gpu"),
            supports_cancel=False,   # GeoMCP 的 execute 是同步调用，没有取消入口
        )

    def register_asset(self, name: str, remote_ref: str | None = None) -> RemoteWorker:
        self.data_assets[name] = remote_ref or name
        return self

    def resolve_input(self, name: str) -> str | None:
        """远端节点自己解析输入；这里只回答"这个资产归不归它"。

        返回非 None 表示"归它"，用于数据本地性打分；具体路径由节点解析。
        """
        return self.data_assets.get(name)

    # ------------------------------------------------------------------ #
    def _connection(self) -> GeoMCPClient:
        with self._lock:
            if self._client is None:
                self._client = GeoMCPClient(
                    self.node_url, timeout=self.timeout, api_key=self.api_key)
            return self._client

    def heartbeat(self) -> bool:
        """探一次节点健康；同时刷新 ``last_seen`` 供租约判断使用。"""
        try:
            self._connection().health()
        except GeoMCPClientError as exc:
            logger.warning("Remote worker %s heartbeat failed: %s", self.worker_id, exc)
            return False
        self._last_seen = time.time()
        return True

    @property
    def last_seen(self) -> float | None:
        return self._last_seen

    # ------------------------------------------------------------------ #
    def execute(self, request: PushdownRequest) -> PushdownResult:
        """在远端节点上执行技能调用。

        失败也**返回** ``PushdownResult`` 而不是抛异常：路由层需要区分"这个 worker
        不行、换一个"和"整件事做不成"，把判断权交给调用方。``retryable`` 就是那个
        信号。
        """
        start = time.time()
        if request.payload_kind != "skill":
            return self._fail(request, start,
                              f"远端 worker {self.worker_id} 只能执行技能调用，"
                              f"收到的是代码下推（code payload）",
                              retryable=False)
        try:
            result = self._connection().execute(
                skill=request.skill or "",
                params=request.params,
                geocards=request.geocards or None,
                spatial=request.spatial,
                temporal=request.temporal,
                request_id=request.task_id,
            )
        except GeoMCPClientError as exc:
            # 节点明确拒绝（业务错误码）与连不上（传输错误）都算"这个 worker 这次
            # 不行"——换一个节点是合理的；但输入缺失这种错换节点也没用，靠 code 区分。
            code = getattr(exc, "code", None)
            return self._fail(request, start, str(exc), code=code,
                              retryable=code is None or code >= 1000)
        self._last_seen = time.time()
        outcome = PushdownResult(
            task_id=request.task_id,
            status=TaskStatus.SUCCEEDED,
            result=result,
            worker_id=self.worker_id,
            elapsed_seconds=time.time() - start,
        )
        self._jobs[request.task_id] = outcome
        return outcome

    def _fail(self, request: PushdownRequest, start: float, error: str,
              *, code: int | None = None, retryable: bool) -> PushdownResult:
        outcome = PushdownResult(
            task_id=request.task_id,
            status=TaskStatus.FAILED,
            error=error,
            worker_id=self.worker_id,
            elapsed_seconds=time.time() - start,
            error_code=code,
            retryable=retryable,
        )
        self._jobs[request.task_id] = outcome
        return outcome

    def get_job(self, task_id: str) -> PushdownResult | None:
        return self._jobs.get(task_id)

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None
