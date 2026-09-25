# 案例：东亚—澳大利亚迁飞区 · 湿地水面变化与优先地点评估

把一篇横跨 10 国的候鸟保护研究（*Scientific Reports* 2026，DOI
[10.1038/s41598-025-31727-2](https://doi.org/10.1038/s41598-025-31727-2)）组织成
**平台可用、能真跑、产出可下载报告**的案例。这一版同时充当「候鸟—湿地」专题模版的实例。

- 事实台账与决策记录：[`docs/cases/flightway-wetland.md`](../../../docs/cases/flightway-wetland.md)
- 通用案例模版：[`docs/case-template.md`](../../../docs/case-template.md)
- 原文存档：[`docs/cases/source/`](../../../docs/cases/source/)

## 一分钟跑通

```bash
cd core/examples/flightway_wetland
python make_sample_data.py --out data            # 生成合成样例数据（已提交，可跳过）
python run_case.py                               # 真跑：5 步 → 3 个交付物
python run_case.py --pc1-threshold 10            # 换成论文的区域校准阈值（样例数据下无人入选）
python run_case.py --region yangtze --target-year 2020
```

产物落在 `output/`：`water_change.tif`、`priority_sites.csv`、`report.html`、`run.json`（含每个
交付物的路径、字节数、sha256 与两次可见性判定）。

## 这个案例由什么组成

| 步骤 | 算子 | 做什么 |
|---|---|---|
| s1 / s2 | `wetland-water-extent` | 基准年/目标年水面提取（NDWI = Green−NIR / Green+NIR） |
| s3 | `wetland-water-change` | 两期水面逐像元对比：损失 / 增加 / 净变化 / 损失率 |
| s4 | `waterbird-priority-score` | **PC1**：对每个达到国际重要数量（≥ 种群 1%）的种群累加「本地点个体数 ÷ 种群总量」 |
| s5 | `wetland-report` | 渲染报告（结论 / 方法 / 优先地点 / 来源 / 局限） |

交付物（方案 `outputs` 里声明的结论，不是每一步的副产品）：`water_change_map`（data）、
`priority_sites`（data）、`report`（knowledge）。

```
recipe.yaml ──materialize()──▶ TaskGraph ──逐个执行──▶ 产物 ──▶ 交付物
                                   │
                         上游产物按 step://<step>/<output> 传给下游
```

## 方法忠实度：哪一步对应论文哪一节

| 论文 | 本案 | 忠实度 |
|---|---|---|
| 第一步 比较国际湿地保护框架（湿地公约 / EAAF 伙伴关系 / IBA） | **未实现**（属于制度比较，不是计算） | — |
| 第二步 收集候选湿地与水鸟数量 | 数据槽位：`waterbird_counts.csv` + `species_populations.csv` | 形状一致；数值为合成 |
| 第三步 按统一阈值算优先分值（PC1） | `waterbird-priority-score`（1% 规则 + 比例求和 + 入选阈值） | 按原文描述实现 |
| 第四步 与各国专家核定地点边界 | **未实现**（需要专家，不是算法） | — |
| 第五步 分析类型与保护状况（沿海/内陆、是否受保护） | 部分：`sites.csv` 带 `kind` 与 `protected_status`，报告未展开 | 待补 |

原文第七节的**区域校准**（中国、蒙古阈值 10，其余八国 1）作为 `pc1_threshold` 参数的说明保留下来。

## 数据槽位（替换真实数据就看这张表）

| 槽位 | 现在是什么 | 换成什么 | 影响 |
|---|---|---|---|
| `data/<region>/stack_<year>.tif` | **合成** 2 波段（green/nir）反射率栈，160×240 | Sentinel-2 L2A（B03/B08）或 Landsat 地表反射率，转成同样的两波段顺序 | 水面提取结果 |
| `data/<region>/waterbird_counts.csv` | **合成** 8 地点 × 20 物种计数 | 亚洲水鸟普查（AWC）/ 各国监测 / eBird 记录，保持 `site_id, species, count` 列 | PC1 分值 |
| `data/<region>/species_populations.csv` | **示意**种群量 | EAAF 首次保护状况评估的 **1% 阈值**（每物种一个数） | 达标判定与 PC1 |
| `data/<region>/sites.csv` | **示意**坐标（落在 AOI 内） | 论文 147 处优先湿地的**核定边界** | 图层、可见性、空间统计 |
| 研究区窗口 | 约 1°×1° 的示意窗口 | 具体地点的边界（论文第四步的产物） | 全部空间结论 |

所有合成文件内部都带 `GEONEXUS_SYNTHETIC=TRUE`（栅格写进 GeoTIFF 标签，表格写进首行注释）。
`run_case.py` 的输出里也标注了 `synthetic`，报告页首会显示醒目提示。

## 可见性分层

案例自己的档位是 `public`，但组成里有一份**受限**的精确点位
（`geocard.eaaf.priority-sites-restricted`）。按可见性继承规则：

```
作者/管理员视角   visible=True   visibility=restricted   （组成项都解析得到）
访客视角          visible=False  missing=[精确点位]      ⇒ 平台返回 404
```

精确点位之所以受限：具体坐标会指向可被干扰的停歇地。用"案例简介是公开的"绕过是不允许的，
所以整案对访客不存在 —— 这条规则在 `run_case.py` 的 `visibility` 段落里每次运行都会重算并写进
`run.json`。

## 已知缺口（下一版要补的）

1. **147 处地点是点，不是面**：论文强调"功能完整的湿地，而不是地图上一条边界"，
   所以真实案例需要地点**边界**（面）与类型（沿海/内陆/人工）。
2. **单一 AOI vs 迁飞网络**：案例目前跑在一个示意窗口上。要表达"网络"，需要把 147 处地点
   作为**一组图层**（点/面）而不是一个 bbox —— 这正是四级 LOD 与血缘弧线的用武之地，
   平台侧的 `/api/cases/:id/layers` 已经能承载，但还没有把"网络"这一层做出来。
3. **运行身份未从平台注入**：报告里的 `run_id` 目前由执行器补（`run_case.py`），
   平台 `/api/recipes/run` 尚未把 run_id 注入到步骤参数里。这是 SDK/平台都该补的一小块。
4. **AI 与专家环节没有落点**：原文第二步（收集）、第四步（与各国专家核定边界）是本案
   跑不出来的部分。它们应该作为案例的**人工步骤**显式建模，而不是假装算法能做。
