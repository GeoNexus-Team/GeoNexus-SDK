"""审批状态词表 —— 卡片与方案**共用同一套**取值。

为什么单独一个模块：``pending`` / ``approved`` / ``rejected`` 描述的既不是卡片的
语义，也不是配方的语义，而是**治理状态**。放在 :mod:`geonexus.registry` 里会让
``geonexus.recipe`` 反过来依赖注册中心（循环导入的根因）；放在任何一侧都会让
另一侧"借用别人的词表"，迟早分叉成两套。

词表在这里定义，两侧各自 re-export。**裁决权不在 SDK**：本模块只有取值，
"谁可以批准"是平台的事（见 ``docs/ARCHITECTURE.md`` 的分工）。
"""

from __future__ import annotations

__all__ = [
    "STATUS_PENDING",
    "STATUS_APPROVED",
    "STATUS_REJECTED",
    "REVIEW_STATUSES",
]

#: 已提交、等待裁决。默认的新登记状态。
STATUS_PENDING = "pending"
#: 已批准，可被发现与复用。
STATUS_APPROVED = "approved"
#: 已驳回；记录保留，便于"为什么没通过"可追溯。
STATUS_REJECTED = "rejected"

#: 全量取值，顺序即流程顺序。
REVIEW_STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED)
