# 模型调用契约（Model Invocation）

> 状态：Phase 3 已落地（`geonexus.model`、GeoMCP `geo.invoke`）。
> 相关：`RESOURCE.md`（算力匹配）、`GEOTASK.md`（任务契约）、`GEOCARD.md`（`type: model`）。

## 1. 为什么需要这一层

在 Phase 3 之前，SDK 能**描述**模型（GeoCard 的 `model` 类型、`runtime.{cpu,memory,gpu}`）、
能**匹配算力**（`geonexus.resource.match_resource`），但没有任何"调用一个 model card"的
路径：`geo.execute` 只接受技能名。`RESOURCE.md` 里写的
`retrieve → match → route → execute`，最后一步是缺的——匹配结果没有任何东西消费。

这一层补的就是最后一步，并且**只做这一步**：不托管模型、不做训练、不做评测。

```
model GeoCard
    │
    ├─ ① 输入契约     card.inputs 的必填项（IOField.name / required）
    │
    ├─ ② 算力门禁     match_resource(card, compute capabilities)
    │                 不匹配 → 带理由拒绝，**一个字节都不发出去**
    │
    ├─ ③ provider     InferenceProvider（一次请求一次响应）
    │                 BatchProvider（提交 → 轮询 → 回收）
    │                 门禁选中的节点会**改写调用目标**（retarget）
    │
    └─ ④ 输出契约     card.outputs 的必填项；缺了就报 invalid_spec
```

## 2. 两条不可让步的原则

1. **不匹配就不执行。** 算力不满足时必须在调用**之前**拒绝并说明缺什么。
   发出去等对方失败，是把"部署错误"伪装成"运行时错误"——两者的排查成本完全不同。
2. **输出必须对得上声明。** `card.outputs` 是契约。provider 返回了未声明的字段、
   或缺少必填字段，都要显式报告，不能让下游拿到一个形状不对的结果。

还有一条隐含的：**门禁结论必须真的影响调用目标**。如果匹配到节点 A 却仍然打到卡片
里写死的 endpoint，门禁就只是个装饰。所以 `invoke_model` 会在 provider 支持时调用
`provider.retarget(compute_node)`。

## 3. 公开 API

| 名字 | 说明 |
|---|---|
| `geonexus.model.invoke_model(card, inputs, ...)` | 主流程：输入校验 → 算力门禁 → provider → 输出校验 |
| `geonexus.model.choose_provider(card, providers=None)` | 按 `card.access.protocol` 选 provider；未知协议**明确失败** |
| `geonexus.model.validate_inputs(card, inputs)` / `validate_outputs(card, outputs)` | 返回问题列表（空 = 通过） |
| `geonexus.model.InferenceProvider` | HTTP 同步推理 |
| `geonexus.model.BatchProvider` | 批/云作业：`submit` / `poll` / `collect` / `invoke` |
| `geonexus.model.ModelBackend` | 把模型调用接到 GeoTask 调度契约上的 `ExecutionBackend` |
| `geonexus.model.ModelInvocation` / `ModelOutcome` | 请求 / 结果（**失败也返回结果，不抛异常**） |
| `geonexus.model.ModelProvider` | provider 协议：`name` / `capabilities()` / `invoke()` |
| `geonexus.model.ModelError` | 配置/协议层面的错误（如没有注册对应的 provider） |

`invoke_model` 的签名要点：

```python
invoke_model(
    card,                       # type: model 的 GeoCard
    inputs,                     # 按 card.inputs 命名的入参
    *,
    params=None,                # 模型专属参数（原样透传）
    capabilities=None,          # 候选算力；不给则从 registry_url 发现
    registry_url=None,
    providers=None,             # 注入 provider（定制部署 / 测试）
    timeout=300.0,
    require_compute=True,       # 默认强制过算力门禁
) -> ModelOutcome
```

`require_compute=True` 是默认值，且**不建议关**：关掉它只适用于"明确知道自己在没有
compute 卡片的环境里跑"，否则等于把门禁拆了。

## 4. 错误码

`ModelOutcome` 的 `error_code` 与 `GEOTASK.md` §5.1 的错误码表同名，便于跨层统计：

| `error_code` | 触发 | 可重试 |
|---|---|---|
| `no_compatible_compute` | 没有满足 `runtime` 的算力（含发现算力失败） | 否 |
| `input_missing` | 缺少 `card.inputs` 的必填项 | 否 |
| `invalid_spec` | 输出契约不符 / 请求本身被服务拒绝（4xx） | 否 |
| `resource_unavailable` | 推理服务 5xx（服务在，但暂时不可用） | 是 |
| `executor_lost` | 连不上、连接中断 | 是 |
| `timeout` | 超时（含批作业在期限内没到终态） | 是 |
| `execution_error` | 其他执行失败 | 视 provider |

`ModelOutcome.retryable` 由 **provider** 决定（它知道具体情形），`ERROR_RETRYABLE`
只是没有 provider 判定时的兜底。`ModelBackend` 会把它翻译成 `TaskError`，于是
`RetryPolicy` 的重试与退避**自动生效**。

## 5. provider 的线上约定

### 5.1 `InferenceProvider`（同步）

```
POST {card.access.endpoint}
{"model": "<card id>", "inputs": {...}, "params": {...}, "request_id": "invoke-..."}

200 → {"outputs": {...}}      # 或直接返回产物对象 {"<output name>": ...}
4xx → 请求本身有问题（不可重试）
5xx / 超时 / 连不上 → 服务或网络的问题（可重试）
```

`api_key` 非空时带 `Authorization: Bearer <key>`。

### 5.2 `BatchProvider`（提交 → 轮询 → 回收）

```
POST {endpoint}            → {"job_id": "...", "status": "queued"}
GET  {endpoint}/{job_id}   → {"status": "running"|"succeeded"|"failed", ...}
                             succeeded → {"outputs": {...}}
                             failed    → {"error": "..."}
```

`submit()` 与 `collect()` 是分开的公开方法：长作业的调用方可以只提交、把 `job_id`
持久化，之后再回收（进程重启也不丢）。`invoke()` 是三步的便捷组合。

交互顺序（测试里断言过）：**先 POST，再 GET**，`GET` 直到终态为止。

### 5.3 协议 → provider 的映射

| `card.access.protocol` | provider |
|---|---|
| `batch` / `job` / `oge-job` / `https+batch` | `BatchProvider` |
| `http` / `https` / `rest` / `inference` / 空 | `InferenceProvider` |
| 其他（例如 `grpc`） | **明确报错** |

声明 `protocol: batch` 的模型不会被偷偷降级成同步调用——那种"能跑但跑法不对"的
问题最难查。gRPC 目前**不支持**，遇到时明确失败而不是猜。

## 6. `geo.invoke`（GeoMCP 加法式扩展）

Phase 3 只加了**一个**方法，且是加法式的：

```jsonc
// request
{"jsonrpc": "2.0", "id": 1, "method": "geo.invoke",
 "params": {"model": "clip-vit-b32", "inputs": {"image": "s3://…"},
            "params": {"top_k": 5}, "timeout": 60}}

// result —— 是 ModelOutcome.to_dict()，**门禁拒绝也是 result 而不是 error**
{"status": "failed", "error_code": "no_compatible_compute",
 "reasons": ["gpu: 需要 cuda，可用的是 none"], "outputs": {},
 "compute_node": null, …}

// 成功时 compute_node 是**真的**被打到的那台（门禁结论而非卡片里写死的 endpoint）
```

- 只接受 `type: model` 的卡片；其它类型 → `invalid_argument`。
- 卡片不存在 → `geocard_not_found`。
- 节点没有配置模型调用能力 → `invalid_argument`，并在 `data.capability` 里说明。
- **“调用失败”不是协议错误**：协议调用成功、被调用的东西失败了，就是 `result`
  里一个 `status: failed`。这样调用方能拿到 `error_code` / `retryable` / `reasons`。
- 它是**写**方法：配置了 `api_keys` 的节点要求 `X-API-Key`（与 `geo.execute` 同级）。

能力发现：

```jsonc
// geo.capabilities 里的新键（老客户端读不认识的键会忽略）
"model_invocation": {"enabled": true, "method": "geo.invoke",
                     "compute_gate": true, "providers": ["inference"],
                     "accepted_cards": ["model"]}
```

老节点（不认识 `geo.invoke`）返回标准 JSON-RPC `METHOD_NOT_FOUND`（-32601），
`data.supported` 里是它真正支持的方法表。

## 7. 接到 GeoTask 上：`ModelBackend`

模型调用与技能执行共用**同一套**状态机、租约、重试与观测——否则模型任务在平台上
就是"另一种东西"，运维要学两套。

```python
backend = ModelBackend(
    {"clip-vit-b32": card},              # 或 resolver=…（按 id 去注册中心取）
    capabilities=[ComputeCapability(...)],
    providers={"inference": InferenceProvider(...)},
)
task = GeoTask("t1", TaskKind.MODEL, spec={"model": "clip-vit-b32",
                                           "params": {"top_k": 5}})
```

- `spec` 键：`model` / `model_id` / `card`（直接给卡片对象）、`inputs`（覆盖
  `task.inputs`）、`params`、`timeout`。
- `task.inputs` 里的 `TaskIO` 被摊平成入参：取 `ref` 的值。**不在这里解引用**——
  解引用是资产层的职责，模型服务要的是路径或句柄。
- `capabilities()`：`kinds={"model","batch"}`、`supports_cancel=False`、
  `supports_progress=False`、`resource_dims=("cpu","memory_mb","gpu")`。

**取消：做不到就是做不到。** HTTP 请求发出后本地无法撤销远端计算，所以
`ModelBackend.cancel()` 返回 `False`，取消只在提交前生效。谎报"已取消"比说
"做不到"更有害。资源占用由 `geonexus.geotask.scheduler` 在准入阶段控制（见
`GEOTASK.md` §10）。

## 8. 明确不做

| 不做 | 理由 |
|---|---|
| 在 GeoNode 进程内托管模型 | 占进程、难隔离、显存不可管 |
| 训练 / 微调 / 评测生命周期 | 那是另一个产品，混进来会稀释 SDK 的协议与契约定位 |
| gRPC provider | 尚未实现；遇到 `protocol: grpc` 明确报错，不猜 |
| 自动解引用资产 | 资产解析是资产层的职责；模型服务要的是路径或句柄 |

## 9. 验收（可运行）

`tests/test_model.py`（52 项）：

| 判据 | 证据 |
|---|---|
| 端到端闭环 1 条（**真实** HTTP 推理服务，非 mock） | `TestInferenceProvider::test_end_to_end_over_real_http` 用 `http.server.ThreadingHTTPServer` 起真服务、真 httpx 请求 |
| 无兼容算力的模型**带理由拒绝**，且没进执行 | `TestComputeGate::test_gpu_mismatch_rejected_before_any_call` 断言该服务的请求记录为空 |
| 门禁结论真的改变调用目标 | `test_match_succeeds_and_targets_the_gated_node`：卡片 endpoint 指向死端口，命中算力节点后成功 |
| 批作业：提交 → 轮询 → 回收 全路径可测 | `TestBatchProvider::test_full_submit_poll_collect_path` 断言请求序列是 POST 后接 GET |
| 老客户端（不协商新能力）行为不变 | `TestGeoInvoke::test_methods_is_a_superset_of_the_original_four`、`test_legacy_keys_unchanged`、`test_old_dispatcher_without_invoke_returns_method_not_found` |
| 模型调用接入任务契约 | `TestModelBackend::*`（错误码与可重试性跨层、门禁理由可达调度器） |
