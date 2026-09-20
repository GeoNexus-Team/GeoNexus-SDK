# GeoCard resource matching (v1.1)

The GeoCard workflow is *retrieve → match → route → execute*: find assets,
check they satisfy the request, decide **where** the computation runs, then
execute. `geonexus.resource` implements the "match → route" part for the
**compute dimension**: it decides whether a model can run on a compute node
and explains why.

## The three asset kinds

| GeoCard `type` | What it declares | Example |
|---|---|---|
| `data` | what the data covers (bbox / time / bands) and where it lives (`access`) | `amazon-ndvi-2015` |
| `model` | a computation (capability) **and its runtime requirements** (`runtime.cpu` / `runtime.memory` / `runtime.gpu`) | `model-ndvi-change` (needs `gpu=cuda`) |
| `compute` | what a node can actually provide (`runtime`) + its endpoint (`access`) | `compute-node-gpu` (8 cores / 16Gi / cuda) |

A model card without a compatible compute node must be **rejected with a
reason**, not silently failed at execution time.

## Usage

```python
from geonexus.resource import match_resource, resolve_compute_capabilities

# 1. Discover what compute nodes the registry advertises (type: compute cards).
caps = resolve_compute_capabilities("http://registry:8790")

# 2. Match a model card (declares runtime) against those nodes.
match = match_resource(model_card, caps)
print(match.matched, match.compute_node)
for reason in match.reasons:
    print(" -", reason)
```

Matching rules (all must hold):

- `cpu`: model needs ≤ node provides (when both known).
- `memory`: parsed via `parse_memory` (`"4Gi"`, `"4096Mi"`, `"8 GB"` → GiB);
  model needs ≤ node provides.
- `gpu`: `none` requirement → any node; a concrete requirement (`cuda`,
  `mps`, device name) → node must offer a compatible GPU.

Unknown model-side values are treated as satisfied; unknown node-side values
are assumed sufficient but recorded as warnings in `reasons`. The first
satisfying node wins.

## Integration

Pair with the registry (`search(type="compute")`) and the planner: after
`GeoAgentPlanner` resolves data nodes, run `match_resource` to route the
model step to the compute node that satisfies its runtime. The reference app
(`geonexus-web`) demonstrates the full flow with a teaching timeline:
retrieve → contract check → resource match → plan → execute.

### "execute" 现在真的存在了（Phase 3）

在 Phase 3 之前，匹配结果没有任何东西消费——`geo.execute` 只接受技能名，模型卡
匹配完就断了。`geonexus.model.invoke_model` 补上了最后一步，并且**把门禁前置**：

```python
from geonexus.model import invoke_model

outcome = invoke_model(model_card, {"image": "s3://…"}, capabilities=resolve_compute_capabilities(reg))
if not outcome.ok:
    print(outcome.error_code, outcome.reasons)   # 例如 no_compatible_compute + gpu 理由
```

**匹配结论必须真的影响调用目标**：门禁选中的节点会通过 `provider.retarget()` 改写
实际 endpoint，否则门禁只是个装饰。细节见 `MODEL.md`。

### 节点内调度（Phase 4）

`match_resource` 回答"**哪台**算力能满足这个模型"，不回答"**现在**能不能起"。
后者由 `geonexus.geotask.scheduler.Scheduler` 负责：按 `cpu_cores` / `memory_mb` /
`gpu_count` 的预约之和做准入，容量不足时任务进 `blocked`（带理由），装不下则直接
拒绝。两者是同一套单位（`ResourceRequirement.from_geocard_runtime` 复用
`parse_memory`），所以模型的 `runtime` 声明可以直接变成调度预约。详见
`GEOTASK.md` §10。
