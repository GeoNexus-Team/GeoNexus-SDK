# 扩展演示：遥感栖息地变化（NDWI）—— **不是**原文的方法

论文（Crosby et al. 2026, *Scientific Reports* 16:1916）在第 24 节"还应当如何改进这套优先排序"
里把**加入栖息地变化速率**列为后续方向之一：

> 2. 加入栖息地变化速率：遥感可以识别潮滩、红树林、湖泊水面和养殖塘的长期变化。
>    正在快速丧失的高价值地点，应得到更高风险权重。

论文本身**没有**做这一步，也没有给出它使用的遥感产品、指数或阈值。所以：

- **案例主体**（上一级目录）严格按论文五步组织：框架比较 → 数据汇集 → PC1 与区域校准
  阈值取舍 → 专家核定边界 → 刻画与 χ²。里面没有 NDWI，也没有水面变化图。
- **本目录**保留一条可跑的 NDWI 水面变化链路，作为"原文建议的下一步"的**演示实现**。
  它与论文结论无关，数据是**合成样例**（`make_sample_data.py` 生成，
  栅格文件内部标注 `GEONEXUS_SYNTHETIC=TRUE`，CSV 首行有同样标注）。

## 跑一遍

```bash
cd core/examples/flightway_wetland/extensions/habitat_change
python make_sample_data.py --out data            # 合成样例数据（已提交，可跳过）
python run_extension.py                          # 5 步 → 3 个交付物（写在本目录 output/）
python run_extension.py --pc1-threshold 10       # 换成论文的区域校准阈值：样例数据下无人入选
```

产物：`output/water_change.tif`、`output/priority_sites.csv`、`output/report.html`、`output/run.json`。

| 步骤 | 算子 | 做什么 |
|---|---|---|
| s1 / s2 | `wetland-water-extent` | 基准年/目标年水面提取（NDWI = Green−NIR / Green+NIR） |
| s3 | `wetland-water-change` | 两期水面逐像元对比：损失 / 增加 / 净变化 / 损失率 |
| s4 | `waterbird-priority-score` | PC1：对达到 1% 的种群累加「本地点个体数 ÷ 种群总量」 |
| s5 | `wetland-report` | 渲染报告（结论 / 方法 / 优先地点 / 来源 / 局限） |

> 与案例主体的关系：主体给出**原文结论**（147 处优先湿地、区域校准、χ²），
> 本目录给出**原文建议的下一步**在平台上的样子。两者刻意分开，避免把扩展当成论文方法引用。
