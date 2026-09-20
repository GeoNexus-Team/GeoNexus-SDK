"""模型调用契约 —— 让 GeoCard 的 ``type: model`` 真正可执行。

## 在此之前的状态

SDK 能**描述**模型（schema 有 ``model`` 类型、``runtime.{cpu,memory,gpu}``）也能
**匹配算力**（``geonexus.resource.match_resource``），但没有任何"调用一个 model card"
的路径：``geo.execute`` 只接受技能名。``docs/RESOURCE.md`` 写的
``retrieve → match → route → execute`` 里，最后一步是缺的。

## 这个包补的是最后一步

::

    model GeoCard
        │
        ├─ ① 前置门禁 match_resource(model, compute caps)
        │      不匹配 → 直接带理由拒绝，**不进执行**
        │
        ├─ ② 输入校验 card.inputs（IOField 名字与 required）
        │
        ├─ ③ 选 provider（按 access.protocol 与载荷形态）
        │      InferenceProvider  一次请求一次响应
        │      BatchProvider      提交 → 轮询 → 回收
        │
        └─ ④ 输出校验 card.outputs，产物按 IOField 名字登记

## 两条不可让步的原则

1. **不匹配就不执行**。算力不满足时必须在调用前拒绝并说明缺什么，而不是发出去
   等对方失败——那是把"部署错误"伪装成"运行时错误"，排查成本完全不同。
2. **输出必须对得上声明**。``card.outputs`` 是契约；provider 返回了未声明的字段、
   或缺少必填字段，都要显式报告，而不是让下游拿到一个形状不对的结果。
"""

from __future__ import annotations

from .backend import ModelBackend
from .batch import BatchProvider
from .inference import InferenceProvider
from .invocation import (
    ModelError,
    ModelInvocation,
    ModelOutcome,
    ModelProvider,
    choose_provider,
    invoke_model,
    validate_inputs,
    validate_outputs,
)

__all__ = [
    "ModelInvocation", "ModelOutcome", "ModelProvider", "ModelError",
    "InferenceProvider", "BatchProvider", "ModelBackend",
    "invoke_model", "choose_provider", "validate_inputs", "validate_outputs",
]
