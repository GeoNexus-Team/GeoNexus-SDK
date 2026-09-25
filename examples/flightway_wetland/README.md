# 案例：东亚—澳大利亚迁飞区优先湿地识别（按原文五步复现）

把 Crosby et al. 2026（*Scientific Reports* **16**:1916，DOI
[10.1038/s41598-025-31727-2](https://doi.org/10.1038/s41598-025-31727-2)，CC-BY 4.0）
这篇横跨 10 个亚洲国家的候鸟保护研究，组织成**平台可用、能真跑、产出可下载报告**的案例。

- 事实台账与逐条决策：[`docs/cases/flightway-wetland.md`](../../../docs/cases/flightway-wetland.md)
- 数据来源与许可：[`data/rfi/PROVENANCE.md`](data/rfi/PROVENANCE.md)
- 通用案例模版：[`docs/case-template.md`](../../../docs/case-template.md)
- 扩展演示（原文建议的后续方向，**不是**论文方法）：[`extensions/habitat_change/`](extensions/habitat_change/)

## 一分钟跑通

```bash
cd core/examples/flightway_wetland
python run_case.py                     # 按原文五步跑一遍 → 4 个交付物
python run_case.py --json | head -40   # 机器可读的运行清单
python export_geojson.py               # 147 处站点 → 地图可用的 GeoJSON
```

产物落在 `output/`：

| 交付物 | 是什么 |
|---|---|
| `site-data/sites_normalised.csv` | 147 处优先湿地（名称、坐标、PC1 或名次、达到 1% 的物种数、保护地标记） |
| `pc1/priority_selection.csv` | 逐站点取舍表：用了哪个阈值、为什么入选或落选 |
| `characterise/country_characteristics.csv` | 国别刻画：沿海/内陆、保护状态、HDI 档 |
| `report.html` | 评估报告：框架比较、漏斗、国别表、物种、χ²、局限、数据可用性 |
| `geometry/priority_sites.geojson` | 147 个点要素（真实坐标）+ 分国别子集 + 图例 |

## 这个案例由什么组成（原文五步）

| 步骤 | 算子 | 对应原文 |
|---|---|---|
| s1 | `rfi-framework-review` | 第一步：比较 Ramsar、EAAFP 迁飞区网络、BirdLife IBA/KBA 三套框架与其水鸟标准 |
| s2 | `rfi-site-data` | 第二步：汇集站点与水鸟数据；这里校验 147 处、沿海 91/内陆 56、10 国分布 |
| s3 | `rfi-pc1-select` | 第三步：PC1（`Σ(nᵢ/Nᵢ)×100`，只累加 CSR1 分值 > 1.0 的种群）＋区域校准阈值 10/1 ＋ 3 处专家例外 |
| s4 | `rfi-characterise` | 第五步：沿海/内陆、保护状态、HDI 档，并用 χ² 复算原文统计量 |
| s5 | `rfi-report` | 报告：结论、方法、统计、局限与数据可用性 |

第四步（与各国专家共同核定站点边界）在原文里是**人工过程**，本案例不假装能自动化：
它在报告与台账里如实标注为人工步，配方里不设对应的算子。

## 数据：什么是真的，什么拿不到

- **真的（公开）**：147 处站点的名称、省/邦、中心坐标、达到 1% 的物种与 IUCN 等级、
  >10%/>50% 标记、Ramsar 与迁飞区网络标记、PC1 分值、以及国别汇总（Table 1/3/4）。
  全部来自论文补充材料 Table 3 与正文表格，由 `extract_supp_table3.py` 解析成 CSV。
- **拿不到（受限）**：支撑 PC1 的**逐笔水鸟计数数据**（亚洲水鸟普查、国家监测、eBird、专家资料）。
  原文 Data availability 明确写不公开、可向作者申请。因此案例**不复算** PC1，
  只复核取舍规则与统计量；拿到数据后走 `run_case.py --counts imports/<file>.csv`
  即可接上完整计算路径（字段见补充材料 Table 1）。

## 原文两份公开材料之间的不一致（案例照原样报出，不做人工对齐）

| 项 | 正文 Table 4 | 补充材料 Table 3 | 案例怎么处理 |
|---|---|---|---|
| 受保护或部分受保护 | 108 处 | 粗体标记 117 处 | 报告与图层里两个数字都显示 |
| 入选站点 | 147 处 | 按公开分值严格套阈值只有 145 处（Danzhou-Lingao coast 9.29、Fangcheng coast 8.49 低于 10） | 默认"公开结果为准"保留 147，并把 2 处差异逐条写进报告；`--strict-rule` 可看纯规则结果 |

## 地图怎么画（可视化的约定）

`export_geojson.py` 产出的属性就是为下面这套画法准备的；图例文字与分级写在
`geometry/manifest.json` 里，前端只负责画，不再自己定义区间。

| 视觉通道 | 映射到什么 | 说明 |
|---|---|---|
| 颜色 | `wetland_type`：沿海 / 内陆 | 原文的核心分类（沿海 91、内陆 56，χ²=8.33） |
| 描边 | `protected`：与保护地重叠 / 未重叠 | 原文补充材料用粗体表达；注意与正文 Table 4 的 108 处差 9 处 |
| 大小 | `pc1` 分级（极高 ≥100 / 高 10–100 / 中 1–10 / 低） | 蒙古 11 处只有名次（`rank`）、我国内陆 15 处只有叙述 ⇒ `pc1` 为空，画成固定小点并标注"原文未公布分值" |
| 筛选 | `country`（可只画一国，已预生成分国别文件） | 原文明确提醒：国别间不宜直接比较 |
| 标注 | `designations`（R=拉姆萨尔、F=迁飞区网络）、`over_10pct` / `over_50pct` | 原文以 * 与 ** 标注超过种群 10% / 50% |

原文图 3 的"单物种比例圆"只能做到**是/否达到 1%** 与星标分级：精确百分比未公开。
地图上选定一个物种时，应只高亮 `site_species.csv` 里出现该物种的站点，并注明"仅表示达到 1%，
不代表具体比例"。

## 参数：哪些能改

| 参数 | 默认 | 能不能改 |
|---|---|---|
| `threshold_prc_mongolia` | 10 | **能改**：作者的区域校准选择，改成 1 就是敏感性分析 |
| `threshold_others` | 1 | **能改** |
| `include_expert_exceptions` | true | **能改**：关掉就纯粹按 PC1 ≥ 10 |
| `include_published_sites` | true | **能改**：关掉得到纯规则的 145 处 |
| `one_percent_rule` | true | **不能改**（`scope: fixed`）：1% 是 PC1 的定义，关掉算出来的不是 PC1 |
| `counts` | 空 | 可选：导入受限的逐笔计数数据 |

```bash
python run_case.py --threshold-prc-mongolia 1 --threshold-others 1   # 取消区域校准
python run_case.py --no-expert-exceptions                            # 去掉专家例外
python run_case.py --counts imports/counts.csv                       # 导入受限计数
```

## 可见性分层

案例自身是 `public`，但它引用了受限的逐笔计数数据（`required: true`），
所以**组合后的可见性取最严值 `restricted`**：拿不到那份数据的人，整案不可见
（平台返回 404）。这不是保守过度 —— 公开的壳不能成为绕过受限内容的通道。
平台演示用的公开版本只绑定公开组成项（`scripts/seed-flightway-case.mjs --public`）。
