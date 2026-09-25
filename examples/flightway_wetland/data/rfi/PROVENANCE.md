# 数据来源与许可（RFI 优先湿地数据集）

本目录的五个 CSV **不是**我们生成的，而是把论文补充材料里的表格解析成机器可读形式：

| 文件 | 来自原文哪张表 |
|---|---|
| `sites.csv` | Supplementary Table 3：147 处优先湿地（名称、省/邦、中心坐标、达到 1% 的物种数、PC1 分值或名次、R/F 标记、粗体=与保护地重叠） |
| `site_species.csv` | Supplementary Table 3 的"EAAF SPECIES (≥1%)"列（逐条：英文名、学名、IUCN 等级、>10%/>50% 标记） |
| `country_summary.csv` | 正文 Table 4：国别 × 沿海/内陆 × 保护状态 × HDI 档（并并入 Table 1 的三套框架站点数） |
| `framework_designations.csv` | 正文 Table 1：Ramsar / EAAFP 迁飞区网络 / 湿地 IBA-KBA 在 10 国的站点数 |
| `assessed_sites.csv` | Supplementary Table 2：各国被评估的候选站点数 |

## 出处与许可

- 论文：Crosby, M., Wee, S. Q. W., Yong, D. L. et al. *Identifying priority wetland sites in the
  East Asian–Australasian Flyway for migratory bird conservation.* **Scientific Reports 16, 1916 (2026)**.
  DOI [10.1038/s41598-025-31727-2](https://doi.org/10.1038/s41598-025-31727-2)（CC-BY 4.0）。
- 正文 PDF（Version of Record，CC-BY）：
  `https://eprints.soton.ac.uk/id/eprint/507398/2/s41598-025-31727-2.pdf`
  sha256 `700eefc268c903c27f7fc99ddf99ca1ef528cc94a1316649c05fafed466fe747`
- 补充材料 PDF（Supplementary Information，含 Table 1–3）：
  `https://static-content.springer.com/esm/art%3A10.1038%2Fs41598-025-31727-2/MediaObjects/41598_2025_31727_MOESM1_ESM.pdf`
  sha256 `989addbaca1d9fd111be15130ec68985ba18f469cdd927452459fca965f9244c`

**本仓库不随镜像分发这两份 PDF**（第三方作品）。需要复现时按上面的 URL 下载，再用
`extract_supp_table3.py --pdf <supplementary.pdf> --out data/rfi --strict` 重新生成。

## 原始计数数据是受限的（必须如实说明）

支撑 PC1 计算的**逐笔水鸟计数数据**（亚洲水鸟普查、国家监测、eBird、专家资料）由
Wetlands International 许可提供，论文的 Data availability 声明明确写"not publicly available，
可向作者申请"。因此：

- 本数据集覆盖的是**站点级结果**（原文公开发表的部分），不是原始计数；
- 案例**不复算** `PC1 = Σ(nᵢ/Nᵢ)×100` 的原始计算（没有 nᵢ 与 CSR1 阈值明细），
  而是**校验**原文的取舍规则（中国/蒙古阈值 10、其余八国 1，加 3 处专家例外）能否从公开数据
  重现 147 这个结果，并把这一步写进报告的"数据可用性"一节；
- 若拿到受限数据集（表结构见 Supplementary Table 1，字段：Seq / Reference / English name /
  Scientific name / IUCN / Country: Province / Locality name / Site name / Count / Selected count /
  Date / Primary Source / CSR1 / CSR1 Score），把它放进 `imports/` 并运行
  `run_case.py --counts imports/<file>.csv` 即可走完整计算路径。

## 我们的推断（不是原文的字段）

- `wetland_type`（沿海/内陆）：原文只按国别公布合计（Table 4），逐站点未标注。我国站点按原文
  "沿海省 → 内陆省"的分组直接判定；蒙古、老挝按 Table 4（全部内陆）判定；其余国家按站点名里的
  沿海/内陆线索判定，**并用 Table 4 的国别合计做约束**（校验不通过即报错）。因此该列是
  **推断**，`type_source` 字段标出是 `paper-section`（原文分组）还是 `inferred`（我们推断）。
- `overlaps_protected_bold`：原文补充材料 Table 3 的**粗体**标记（脚注：Sites not in bold do not
  overlap with protected areas）。共 117 处，而正文 Table 4 记 108 处"受保护或部分受保护"。
  **两者相差 9 处，原文没有解释差异来源**，本案例两个数字都保留，不做人工对齐。
