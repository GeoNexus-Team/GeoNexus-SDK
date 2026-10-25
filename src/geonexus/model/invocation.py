"""模型调用的类型、输入输出校验，以及"门禁 → 选 provider → 调用"的主流程。"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..resource import ComputeCapability, match_resource, resolve_compute_capabilities

logger = logging.getLogger(__name__)

__all__ = [
    "ModelInvocation",
    "ModelOutcome",
    "ModelProvider",
    "ModelError",
    "invoke_model",
    "choose_provider",
    "validate_inputs",
    "validate_outputs",
    "NO_COMPUTE",
    "INPUT_MISSING",
    "OUTPUT_MISMATCH",
    "PROVIDER_ERROR",
]

#: 错误码（与 GeoTask 契约 §5.1 的错误码表同名，便于跨层统计）
NO_COMPUTE = "no_compatible_compute"
INPUT_MISSING = "input_missing"
OUTPUT_MISMATCH = "invalid_spec"
PROVIDER_ERROR = "execution_error"


class ModelError(Exception):
    """模型调用无法完成（配置/协议层面的错误）。"""


@dataclass
class ModelInvocation:
    """一次模型调用请求。"""

    model_id: str
    inputs: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    timeout: float = 300.0
    request_id: str = field(default_factory=lambda: f"invoke-{uuid.uuid4().hex[:12]}")
    #: 算力门禁的结果——provider 与观测面都要能看到"为什么是它"
    compute_node: str | None = None
    match_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "inputs": self.inputs,
            "params": self.params,
            "timeout": self.timeout,
            "request_id": self.request_id,
            "compute_node": self.compute_node,
            "match_reasons": self.match_reasons,
        }


@dataclass
class ModelOutcome:
    """一次模型调用的结果。失败也返回它，而不是抛异常。

    理由与 CAFE 的 ``PushdownResult`` 相同：上层（调度器 / 任务模型）要区分"换一个
    执行者"和"整件事做不成"，而这个判断需要错误码与可重试性。
    """

    status: str = "succeeded"  # succeeded | failed
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    error_code: str | None = None
    retryable: bool = False
    provider: str | None = None
    model_id: str | None = None
    #: 算力门禁选中的节点（**真的**被打到的那台）。没有过门禁时为 ``None``。
    #: 记在结果上而不是只留在 invocation 里：调用方要能回答"它到底跑在哪"。
    compute_node: str | None = None
    request_id: str | None = None
    elapsed_seconds: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"

    @classmethod
    def failure(
        cls, code: str, message: str, *, retryable: bool = False, reasons: list[str] | None = None
    ) -> ModelOutcome:
        return cls(
            status="failed",
            error=message,
            error_code=code,
            retryable=retryable,
            reasons=list(reasons or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "outputs": self.outputs,
            "error": self.error,
            "error_code": self.error_code,
            "retryable": self.retryable,
            "provider": self.provider,
            "model_id": self.model_id,
            "compute_node": self.compute_node,
            "request_id": self.request_id,
            "elapsed_seconds": round(self.elapsed_seconds, 4),
            "reasons": self.reasons,
        }


@runtime_checkable
class ModelProvider(Protocol):
    """模型执行后端：一次请求一次响应，或提交后轮询。"""

    name: str

    def capabilities(self) -> dict[str, Any]: ...
    def invoke(self, invocation: ModelInvocation) -> ModelOutcome: ...


# --------------------------------------------------------------------------- #
# 输入输出校验
# --------------------------------------------------------------------------- #
def _io_names(card: Any) -> tuple[tuple[set[str], set[str]], tuple[set[str], set[str]]]:
    """取 ``card.inputs`` / ``card.outputs`` 的（名字集合, 必填集合）。"""

    def split(fields: Any) -> tuple[set[str], set[str]]:
        names: set[str] = set()
        required: set[str] = set()
        for item in fields or []:
            name = getattr(item, "name", None)
            if name is None and isinstance(item, dict):
                name = item.get("name")
            if not name:
                continue
            names.add(str(name))
            is_required = getattr(item, "required", False)
            if isinstance(item, dict):
                is_required = item.get("required", False)
            if is_required:
                required.add(str(name))
        return names, required

    return split(card.inputs), split(card.outputs)


def validate_inputs(card: Any, inputs: dict[str, Any]) -> list[str]:
    """按 ``card.inputs`` 校验入参，返回问题列表（空 = 通过）。

    只把**必填缺失**当错误；多给的键不算失败——模型常有可选参数，契约里没声明的
    额外键不值得中断一次调用。
    """
    (names, required), _ = _io_names(card)
    if not names:
        return []
    return [f"缺少必填输入 {name!r}" for name in sorted(required - set(inputs))]


def validate_outputs(card: Any, outputs: dict[str, Any]) -> list[str]:
    """按 ``card.outputs`` 校验产物，返回问题列表。

    声明了必填输出却拿不到 → 失败（下游按契约取字段会拿到空）。
    """
    _, (names, required) = _io_names(card)
    if not names:
        return []
    return [f"缺少必填输出 {name!r}" for name in sorted(required - set(outputs))]


# --------------------------------------------------------------------------- #
# Provider 选择
# --------------------------------------------------------------------------- #
#: ``access.protocol`` → provider 形态。这是把 GeoCard 已有的字段**落到调用路径**上。
_BATCH_PROTOCOLS = frozenset({"batch", "job", "oge-job", "https+batch"})
_HTTP_PROTOCOLS = frozenset({"http", "https", "rest", "inference", ""})


def choose_provider(card: Any, providers: dict[str, ModelProvider] | None = None) -> ModelProvider:
    """按 ``card.access.protocol`` 选 provider。

    未注册对应 provider 时抛 :class:`ModelError`——**明确失败**好过偷偷用默认值，
    否则一个声明 ``protocol: batch`` 的模型会被同步调用，行为与声明不符。
    """
    from .batch import BatchProvider
    from .inference import InferenceProvider

    access = getattr(card, "access", None)
    protocol = (getattr(access, "protocol", None) or "").lower()
    endpoint = getattr(access, "endpoint", None)
    if not endpoint:
        raise ModelError(f"模型 {card.id} 没有声明 access.endpoint，无法调用")

    if providers:
        key = "batch" if protocol in _BATCH_PROTOCOLS else "inference"
        provider = providers.get(key) or providers.get(protocol)
        if provider is None:
            raise ModelError(
                f"没有为 protocol={protocol or 'inference'} 注册 provider"
                f"（可用: {sorted(providers)}）"
            )
        return provider

    if protocol in _BATCH_PROTOCOLS:
        return BatchProvider(endpoint)
    if protocol in _HTTP_PROTOCOLS:
        return InferenceProvider(endpoint)
    raise ModelError(f"未知的 access.protocol: {protocol!r}")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def invoke_model(
    card: Any,
    inputs: dict[str, Any] | None = None,
    *,
    params: dict[str, Any] | None = None,
    capabilities: list[ComputeCapability] | None = None,
    registry_url: str | None = None,
    providers: dict[str, ModelProvider] | None = None,
    timeout: float = 300.0,
    require_compute: bool = True,
    strict_compute: bool = True,
) -> ModelOutcome:
    """调用一个 model GeoCard：先过算力门禁，再执行。

    Args:
        card: ``type: model`` 的 GeoCard。
        inputs: 按 ``card.inputs`` 命名的入参（通常是资产引用或路径）。
        capabilities: 候选算力；不给则从 ``registry_url`` 读 ``type: compute`` 卡片。
        registry_url: 发现算力用。
        providers: 注入 provider（测试与定制部署用）。
        strict_compute: 拒绝未知 CPU/内存容量；兼容旧部署时可显式设为 False。
        require_compute: 是否强制过算力门禁。默认 True——见包文档原则 1。
            只有"明确知道自己在没有 compute 卡片的环境里跑"时才关掉。
    """
    started = time.time()
    inputs = dict(inputs or {})
    invocation = ModelInvocation(
        model_id=getattr(card, "id", "unknown"),
        inputs=inputs,
        params=dict(params or {}),
        timeout=timeout,
    )

    # ① 输入契约
    problems = validate_inputs(card, inputs)
    if problems:
        return ModelOutcome.failure(INPUT_MISSING, "；".join(problems), reasons=problems)

    # ② 算力门禁：**不匹配就不进执行**
    if require_compute:
        caps = capabilities
        if caps is None and registry_url:
            try:
                caps = resolve_compute_capabilities(registry_url)
            except Exception as exc:  # noqa: BLE001 - 发现失败要变成可读的拒绝
                return ModelOutcome.failure(NO_COMPUTE, f"无法发现算力：{exc}", reasons=[str(exc)])
        match = match_resource(card, list(caps or []), strict=strict_compute)
        invocation.compute_node = match.compute_node
        invocation.match_reasons = list(match.reasons)
        if not match.matched:
            # 本模块最重要的一条路径：拒绝**必须**带理由，否则用户只知道"不行"
            return ModelOutcome.failure(
                NO_COMPUTE,
                f"没有满足 {invocation.model_id!r} 运行时要求的算力节点",
                reasons=list(match.reasons),
            )

    # ③ 选 provider 并调用
    try:
        provider = choose_provider(card, providers)
    except ModelError as exc:
        return ModelOutcome.failure(PROVIDER_ERROR, str(exc))
    provider = _retarget(provider, invocation.compute_node)

    try:
        outcome = provider.invoke(invocation)
    except ModelError as exc:
        return ModelOutcome.failure(PROVIDER_ERROR, str(exc), retryable=True)
    except Exception as exc:  # noqa: BLE001 - provider 的任何异常都变成失败的调用
        return ModelOutcome.failure(PROVIDER_ERROR, f"{type(exc).__name__}: {exc}", retryable=True)

    outcome.model_id = invocation.model_id
    outcome.compute_node = invocation.compute_node
    outcome.request_id = invocation.request_id
    outcome.elapsed_seconds = time.time() - started
    outcome.reasons = list(invocation.match_reasons) + list(outcome.reasons or [])

    # ④ 输出契约
    if outcome.ok:
        mismatch = validate_outputs(card, outcome.outputs)
        if mismatch:
            return ModelOutcome(
                status="failed",
                outputs=outcome.outputs,
                error="；".join(mismatch),
                error_code=OUTPUT_MISMATCH,
                provider=outcome.provider,
                model_id=invocation.model_id,
                compute_node=invocation.compute_node,
                request_id=invocation.request_id,
                elapsed_seconds=outcome.elapsed_seconds,
                reasons=outcome.reasons + mismatch,
            )
    return outcome


def _retarget(provider: ModelProvider, compute_node: str | None) -> ModelProvider:
    """算力门禁选中的节点与 provider 的 endpoint 不一致时，以门禁结果为准。

    门禁的语义是"这个模型应当跑在这台算力上"，所以它的结论必须**真的影响调用目标**，
    否则门禁只是个装饰。provider 支持 ``retarget`` 时改指向；不支持就保持原样。
    """
    if not compute_node:
        return provider
    retarget = getattr(provider, "retarget", None)
    if not callable(retarget):
        return provider
    current = getattr(provider, "endpoint", None)
    if current and current.rstrip("/") == compute_node.rstrip("/"):
        return provider
    logger.info("Retargeting model call -> %s (per compute gate)", compute_node)
    return retarget(compute_node)
