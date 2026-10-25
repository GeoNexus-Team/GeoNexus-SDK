# SDK 兼容性报告：Platform v0.9.2 / GeoHarness 0.2.9 × released v1.1.0

## 背景

- Platform v0.9.2 的部署断言 `geonexus-sdk==1.1.1.dev20261015`
  （`deploy/unified/run_services.py:160`、`verify_installed.py:12`）。
- `deploy/unified/cohort-manifest.json` 记录该 wheel 由
  `feat/unified-runtime-20261015`（base `27f1b7d`，为 released v1.1.0 的祖先）
  **加 49 个未提交文件** 构建，`committed_at_review_time: false`，
  wheel SHA256 `566fedfd…`。
- released v1.1.0（`release/1.1.0`，`8b6b4e8`）在时间线上更新，含该 base 之后的 3 个提交，
  但**不包含**部署实际使用的四处 API。

## 四处差异（以平台/客户端代码为证据）

| # | API | 使用方（证据） | released v1.1.0 | 本分支处理 | 待评审 |
|---|---|---|---|---|---|
| 1 | `geonexus.geocard.contracts`：`ContractRequest/Report/ResultManifest/validate_contract` | `platform/services/v02/integration.py:26` | 不存在 | 已实现（自包含校验：CRS/范围/时间/波段/单位/`no-export`） | 导出策略与 findings 字段是否与 dev 版一致 |
| 2 | `create_mcp_server(server, http_guarded=...)` 且返回对象可 `await call_tool()` | `platform/services/v02/integration.py:348,723` | 无该参数、无 call_tool | 已实现 `GeoMCPBridge`（进程内分派 + 委托 MCP 服务器属性）；`http_guarded` 给 Streamable HTTP 加 `X-API-Key` 门槛 | `http_guarded` 的准确语义（鉴权？限流？）；钥匙由谁校验 |
| 3 | `geonexus.registration`：`RegistrationError/safe_url/make_draft/validate_profile` | `geoharness/src/geo_harness/app.py:21-24`、`account.py:3` | 不存在（只有 `geonexus.register` 低层管线） | **临时适配**：`safe_url` 完整实现；`make_draft/validate_profile` 复用 `register.build` 并对 metadata 做最小约定 | metadata/binding 字段契约（草稿→提交→本地产品） |
| 4 | `RegistryServer.__init__(..., public_submissions=...)` | `geoharness/src/geo_harness/app.py:75` | 无该参数 | **未实现**（拒绝加无行为桩） | “公开提交”是审核队列、目录可见性，还是第三方投稿开关？ |

## 影响与验证

- 补齐 #1–#3 后：Platform `services/v02` 可启动；`services/v02/test_case_stories.py` **6/6 通过**
  （Durham 包完整性、68 tracts、6 步 succeeded）。
- GeoHarness 单元测试 **44 项通过**（policy / map-context / freeze / agents / observability / commands）。
- app 级 HTTP 冒烟仍被 #4 阻断（`create_app` 构造 `RegistryServer` 时 TypeError）。

## 建议

1. **以已提交的源码发布 tagged v1.1.1**（而非 49 个未提交文件），恢复可复现性；
2. 对 #1–#4 的语义给出确认或修正意见；#4 需要明确行为后再实现；
3. 在 SDK CI 增加“消费方契约测试”（本文件的四处调用），避免再次出现“部署能用、仓库不能构建”。

## 复现

```bash
git -C tmp/sdk-repo checkout release/1.1.1
pip install --no-deps -e tmp/sdk-repo
PYTHONPATH=src python -m pytest tests/test_geocard_contracts.py tests/test_mcp_bridge.py
```
