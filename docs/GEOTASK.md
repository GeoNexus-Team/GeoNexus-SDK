# GeoTask — 统一任务模型与执行契约

**状态**：契约已冻结（ROADMAP Phase 0 产出）。Phase 1 按本文实现。
**定位**：这是 **P0 冻结的契约**，不是实现说明。实现可以让本文变具体，但不该让本文
变正确——改契约要回到本文改，并让 `tests/test_geotask_contract.py` 跟着变。

---

## 一、为什么要统一

SDK 里现在有**四个互不相干的状态词表**和三个"任务"概念：

| 现有物 | 状态词 | 缺什么 |
|---|---|---|
| `web.TaskManager` / `Task` | `queued/running/done/failed/cancelled` | 依赖、资源、重试、归属、持久化；**纯内存，重启后旧记录会撒谎** |
| `cafe.PushdownRequest` / `TaskStatus` | `queued/running/succeeded/failed/timeout` | 依赖、优先级、重试策略、资源约束；`metadata` 是自由字典 |
| `agent.Plan` / `PlanStep` | `pending/done/failed/skipped` | **有 DAG 却串行执行**；不落盘、无重试 |
| `adapters.OgeTaskPoller` | 远端自有状态 | 轮询交互，没有统一语义 |

四套词表意味着：同一个"失败"，在 web 里是 `failed`、在 cafe 里可能是 `timeout`、
在 agent 里是 `failed` 而下游变 `skipped`。跨层拼装时要写四套映射，且没有一处能
回答"这个任务现在到底怎么样"。

GeoTask 的目标不是"再造一个任务类"，而是**让上面四个都变成它的视图或后端**。

---

## 二、数据模型

字段命名与 GeoCard 对齐（`inputs`/`outputs` 用引用而非值；溯源沿用同一套强制字段）。

```python
@dataclass
class GeoTask:
    task_id: str                    # 全局唯一，同时是**幂等键**
    kind: TaskKind                  # skill | model | batch | composite
    spec: dict[str, Any]            # 做什么：{"skill": name} / {"model": card_id} / {"job": ...}
    inputs: dict[str, TaskIO]       # 名称 → 引用
    outputs: dict[str, TaskIO]      # 名称 → 引用（声明式，执行体负责填充）
    depends_on: list[str]           # 上游 task_id（DAG 边）
    priority: int = 0               # 越大越先；同优先级按提交时间 FIFO
    resources: ResourceRequirement  # 预约需求，见第二节之二
    retry: RetryPolicy              # 重试策略，见第五节
    owner: str | None = None
    tenant: str | None = None
    labels: dict[str, str] = {}     # 路由提示（原 cafe 的 metadata）
    provenance: dict[str, Any] = {} # origin / source / license / retrieved（与 GeoCard 同强制）
    # --- 运行时状态（由状态机维护，调用方不改） ---
    state: TaskState = TaskState.CREATED
    attempt: int = 0
    scheduled_to: str | None = None      # 执行者 id（节点/worker/推理服务）
    lease_expires_at: float | None = None
    cancel_requested: bool = False
    progress: float = 0.0
    message: str = ""
    result: TaskResult | None = None
    error: TaskError | None = None
    created_at: float = ...
    started_at: float | None = None
    finished_at: float | None = None
```

### 2.1 TaskIO —— 引用，不是值

```python
@dataclass
class TaskIO:
    ref: str                          # 资产 id / URI / 变量名
    kind: str                         # "asset" | "literal" | "task_output"
    from_task: str | None = None      # kind=task_output 时的上游 task_id
    field: str | None = None          # 取上游输出的哪个字段
```

步骤间传值沿用 agent 现有语义（`${stepN.outputs.key}`），但**用引用表达**而不是把值
内联进参数，这样：产物可以只留在原地（数据主权）、任务记录可以只存引用、重放不需要
复制数据。

> `agent.planner._resolve_templates` 的字符串模板是现有可用实现，Phase 1 让它产出
> `TaskIO` 而不是替换后的值。**不要**在 GeoTask 层重新发明模板语法。

### 2.2 资源：声明 vs 预约

GeoCard 的 `runtime.{cpu,memory,gpu}` 是**声明**（模型需要什么），粒度粗
（`gpu: "cuda"` 是种类不是数量）。调度需要**预约**（占多少、占多久）。两者不合并，
但必须能换算：

```python
@dataclass
class ResourceRequirement:
    cpu: float = 0.0                       # 核
    memory_mb: int = 0
    gpu: GpuRequirement | None = None      # 只有需要 GPU 时才非空

@dataclass
class GpuRequirement:
    kind: str = "cuda"                     # 与 GeoCard runtime.gpu 同一套取值
    count: int = 1
    memory_mb: int | None = None
```

**记账单位（这是路线图里的留白，此处定死）**：

* 调度器按 **`cpu_cores` / `memory_mb` / `gpu_count`** 三个维度记账，`gpu` 额外记
  `gpu_kind`（不同种类不可互相顶替）。
* **占用 = 该节点上所有 `running` 任务的预约之和**。不按"实际用量"记账——
  实际用量要监控/埋点，而预约是我们能保证的契约；两者不一致是**监控**问题，不是
  调度问题。
* 显存（`gpu.memory_mb`）**声明了就记，没声明不记**，不猜。
* 字符串到数值的换算复用 `geonexus.resource.parse_memory`（已实现并有测试），
  不另写一份。

---

## 三、状态机

### 3.1 声明（机器可校验）

下面这段 JSON 是**权威定义**，`tests/test_geotask_contract.py` 会解析它并做自洽性
检查；Phase 1 的实现必须与它逐条相等，否则测试红。

```json geotask-state-machine
{
  "initial": "created",
  "states": ["created", "queued", "blocked", "scheduled", "running",
             "succeeded", "failed", "cancelled", "skipped"],
  "terminal": ["succeeded", "failed", "cancelled", "skipped"],
  "transitions": [
    ["created", "queued"],
    ["created", "cancelled"],
    ["queued", "scheduled"],
    ["queued", "blocked"],
    ["queued", "cancelled"],
    ["blocked", "queued"],
    ["blocked", "failed"],
    ["blocked", "skipped"],
    ["blocked", "cancelled"],
    ["scheduled", "running"],
    ["scheduled", "queued"],
    ["scheduled", "failed"],
    ["scheduled", "cancelled"],
    ["running", "succeeded"],
    ["running", "failed"],
    ["running", "cancelled"],
    ["running", "queued"]
  ]
}
```

### 3.2 为什么是这几个状态

| 状态 | 含义 | 为什么需要它 |
|---|---|---|
| `created` | 已登记，未入队 | 支持"先建图再提交"（DAG 一次性建好） |
| `queued` | 在队列等调度 | 与 `running` 分开才能表达排队与公平 |
| `blocked` | **资源或依赖不满足，且可解释** | 路线图要求"排队而非自由竞争"；`blocked` 必须带 `blocked_reason`，不许静默等待 |
| `scheduled` | 已选中执行者，等它接单 | 与 `running` 分开才能检测"派了但没跑起来"（租约过期的典型场景） |
| `running` | 执行者已开始 | — |
| `succeeded` / `failed` / `cancelled` / `skipped` | 终态 | 见下 |

**`skipped` 是终态而不是 `failed` 的变体**：agent 现有 `PlanStep.status` 就有
`skipped`，语义是"按策略决定不执行"，不是"执行失败"。把它压进 `failed` 会让
"失败率"这类统计失真。

**`timeout` 不是状态**。cafe 有 `TaskStatus.TIMEOUT`，但那与 `FAILED` 是同一件事的
两种原因。GeoTask 把原因放进错误码（`error.code="timeout"`），终态统一为 `failed`——
状态描述"走到哪了"，错误码描述"为什么"。这样终态集合不会随失败原因增长。

### 3.3 迁移规则（谁在什么时候改状态）

| 触发 | 迁移 | 备注 |
|---|---|---|
| 提交 | `created → queued` | 登记即入队 |
| 调度器选中 | `queued → scheduled` | 同时写 `scheduled_to` 与 `lease_expires_at` |
| 依赖未满足 / 资源不足 | `queued → blocked` | **必须**写 `blocked_reason` |
| 条件满足 | `blocked → queued` | 依赖完成或资源释放，由调度器重算 |
| 执行者接单 | `scheduled → running` | 记 `started_at` |
| 执行者拒收 / 租约过期且可重试 | `scheduled → queued` | `attempt` 不变，见第五节 |
| 正常结束 | `running → succeeded` | 记 `finished_at` |
| 失败（含超时） | `running → failed` | 写 `error.code` |
| 取消生效 | 任意非终态 `→ cancelled` | **协作式**，见 3.4 |
| 依赖失败级联 | `blocked → failed` | `error.code="upstream_failed"`，fail-fast |
| 策略跳过 | `blocked → skipped` | 仅由显式策略触发（如反思建议 skip） |
| 可重试失败 | `running → queued` | `attempt += 1` |

### 3.4 取消是协作式的

下推出去的子进程杀不掉（GeoKG 的 `refresh checkpoint` 已验证这个模式）。
因此：

* 取消请求只设 `cancel_requested = True`，状态**不变**。
* 执行体在**步骤边界**检查它；检查通过后自行结束并返回"已取消"。
* 状态转为 `cancelled` 由**执行体正常返回后**的状态机落定，而不是由取消请求直接改写。
* 若任务处于 `created`/`queued`/`blocked`（尚未真正执行），取消立即生效。

> 这条直接来自 `web.TaskManager` 的现有语义，保持行为一致，不发明第二套。

---

## 四、事件契约

### 4.1 形状

```python
{
  "task_id": str,
  "seq": int,            # **每个任务内从 1 开始严格递增**
  "event": str,          # 见 4.2
  "data": dict,
  "timestamp": float,
  "attempt": int,
}
```

**`seq` 是新增的，也是对现有 `cafe.events.push_event` 的修正**：它的 payload 只有
`request_id/event/data/timestamp`，客户端无法判断是否丢帧，重连后也无法去重。有了
`seq`，客户端只要记住最后收到的号，就能"无重复、无空洞"地续订。

### 4.2 事件类型

| 事件 | 何时 | `data` 至少含 |
|---|---|---|
| `task.created` | 登记后 | `kind` |
| `task.state_changed` | 每次状态迁移 | `from`, `to`, `reason`(可选) |
| `task.progress` | 执行体上报 | `progress`(0..1), `message` |
| `task.output` | 产物登记 | `name`, `ref` |
| `task.failed` | 进入 `failed` | `code`, `message`, `retryable` |
| `task.cancelled` | 进入 `cancelled` | `stage`(在哪个边界停下的) |

### 4.3 顺序与重放

* **同一任务内**：事件按 `seq` 全序，**不得乱序、不得跳号**。
* **跨任务**：**不保证顺序**。这与 `web.TaskManager` 现有 docstring 的警告一致
  （"Ordering across concurrent transitions of the *same* task is not guaranteed"）——
  这里把那条警告升级成契约：需要顺序就按 `seq` 排，别假设到达顺序。
* **重放**：订阅接口接受 `since=<seq>`，服务端只回 `seq > since` 的事件。历史保留
  窗口由 store 决定，超出窗口必须返回 `410 Gone` + 最小可用 `since`，**不许静默
  从头开始**（那会让客户端重复处理）。

---

## 五、失败语义

### 5.1 错误码

| code | 可重试 | 说明 |
|---|---|---|
| `input_missing` | ❌ | 引用的资产或上游输出不存在 |
| `invalid_spec` | ❌ | `spec` 不满足契约（含 schema 校验失败） |
| `no_compatible_compute` | ❌ | model 的 `runtime` 无匹配算力（`RESOURCE.md` 已定此原则：**带理由拒绝**） |
| `resource_unavailable` | ✅ | 目标节点资源不足 |
| `executor_lost` | ✅ | 租约过期 / worker 掉线 |
| `timeout` | ✅ | 超出 `timeout_seconds` |
| `execution_error` | ✅ | 执行体抛错（默认可重试，可由 `RetryPolicy` 覆盖） |
| `upstream_failed` | ❌ | 依赖失败级联 |
| `cancelled` | ❌ | 用户取消 |

`❌` 的语义是"重试没有意义"——重试只会再失败一次，并掩盖真正的问题。

### 5.2 重试

```python
@dataclass
class RetryPolicy:
    max_attempts: int = 1              # 1 = 不重试
    backoff_seconds: float = 1.0
    multiplier: float = 2.0
    max_backoff_seconds: float = 60.0
```

重试判定 = `error.retryable and attempt < max_attempts`。满足则 `running → queued`
且 `attempt += 1`；不满足则终态 `failed`。**退避只影响何时重新入队，不影响状态语义**。

### 5.3 依赖失败级联（路线图留白，此处定死）

**fail-fast**：上游进入 `failed`/`cancelled` 后，其直接下游由 `blocked → failed`
（`code="upstream_failed"`），并**递归**向下传播。理由：一个 DAG 里某个中间步骤拿不到
输入，继续跑下游只会产生连锁的、更难定位的失败。

`skipped` 的上游**不**级联失败——跳过是策略决定，下游可能仍然可跑。
"可选的依赖边"（上游失败但下游继续）**v1.1 不做**，留到后续。

### 5.4 幂等

`task_id` 就是幂等键。**以同一 `task_id` 再次提交：不新建、不重启，返回既有任务**。
这条是跨节点重试与网络重投递不产生重复执行的唯一保证。

---

## 六、持久化与重启对账

### 6.1 Store 协议

```python
class TaskStore(Protocol):
    def put(self, task: GeoTask) -> None: ...
    def get(self, task_id: str) -> GeoTask | None: ...
    def list(self, *, states=None, owner=None, tenant=None, limit=100) -> list[GeoTask]: ...
    def transition(self, task_id, to: TaskState, *, reason="", error=None) -> GeoTask: ...
    def append_event(self, task_id: str, event: str, data: dict) -> int: ...   # 返回 seq
    def events_since(self, task_id: str, since: int) -> tuple[list[dict], int]: ...
```

`transition()` 是**唯一**能改状态的方法：它校验本文第三节的迁移表，非法迁移抛
`IllegalTransition`。这样"状态机"不是一个文档约定，而是**代码里的关卡**。

后端计划：`memory`（测试）→ `JSONL`/`SQLite`（单节点）→ `Postgres`（集群）。
接口不变，换实现不影响上层。

### 6.2 重启对账（"不撒谎"原则）

进程重启后**绝不允许**把无人执行的任务报成 `running`。启动时扫一遍：

| 重启时状态 | 动作 |
|---|---|
| `running` / `scheduled`，且 `lease_expires_at` 已过期 | 按 `retry.retryable` 与 `attempt` 决定 → `failed(executor_lost)` 或 `queued` |
| `running` / `scheduled`，租约未过期 | 保留状态，但**必须有存活的执行者认领**；认领失败即视为过期 |
| `queued` / `blocked` / `created` | 保留，交回调度器 |
| 终态 | 不动 |

租约机制是这一条的前提：`scheduled_to` + `lease_expires_at`，执行者定期续租。
**没有租约就没有"对账"可言**，所以租约不是优化，是必需项。

---

## 七、执行后端协议

调度层不关心任务在哪跑，只认这个接口：

```python
class ExecutionBackend(Protocol):
    name: str
    def capabilities(self) -> BackendCapabilities: ...   # 支持的 kind、资源维度、是否支持取消
    def submit(self, task: GeoTask, ctx: ExecutionContext) -> Handle: ...   # 非阻塞
    def poll(self, handle: Handle) -> TaskOutcome: ...   # 状态 + 进度 + 产物引用
    def cancel(self, handle: Handle) -> bool: ...        # 协作式，返回"是否已受理"
```

| 后端 | 用途 | 对应决策 |
|---|---|---|
| `LocalBackend` | 进程内技能，线程池 | D1 本地层 |
| `CafeBackend` | 下推（沙箱 + 跨节点） | D2 唯一化 |
| `InferenceBackend` | 模型推理，HTTP/gRPC 独立服务 | D4 |
| `BatchBackend` | 批/云作业，提交→轮询→回收 | D4 |

`capabilities()` 存在的意义：**不是所有后端都支持取消或进度**。调度器据此决定是
报错还是降级，而不是假装支持然后静默失效。

---

## 八、迁移映射

### 8.1 三个概念 → GeoTask

| 现有 | 变成 | 处置 |
|---|---|---|
| `web.TaskManager` | `TaskStore` + `LocalBackend` 的**适配器视图** | 公开 API（`submit/get/list/cancel/should_cancel/update_progress/wait/close`）**不变**；16 条 HTTP 路由契约不变 |
| `web.Task` | `GeoTask` 的投影 | `id→task_id`、`status→state`、其余同名；`progress`/`message` 保留 |
| `cafe.PushdownRequest` | `GeoTask(kind=skill/batch)` | `code→spec["code"]`、`inputs` 保持引用语义、`returns→outputs`、`timeout_seconds→timeout`、`metadata→labels` |
| `cafe.TaskStatus` | `TaskState` + `error.code` | `timeout` 拆成 `failed` + `code=timeout` |
| `cafe.TaskRouter` | 调度层 `TaskRouter` | 合并数据本地性（federation 的归属查表）+ 健康 + 算力余量（`resource.py`）+ 合规 |
| `cafe.events` | 事件层 | 加 `seq`、加 `since` 重放；形状见第四节 |
| `agent.Plan` / `PlanStep` | DAG 编排层 | `depends_on` 语义保留；`${stepN.outputs.key}` 模板保留；**执行由串行改为就绪集并行** |
| `agent.PlanStep.status` | `TaskState` | 见 8.2 |
| `federation.FederatedGeoMCPClient` | `CafeBackend` 的薄封装 | 公开 API **不变**（已冻结） |
| `adapters.OgeTaskPoller` | `BatchBackend` 的一个实现 | 轮询交互复用 |

### 8.2 四套状态词 → 一套

| 现有词表 | 现有取值 | → `TaskState` |
|---|---|---|
| `web` | `queued` / `running` / `done` / `failed` / `cancelled` | `queued` / `running` / **`succeeded`** / `failed` / `cancelled` |
| `cafe` | `queued` / `running` / `succeeded` / `failed` / `timeout` | `queued` / `running` / `succeeded` / `failed`(+`code=timeout`) |
| `agent` | `pending` / `done` / `failed` / `skipped` | **`created`**（未入队）或 **`blocked`**（等依赖）/ `succeeded` / `failed` / `skipped` |
| `OgeTaskPoller` | 远端自有 | 由 `BatchBackend` 翻译成上表 |

> `pending → created|blocked` 的判定规则：该步骤的所有 `depends_on` 都已完成 →
> `created`（可入队）；否则 `blocked`。**不引入第五个状态**去表达"计划内但还没轮到"。

### 8.3 兼容约束

* `API_STABILITY.md` 里已冻结的名字**一个都不能破**；新增都是加法。
* `web` 的 16 条路由与响应形状不变。
* `federation` 公开 API 与行为不变（Phase 2 单独验收）。
* 四个旧状态词表在新代码里**只允许出现在适配器内部**，不允许穿透到调度层。

---

## 九、明确不做（v1.1）

| 不做 | 理由 |
|---|---|
| 抢占（preemption） | 需要优先级反转与检查点配合；没有检查点前抢占等于浪费已完成的算力 |
| 可选依赖边（上游失败下游继续） | fail-fast 先建立确定性；可选边会让"为什么这个任务跑了"难回答 |
| 任务亲和性 / 数据缓存亲和 | 需要节点级缓存模型，属于 Phase 4 之后 |
| 多租户配额与计费 | schema 里留了 `tenant`，但配额策略是运营决策，不是 SDK 契约 |
| 状态机的可视化编辑器 | 图视图已有（GeoKG/门户），任务图用现成渲染即可 |
