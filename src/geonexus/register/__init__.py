"""第三方资源登记核心：探测 → 建卡 → 校验 → 提交。

设计原则（与 docs/plans/client-marketplace-v2.md W1 一致）：
  · **只读探测**：对 URL 只发 GET；对文件只读魔数/表头/JSON，**绝不执行**被探测对象
    （不 torch.load、不跑 ONNX runtime）——登记工具不该成为远程代码执行入口。
  · 取不到的字段**留空并给 warning**，不编造（没有 bbox 就不写 spatial，卡片自然走侧栏）。
  · CLI 向导与平台后台共用这一层，不允许第二份实现漂移。
"""

from .build import KIND_TYPE, build_card, validate_card
from .probe import ProbeResult, probe_target
from .submit import submit

__all__ = ["ProbeResult", "probe_target", "build_card", "validate_card", "submit", "KIND_TYPE"]
