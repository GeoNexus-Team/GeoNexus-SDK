# GeoCard 登记与轻量边界（1.1）

`pip install geonexus-sdk` 仅安装 Pydantic、JSON Schema 和 YAML 依赖。登记、静态契约校验和类型契约不需要 GIS/Web/MCP 运行库。`[http]` 用于 HTTP 客户端；`[mcp]` 用于官方 MCP；`[server]` 用于控制服务；`[geo]` 用于 GIS 执行；`[all]` 适合现有完整 Demo。原有高层模块保留为可选模块，后续可按兼容计划拆分独立包。

## 静态、部署与状态

- GeoCard：资源身份、空间/时间语义、输入输出契约、许可证、来源、资源修订。
- ServiceBinding：部署协议、连接地址、工具名、所有者 GeoNode、凭据引用。
- ServiceState：探测结果、观测时间、到期时间，过期即不可作为当前可用性证据。
- RegistryEntry：提交者、审核状态与审计历史，独立于 GeoCard 自我声明。

`schemas/` 是三种契约的规范源；构建时写入 wheel，并检查源码副本。GeoCard 仍使用既有 1.0 Schema；本轮类型 profile 以 `x-geonexus` 扩展实现，尚不是外部标准。

## 四种登记 profile

通用必填：access、license、provenance.provider、扩展字段 revision。算子映射为 GeoCard `type: skill`，模型和算子必须声明完整 input_schema/output_schema。模型必须有 model_id 或不可变资产标识；算力必须有已知正 CPU、可解析的正内存和执行后端列表。数据未声明空间语义时给出警告，不假定兼容性。

支持 JSON/YAML 描述符、STAC Item、MCP Tool 描述、OGC Process 描述和指定 OpenAPI operationId。导入结果都是需提供者核对的草稿，缺失来源/许可证/修订不会自动补造。`source_digest` 是元数据摘要，不等同于数据或模型文件摘要。

```sh
georegister inspect metadata.json --kind operator
georegister init metadata.json --kind operator --node http://127.0.0.1:9000 --out draft.json
georegister validate draft.json
georegister probe draft.json
georegister submit draft.json --registry http://127.0.0.1:9100
```

提交凭据从 `GEONEXUS_REGISTRY_API_KEY` 环境变量读取。数据文件 endpoint 不能替代 binding.node_url；后者必须明确填写。探测仅说明 endpoint 可达，不能证明资源质量、算力容量或执行权限。

## 审核与执行

公开登记模式须配置具名 principal：提供者只有 registry:submit，审核者只有 registry:review，管理员 registry:admin。HTTP 提交强制 pending、重置自报 verified，并保留可信身份；客户端不能通过 body.by 假冒审核者。私有模式保留旧注册行为，CLI 新提交默认 pending。

```sh
geonexus registry start --port 9100 --public-submissions --principals private-principals.json
```

私有 principals 文件示例（生产应使用凭据管理器并保护文件权限）：

```json
{"provider-key-placeholder":{"subject":"provider","scopes":["registry:submit"]},"review-key-placeholder":{"subject":"reviewer","scopes":["registry:review"]}}
```

GeoMCP 桥提供 geo_capabilities、geo_describe、geo_execute、geo_invoke、geo_plan、geo_health。MCP 工具执行在 stdio 使用 GEONEXUS_NODE_API_KEY，在 HTTP 使用 X-API-Key。读取保持公开。contract_binding、pushdown_spec、federated_sources 尚未接入实际执行，明确拒绝而非忽略。模型调用默认严格校验声明的 CPU/内存要求；未知容量拒绝执行，旧部署可显式关闭 strict_compute。

Agent Skill 的 SKILL.md 是指导文字，不是 GeoCard skill 算子，也不能提升主机授权。公共产品包可以把卡片、绑定、Agent Skill、配方和 viewer 放在一起，安装绑定不意味着下载或移动数据与模型。
