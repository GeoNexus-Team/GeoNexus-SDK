# 迁移指南（v1.0 → Phase 1–5）

> 面向两类既有用户：用过 `geonexus.web.TaskManager` 的，和用过
> `geonexus.federation.FederatedGeoMCPClient` 的。
> 相关契约：`GEOTASK.md`（任务）、`CAFE.md`（下推）、`MODEL.md`（模型调用）。

## 0. 一句话结论

**公开 API 没有破坏性变更，现有代码不需要改。** 这一轮做的是把 SDK 里原先**四套
互不相干的任务/状态概念**收口成一套，并把"模型能被执行"这条断掉的链路接上——
都是"把契约接到既有入口"，不是改契约。

真正需要你动手的只有一种情况：**你碰过内部实现**（`_` 前缀、`web.tasks` 的私有
属性、`federation` 的 `_node_client`）。下面的对照表照着改即可。

## 1. 如果你用 `geonexus.web.TaskManager`

公开方法一个都没变，行为也没变（**11 项既有测试未改动即通过**）：

| 方法 | 状态 |
|---|---|
| `submit(goal=/skill=, …)` | 不变 |
| `get(task_id, include_result=)` | 不变（返回的 `dict` 形状不变） |
| `list(include_result=)` | 不变 |
| `cancel(task_id)` | 不变（`TaskNotCancellableError` 语义不变） |
| `should_cancel(task_id)` | 不变 |
| `update_progress(task_id, progress, message=)` | 不变 |
| `wait(task_id, timeout=)` | 不变 |
| `close()` | 不变 |

对外的状态词表也没变：`QUEUED` / `RUNNING` / `DONE` / `FAILED` / `CANCELLED`。
内部状态与它之间是**边界投影**（`WEB_TO_STATE` / `STATE_TO_WEB`），所以外部字符串
不随内部模型漂移——内部新增了 `created` / `scheduled` / `blocked` / `skipped`，
它们各自映射到既有词表，客户端看不到新词。

### 如果你碰过内部

| 旧写法 | 现在 | 备注 |
|---|---|---|
| `manager._tasks[...]` | `TaskStore`（`manager.store`） | 状态唯一归属地；别再自己存一份 |
| `task.status = "done"` | `store.transition(task_id, TaskState.SUCCEEDED)` | 迁移只能走 store，校验与事件都由它产生 |
| 自己维护 `should_cancel` 标志 | `store.request_cancel(task_id)` + 执行体读 `ExecutionContext.should_cancel()` | 取消仍是**协作式**（见 §4） |
| 自己拼任务 dict 给前端 | `geonexus.web.tasks.project(task)` | 投影集中在边界 |
| 直接 `try/except` 收任务异常 | 交给 `LocalBackend` | 任何执行体异常都变成该任务的 `execution_error` |

### 新增的只读工具

```bash
geonexus task list                     # 列出任务（含 blocked 与理由）
geonexus task show <task_id>           # 单任务详情
geonexus task events <task_id>         # 事件回放（带 seq）
geonexus task reconcile --store <path> # 崩溃后对账：把无主的 running 改回 queued
```

CLI **刻意不做**提交与取消：只有拥有任务的进程能在步骤边界响应取消标志，
异进程置标志无人读，报"取消成功"就是撒谎。详见 `GEOTASK.md` §10.1。

## 2. 如果你用 `geonexus.federation.FederatedGeoMCPClient`

公开 API 与线上行为**完全不变**（4 个 federation 测试文件未改动即通过）：
`resolve_nodes` / `discover` / `node_health` / `discover_skills` / `search_skills` /
`search` / `execute_skill` / `execute` / `close`。

变化在**内部**：它不再是"每个节点一个 `GeoMCPClient`"的自己实现，而是 CAFE 的薄
封装（`CentralServer` 路由 + `RemoteWorker` 传输）。

| 旧内部 | 现在 |
|---|---|
| `client._node_client(url)` | 无此方法；用 `CentralServer.worker_for(url)` / 直接构造 `RemoteWorker` |
| 自己拼 `PushdownRequest` 并直连节点 | `CentralServer.route()` → `dispatch()`（三阶段：本地性 → 健康 → 余量） |
| 自己处理节点失败 | `CentralServer.mark_unhealthy()` + 租约回收（`reclaim_expired()`） |
| `geonexus.federation` 里持有 httpx 客户端 | 唯一持有下推 HTTP 的是 `cafe/remote.py`（有 AST 断言守住） |

**为什么值得迁移**：以前"下推"有两份实现（federation 一份、CAFE 一份），修一个
bug 要改两处、测两遍，还出现过只修一边的情况。现在只有一处。见 `CAFE.md`。

## 3. 新能力（可选采用）

### 3.1 模型调用（Phase 3）

```python
from geonexus.model import invoke_model

outcome = invoke_model(model_card, {"image": "s3://…"}, capabilities=caps)
```

配套的协议方法是 `geo.invoke`（**加法式**：老客户端忽略它，老节点返回
`METHOD_NOT_FOUND`）。要排成任务则用 `geonexus.model.ModelBackend` +
`GeoTask(kind=TaskKind.MODEL)`。细节见 `MODEL.md`。

### 3.2 节点内调度（Phase 4）

```python
from geonexus.geotask import Scheduler, Reservation, TaskRunner

runner = TaskRunner(store, backend, scheduler=Scheduler(Reservation(cpu_cores=8)))
```

**不传 `scheduler` 就是旧行为**（有不限量的容量），所以这一步是纯加法。
传了之后：容量不足的任务进 `blocked`（**带可读理由**），需求超过节点总容量的
任务直接以 `no_compatible_compute` 拒绝。细节见 `GEOTASK.md` §10。

### 3.3 任务图并行（Phase 1）

```python
from geonexus.geotask import TaskGraph, TaskRunner

report = TaskRunner(store, backend).run(TaskGraph.from_tasks(tasks))
```

`plan-executor` 与 `reflective-executor` 已内部改用同一套就绪集并行（模板依赖
`${stepN.outputs.k}` 现在会被**显式化**成 DAG 边，不再靠串行顺序"碰巧能用"）。

## 4. 会让人"觉得变了"的几点（其实是修 bug）

| 现象 | 解释 |
|---|---|
| 取消请求后任务又跑了一会儿才变 `cancelled` | 取消一直是**协作式**的：执行体在**步骤边界**才看标志。子进程杀不掉，这是设计而不是延迟 |
| 默认不重试 | `RetryPolicy.max_attempts` 默认 1 = 不重试。以前怎样现在还是怎样；要重试就显式设 >1，退避由 `backoff_for()` 算 |
| 某些失败变成 `blocked` 再变 `failed` | 状态机不允许 `created/queued → failed`，必须先经 `blocked`（契约 §3.1）。这是事件流里多一条 `state_changed`，不是语义变化 |
| `LocalBackend.cancel()` 返回 `False` | 线程不可中断，返回 `False` 是**如实汇报**"我没有能力撤销它"，而不是假装成功 |
| 一个任务的后端异常不再中断整张图 | `submit` / `poll` / 执行三处都已隔离：失败收成那一个任务的失败 |

## 5. 升级检查清单

1. `pip install -e core`（或 `-e ./core`；`geonexus-execution-plane` 同理）。
2. 跑一遍你自己的测试。若用了 `TaskManager` / `FederatedGeoMCPClient` 的**公开**
   方法，应当零改动通过。
3. 若报 `AttributeError`，对照 §1 / §2 的内部对照表——你碰的是内部实现。
4. 有模型要跑：按 `MODEL.md` 注册 model card（`type: model` + `access.endpoint`
   + `runtime`），然后 `geo.invoke` 或 `ModelBackend`。
5. 想限流：给 `TaskRunner` 传 `Scheduler`，并把额度写进
   `ResourceRequirement`（模型卡可自动换算：`ResourceRequirement.from_geocard_runtime`）。

## 6. 兼容性承诺

- 公开 API 遵循 `API_STABILITY.md`：破坏性变更只会在 MAJOR 版本发生。
- GeoMCP 原有四方法（`geo.capabilities` / `geo.describe` / `geo.execute` /
  `geo.health`）与 JSON-RPC 信封在 1.x 冻结；`geo.invoke` 等新能力走能力协商。
- GeoCard schema 兼容仍在：`geocard_version` 同时接受 `0.1` 与 `1.0`。
