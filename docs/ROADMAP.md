# GeoNexus SDK 下一步路线图（v1.1 → v1.2）

**主题**：把「能描述」补成「能协同调度与执行」—— GeoTask 统一任务模型、CAFE 成为唯一
下推引擎、模型成为可调用的一等资产。

**状态**：设计已定，待实施。
**依据**：本文第一节是实测评估（含行数、测试数、调用点核查），不是印象。

---

## 一、现状评估（实测）

`core/` 共 78 个 py 文件 / 16,988 行 / 470 项测试。

| 层 | 完成度 | 证据 |
|---|---|---|
| **GeoCard** 身份与契约 | 高 | schema 20 个顶层字段、7 种类型（data/model/skill/agent/workflow/knowledge/compute）；契约校验覆盖 bbox（含 CRS 感知重投影）、temporal、bands、resolution；conformance 21 项 |
| **GeoMCP** 协议 | 高（刻意最小） | 4 个方法 + 版本协商 + 错误码；MCP 桥（stdio/HTTP）已实现；Java 侧独立实现并对齐（conformance 15 项） |
| **Registry / 发现** | 高 | 卡片+技能+审批流转+持久化+注册中心联邦；限制是单键 API key（项目自认） |
| **GeoNode / 本地执行 / Web BFF** | 完成 | Web BFF 16 条路由 + JWT + 异步任务 + SSE |
| **GAAG 语义门禁** | 可用 | embedding（词袋 + 可选 LLM）+ pgvector + cosine 语义门 |
| **federation 下推** | 可用（同步） | 283 行；按资产归属路由 + 健康过滤 |
| **agent 编排** | 可用（DAG 但串行） | `Plan.depends_on` + 拓扑排序 + 环检测；但 `PlanExecutor.run()` 是 `for step_id in order` —— 串行，可并行的步骤没并行 |
| **CAFE** | **设计有、未接线** | 509 行 / 10 测试；`CentralServer`+`WorkerNode`+`TaskRouter`+events；**生产代码零调用点** |
| **resource（模型↔算力）** | **设计有、未接线** | 248 行 / 16 测试 + `docs/RESOURCE.md` 写明 retrieve→match→route→execute；`match_resource` **生产代码零调用点** |

关键核查（`grep` 全仓库 import）：

```
from geonexus.cafe / geonexus.resource 的引用仅出现在：
  tests/test_cafe.py、tests/test_security_events.py、tests/test_resource.py
```

### 三个结构性结论

1. **主链路已完成**：`GeoCard（能描述）→ Registry（能被发现）→ GeoMCP/GeoNode（能被调用）`
   这条链是通的、有测试、有跨语言一致性验证。**不需要重写。**

2. **没有 GeoTask，且不是"缺一个类"**：现有三个互不相干的"任务"概念——
   `web.TaskManager`（HTTP 请求线程池，纯内存）、`cafe.PushdownRequest`（单任务，
   无依赖/优先级/重试/资源约束）、`agent.Plan`（有 DAG 但串行执行、不落盘）。
   协同管理要素**逐条缺失**：跨节点依赖图、队列与优先级、资源预约与占用记账、
   重试退避、持久化与断点续跑、任务归属、跨节点委派交接。

3. **AI 资产"能描述、不能运行"**：model/compute 类型与 `runtime.{cpu,memory,gpu}`
   已在 schema 和 `resource.py` 里实现并测试，但
   （a）没有"调用一个 model card"的路径——`geo.execute` 只接受 `skill`；
   （b）没有调度器——`match_resource` 只回答"哪个节点能跑"，不排队、不租约、不记账；
   （c）CAFE 的计算模型是**代码下推**（送 Python 源码到数据旁沙箱执行），没有把
   **模型**当一等工件：无制品/权重引用约定、无推理 provider 抽象、无批处理、无模型与数据的版本绑定。

4. **存在重复实现**：`federation`（按归属路由，已接线）与 `cafe`（按 metadata 路由 +
   沙箱 + 事件，未接线）是两套下推。合并的收益是双向的——**CAFE 缺的正是 federation
   已有的 HTTP 传输，federation 缺的正是 CAFE 已有的路由策略**。

---

## 二、已定决策

| # | 决策 | 含义 |
|---|---|---|
| **D1** | **GeoTask = 领域任务模型 + 分层执行** | 统一的任务模型（DAG/状态机/持久化/取消/归属），本地用线程池、跨节点用 CAFE；收编现有三个"任务"概念 |
| **D2** | **CAFE 成为唯一的下推引擎** | `federation` 退化为 CAFE 的薄封装；路由策略（数据本地性 + 算力余量 + 合规 + 健康）集中一处 |
| **D3** | **AI 支持 = 模型可调用闭环 + 算力调度与占用管理** | 补 model→推理 provider→调用路径，再补队列/租约/占用记账。**训练与评测生命周期明确排除** |
| **D4** | **模型跑在独立推理服务（HTTP/gRPC）与批/云作业上** | 不在 GeoNode 进程内托管模型；`access.endpoint` 指向推理服务，批作业走「提交→轮询→回收」（复用 `OgeTaskPoller` 那类交互） |

---

## 三、目标架构

```
                    ┌──────────── 提交面 ────────────┐
                    │ CLI · HTTP BFF · GeoAgent · 其他节点 │
                    └───────────────┬───────────────┘
                                    ▼
  ┌─────────────────────────── GeoTask ───────────────────────────┐
  │ 领域任务模型：id / kind / 输入输出引用 / 依赖 DAG / 优先级       │
  │             资源需求 / 归属 / 重试策略 / 溯源                   │
  │ 状态机：created → queued → scheduled → running → 终态           │
  └───────────────┬───────────────────────────────┬───────────────┘
                  ▼                               ▼
        ┌── 编排层（DAG）──┐            ┌── 调度层（TaskRouter）──┐
        │ 依赖解析 · 就绪集 │            │ 数据本地性（归属查表）   │
        │ **并行执行就绪步** │            │ 算力余量（resource.py） │
        │ 步骤级重试/降级   │            │ 合规策略 · 健康过滤     │
        └─────────┬────────┘            └───────────┬────────────┘
                  └──────────────┬──────────────────┘
                                 ▼
              ┌────────── 执行后端（多态）──────────┐
              │ LocalBackend     线程池（进程内技能）│
              │ CafeBackend      CAFE 下推（沙箱/跨节点）│
              │ InferenceBackend 模型推理（HTTP/gRPC）│
              │ BatchBackend     批作业（提交→轮询→回收）│
              └──────────────────┬─────────────────┘
                                 ▼
              ┌──── 状态与事件 ────┐
              │ TaskStore（可插拔）│  memory / JSON / SQLite / Postgres
              │ 事件流（SSE）      │  统一现有 cafe/events
              └───────────────────┘
```

### 3.1 GeoTask 数据模型（Phase 0 冻结）

| 字段 | 说明 |
|---|---|
| `task_id` | 全局唯一，可跨节点引用 |
| `kind` | `skill` / `model` / `batch` / `composite` |
| `spec` | 要执行什么（skill 名或 model card id）+ 参数 |
| `inputs` / `outputs` | **引用**而非值（资产 id / URI），与 GeoCard 的 IOField 对齐 |
| `depends_on` | 依赖的 task_id 列表（DAG 边） |
| `priority` | 队列排序用 |
| `resources` | `{cpu, memory, gpu}` —— 复用 GeoCard 的 `runtime` 语义 |
| `owner` / `tenant` | 归属与多租户隔离 |
| `retry` | 最大次数 + 退避策略；区分可重试与不可重试失败 |
| `state` | 见状态机 |
| `attempt` / `timestamps` | 可观测 |
| `provenance` | `origin` / `source` / `license` / `retrieved`（与 GeoCard **同一套强制字段**） |

状态机：

```
created ──▶ queued ──▶ scheduled ──▶ running ──┬──▶ succeeded
                ▲          │           │       ├──▶ failed
                │          │           │       └──▶ cancelled   （协作式，终态）
                └──────────┴───────────┘
                     重试 / 让出（preempted → queued）
   blocked：依赖未满足或资源不足（可解释，不是静默等待）
```

### 3.2 三条设计原则（来自本会话踩过的坑）

1. **状态必须只由数据与既定事实决定**——进程重启后不得把死线程报成 `running`
   （GeoKG 的 TaskManager 就踩过这个：只存内存，重启后旧记录会撒谎）。
   因此 `TaskStore` 必须可插拔，且重启时做一次状态对账。
2. **取消是协作式的**——下推的子进程杀不掉，取消语义只能是"当前单元跑完即停在下一个
   边界"（GeoKG 的 `refresh checkpoint` 已验证这个模式）。
3. **响应/视图不得含易变字段**——否则不可比对、不可缓存（`/version` 的教训）。

### 3.3 模型调用闭环

```
model GeoCard（runtime.gpu=cuda, access.endpoint=…）
   │  ① 路由前校验：match_resource(model, compute_caps)
   │       不匹配 → 带理由拒绝（RESOURCE.md 已定此原则），不进执行
   ▼
TaskRouter 选节点/服务 ──▶ InferenceBackend（HTTP/gRPC）
                          └▶ BatchBackend（提交→轮询→回收）
   ▼
结果回收 → 写入 TaskStore + 事件流；产物按 GeoCard 的 IOField 描述登记
```

模型调用的入口**加法式**引入：`geo.capabilities` 广告新能力，新方法（如
`geo.invoke`）只在协商到该能力时使用，老客户端不受影响（遵守 `API_STABILITY.md`
的冻结承诺）。

---

## 四、分阶段计划

### Phase 0 — 冻结设计与现状清账（0.5 周）✅ **已完成**

- ✅ 本路线图 + `docs/GEOTASK.md`（数据模型、状态机、事件契约、失败语义、
  持久化与重启对账、执行后端协议、迁移映射）
- ✅ 迁移映射表：三个"任务"概念 + **四套状态词表**（`web` / `cafe` / `agent` /
  `OgeTaskPoller`）→ 统一的 `TaskState` + `error.code`
- ✅ 失败测试就位：`tests/test_geotask_contract.py`（16 项 strict xfail 契约 +
  4 项文档自洽性）

**评审时发现的四个契约级留白，已在 `GEOTASK.md` 中定死**（这正是 P0 的价值所在）：

| 留白 | 决定 | 位置 |
|---|---|---|
| 事件顺序与重放 | 每任务内 `seq` 严格递增（现有 `cafe.events` 没有序号，客户端无法判断丢帧）；`since=` 重放，超窗口返回 `410` 而不是静默从头 | GEOTASK §4 |
| 幂等键 | `task_id` 即幂等键：重复提交不新建、不重启，返回既有任务 | GEOTASK §5.4 |
| 依赖失败级联 | fail-fast：下游 `blocked → failed(upstream_failed)` 并递归传播；`skipped` 不级联；可选依赖边不做 | GEOTASK §5.3 |
| 占用记账单位 | 按 `cpu_cores` / `memory_mb` / `gpu_count`(+`gpu_kind`) 记**预约**而非实际用量；显存声明了才记 | GEOTASK §2.2 |

另有两处刻意的取舍需要留意：**`skipped` 是终态**（agent 现有语义，"按策略不执行"
≠ "失败"，压进 `failed` 会让失败率失真）；**`timeout` 不是状态**而是
`failed` + `code=timeout`（状态描述"走到哪"，错误码描述"为什么"）。

**验收证据**
- 文档自洽性由机器检查：状态自洽（迁移端点已声明、终态吸收、非终态有出边、
  全部可达、无重复无自环）
- 契约测试 **今天为红**且可证：`pytest tests/test_geotask_contract.py --runxfail -q`
  → 16 项全部 `ImportError: cannot import name 'geotask'`，即契约未实现
- 套件保持绿：**474 通过 / 0 失败 / 16 xfail**（`strict=True`，Phase 1 一旦实现到
  能通过就会 XPASS 并让套件失败，逼着移除标记）

> 用 strict xfail 而不是让套件直接变红：一整套红的测试会淹没**新出现的**回归。
> 想看真实的红，加 `--runxfail` 即可。

### Phase 1 — GeoTask 模型 + 本地执行后端（1.5 周）🟡 **核心完成，入口待接**

- `geonexus/geotask/`：`GeoTask`、`TaskState`、`TaskStore`(memory/JSON)、
  `TaskGraph`（依赖、拓扑、**就绪集并行**）、状态机与取消
- `LocalBackend`（线程池）；事件流统一复用/重构 `cafe/events`
- HTTP + CLI 提交/查询/取消；SSE 进度
- `web.TaskManager` 改为 GeoTask 的**适配器视图**（现有 16 条路由不改契约）

**验收（四条全部达成，且是可证的）**
- ✅ 含 3 步依赖的任务图：无依赖的步骤**真并行** —— `tests/test_geotask_runner.py`
  用 `threading.Barrier(3)` 证明三个任务**同时**在跑（串行会超时失败），不用计时断言
- ✅ 重启后不撒谎 —— `JsonTaskStore` 落盘 → 重新载入 → `reconcile()` 把无执行者的
  `running` 判为 `failed(executor_lost)` 或退回 `queued`（按重试余量）
- ✅ 协作式取消在步骤边界生效，终态 `cancelled` 而非 `succeeded`
- ✅ 套件 **512 通过 / 0 失败**（原 470 + 16 项契约由 xfail 转正 + 26 项新行为）

**已交付**：`geonexus/geotask/`（model / machine / events / store 内存+JSON / graph /
backend / runner）、`LocalBackend` 线程池、`cafe.events` 补 `seq`+`latest_seq`。

**Phase 1 剩余（已明确，不改变契约）**
| 项 | 说明 |
|---|---|
| `web.TaskManager` → GeoTask 适配器 | 需在边界把 `done/queued` 旧词表投影回去（契约 §8.2）；11 项 web 测试是防线 |
| HTTP + CLI 提交/查询/取消 + SSE | 依赖适配器先定型，避免两套任务 API 并存 |
| ~~`agent.PlanExecutor` 改用就绪集~~ | ✅ **已完成**：模板依赖显式化 + 栅栏法证明并行 + 失败文案逐字保留 |
| `TaskRouter` 调度策略 | 属 Phase 2 |

### Phase 2 — CAFE 唯一化为下推引擎（1.5 周）

- `TaskRouter` 合并策略：数据本地性（federation 的归属查表）+ 健康过滤 + 算力余量
  （`resource.py`）+ 合规
- 为 `WorkerNode` 补 **HTTP 传输**（`RemoteWorker`）——这正是 federation 已有的能力，
  搬进 CAFE 而不是让 federation 留着
- `federation.FederatedGeoMCPClient` 改为薄封装，**公开 API 不变**（回归测试守住）
- worker 心跳 + 租约 + 超时回收 + 结果回传

**验收**
- 全仓库只有一处下推实现（federation 内无重复的路由/执行逻辑）
- 两节点下推：杀掉一个 worker 后任务重试到健康节点并成功
- `federation` 的公开 API 与行为回归测试全过

### Phase 3 — 模型可调用闭环（1.5 周）

- `ModelProvider` 抽象 + `InferenceProvider`(HTTP/gRPC) + `BatchProvider`(提交→轮询→回收)
- `match_resource` 前置到路由：**不匹配就不进执行**，且拒绝理由可读
- model card 的 `access` / `runtime` 语义落到调用路径；产物按 IOField 登记
- `geo.capabilities` 广告新能力；新调用方法加法式引入
- 演示：`model card → 匹配算力 → 独立推理服务 → 结果回传`

**验收**
- 端到端闭环 1 条（真实 HTTP 推理服务，非 mock）
- 无兼容算力的模型**带理由拒绝**，而不是执行时才失败
- 批作业：提交 → 轮询 → 回收 全路径可测
- 老客户端（不协商新能力）行为不变

### Phase 4 — 算力调度与占用管理（1.5 周）

- 队列 + 优先级 + 公平；租约与并发上限；GPU/显存记账
- 重试与退避；失败隔离（一个任务炸掉不拖垮节点）
- 占用与队列深度的观测面（指标 + `/health` 式的诊断字段）

**验收**
- N 个并发模型任务在资源约束下**排队而非自由竞争**；超限有可解释状态（`blocked`）
- 占用记账与实际并发一致（可断言）
- 故障注入：单任务失败不影响同节点其他任务

### Phase 5 — 收口（0.5 周）

- `API_STABILITY.md` 增补新公开面；`docs/GEOTASK.md` / `CAFE.md`（新）/ `RESOURCE.md` 更新
- 迁移指南：老 `TaskManager` 用户、`federation` 用户
- README 与 CLI 帮助同步

**验收**：文档与实现一致（有检查：文档里列的公开名必须真实存在）。

---

## 五、明确不做

| 不做 | 理由 |
|---|---|
| 训练 / 微调 / 评测生命周期 | D3 排除；那是另一个产品，混进来会稀释 SDK 的协议与契约定位 |
| 在 GeoNode 进程内托管模型 | D4 排除；占进程、难隔离、显存不可管 |
| 重写 GeoCard / GeoMCP / Registry | 已完成且已冻结，重写是净损失 |
| 图数据库/存储引擎自研 | 已有 `kg.storage`（gzip+NDJSON）与 Neo4j 后端；自研不产生差异 |

---

## 六、风险

| 风险 | 影响 | 对策 |
|---|---|---|
| `federation` 改动导致回归 | 已发布的公开面被破坏 | API 冻结 + 先写回归测试再改实现；Phase 2 单独验收 |
| 状态机与持久化设计不足，后期返工 | 高（地基） | Phase 0 先冻结契约；Phase 1 用"重启对账"测试逼出设计缺陷 |
| 推理服务的契约不统一 | 每个模型一套接法 | `ModelProvider` 抽象 + model card 的 `access.protocol/endpoint` 作为唯一约定 |
| 调度复杂度失控 | 中 | 只做 D3 范围内的：队列/租约/占用/重试；抢占与亲和性留到后续 |
| 与平台/Java 侧的接口漂移 | 中 | 沿用既有约定：能力协商 + conformance 测试（Java 侧已有先例） |

---

## 七、可验证的里程碑判据（汇总）

| 里程碑 | 一句话判据 |
|---|---|
| M1 (Phase 1) | 有依赖的任务图能并行跑就绪步，且重启后状态不撒谎 |
| M2 (Phase 2) | 全仓库只剩一条下推实现，worker 掉线可自愈 |
| M3 (Phase 3) | 模型能端到端被调用；无算力时带理由拒绝 |
| M4 (Phase 4) | 资源约束下排队与占用记账可断言 |
| 全程 | 现有 470 项测试保持全过；`federation` 公开 API 不变 |
