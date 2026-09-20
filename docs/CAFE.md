# CAFE — 唯一的计算下推引擎

**状态**：Phase 2 已完成（合并 federation 与 CAFE 两套下推）。

---

## 一、为什么会有两个实现，以及为什么必须合并

CAFE 的初衷是"数据、计算与任务协同"：把计算推到数据旁边。但代码库里曾经同时存在
**两套下推**：

| | `geonexus.federation` | `geonexus.cafe` |
|---|---|---|
| 接线状态 | **已接线**（agent / web / demos 都在用） | **零生产调用点**（只有自己的测试） |
| 传输 | HTTP + GeoMCP（自己维护 `GeoMCPClient` 缓存） | 无（只支持进程内 `WorkerNode` 对象） |
| 路由 | 按 GeoCard 归属查表 + 健康过滤 | 按 `metadata` 打分（`TaskRouter`） |
| 执行 | 远端节点跑技能 | 本地子进程沙箱跑代码 |
| 失败处理 | 直接抛异常 | 无（没有换点、没有租约） |

结果是：**接了线的那套没有策略，有策略的那套没人用**。合并的收益是双向的——
CAFE 拿到了它缺的传输，federation 拿到了它缺的路由策略、健康降级与租约。

## 二、合并后的分工

```
                    ┌──────────────────────────────┐
                    │  CentralServer（唯一路由）    │
                    │  硬过滤 → 打分 → 兜底          │
                    │  租约 · 失败降级 · 换点重试     │
                    └───────┬──────────────┬───────┘
                            │              │
              ┌─────────────┴───┐   ┌──────┴──────────────┐
              │ WorkerNode      │   │ RemoteWorker        │
              │ 进程内子进程沙箱 │   │ HTTP / GeoMCP       │
              │ 载荷：code      │   │ 载荷：skill         │
              └─────────────────┘   └─────────────────────┘
```

谁执行哪种载荷由 `capabilities()` **声明**（`WorkerCapabilities.payload_kinds`），
router 在打分前做**硬过滤**：代码任务不会派给只会跑技能的远端，反之亦然。失败因此
发生在路由阶段并带可读原因，而不是执行到一半才报错。

`federation.FederatedGeoMCPClient` 保留的是**注册中心发现 + 归属分组**（"每张卡归
哪个节点"），因为那是它的对外语义（`execute` 要按节点聚合结果）。传输、执行、租约
全部交给 CAFE。公开 API 与返回形状一字未改。

## 三、路由：三阶段，顺序有意义

1. **硬过滤**——载荷类型兼容 + 健康（被降级的不进候选）。
2. **打分**——数据本地性（能解析多少输入）→ 算力是否够 → region/compliance 标签。
3. **兜底**——剩下的第一个。

`RoutingDecision` 同时给出 `reason`（为什么选它）与 `excluded`（为什么没选别人）。
"为什么是这个节点"必须可回答，否则多目标路由就是个黑盒。

算力比较**复用** `geonexus.resource.match_resource`，通过一个最小的"请求卡" shim
（它只读 `card.id` 与 `card.runtime`）。复用是为了复用它的比较语义与**理由文案**
（"model needs 'cuda', node has 'none'"），而不是为了复用读卡的方式。

## 四、租约与故障转移

`dispatch()` 在执行前登记租约（`task_id → worker`，带到期时间），执行完释放。
`reclaim_expired()` 回收过期租约并**返回**它们——重试策略属于调度层（GeoTask），
CAFE 只负责报告事实。

故障转移的核心在 `dispatch(retries=None)`："试完所有候选"。
只有 `result.retryable` 的失败才换点——节点拒绝、连不上可以换；**输入缺失换谁都没用**，
所以不换（也不把节点标记为不健康，那会白白降级一个好节点）。

## 五、三个被修掉的真实缺陷

1. **`PushdownResult(status="failed")` 用字符串**——`CentralServer` 的两条错误路径
   （没有 worker / worker 不存在）传的是字符串，而 `to_dict()` 读 `self.status.value`，
   于是**任何序列化这两个结果的调用都会 `AttributeError`**。因为它零调用点，一直没人
   踩到。现在 `__post_init__` 把 `status` 归一成 `TaskStatus`，非法值在构造时就抛。
2. **`RemoteWorker` 必须在 `dispatch_to` 之前注册**——第一版漏了这一步，症状是
   `worker not found: <node>`。`dispatch_to` 只认已注册的 worker，这是有意的：
   否则路由表里会出现没人登记过的节点。
3. **`_node_client` 的 check-then-act 竞态**（并行执行时会泄漏连接池）——合并时
   连同锁一起搬进 `RemoteWorker._connection`。

## 六、验收

| 判据 | 证据 |
|---|---|
| 全仓库只有一处下推实现 | `tests/test_cafe_router.py::TestSinglePushdownImplementation` 用 **AST** 断言 federation 里没有 `GeoMCPClient` / `httpx` / `_node_client`，并列出"谁可以持有下推客户端"的白名单 |
| 杀掉一个 worker 后重试到健康节点并成功 | 两条：一条用从未监听的端口，一条**先成功跑一次再停掉真实节点**（真实故障的样子） |
| federation 公开 API 与行为回归全过 | `test_federated.py` / `test_federation_deep.py` / `test_skill_registry.py` / `test_ogc_pipeline.py` 未改动即通过 |
| 载荷/健康/算力是硬过滤 | `TestHardFilters` 五条，含被排除原因的可读性 |
| 租约登记与回收 | `TestLeases` 两条（执行中持有、过期可回收且幂等） |

> 断言用 AST 而不是字符串搜索，是因为第一版用字符串搜索时被文档字符串里的解释性
> 文字绊倒，报出两个假阳性（`httpx.Client 可并发使用`、`Bridges GeoMCP execution to
> OGC`）。按代码里的标识符判断才说明问题。

## 七、明确不做

| 不做 | 理由 |
|---|---|
| 抢占式调度 | 需要检查点配合，属 Phase 4 之后 |
| 把"归属分组"也搬进 CAFE 的路由 | federation 的 `execute` 契约就是按节点聚合结果，分组是它的语义而非传输细节 |
| 给沙箱加取消入口 | 子进程可杀，但协议里没有取消语义；要加得先定义"取消一个正在下推的代码"是什么意思 |
| 承载模型调用 | 模型调用**不走下推**：它是"算力门禁 → provider"（见 `MODEL.md`），算力选择由 `match_resource` 决定，而下推解决的是"跨节点跑一段代码"。两者都需要，但不是同一件事 |
