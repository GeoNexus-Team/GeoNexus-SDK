# 案例模版骨架（v1.1）

复制这个目录，按 `docs/case-template.md` 填三份文件：

| 文件 | 填什么 |
|---|---|
| `case.card.yaml` | 案例身份、时空范围、**组成**、**参数契约**、`recipe_ref` |
| `recipe.yaml` | 步骤（DAG）、依赖、`step://` 引用、**交付物**声明 |
| `datasets.md` | 数据槽位表（含三层数据）、已知不一致、合成标注、可复算校验量、数据快照 |

填完先自测：

```bash
python -c "from geonexus.geocard import load_geocard; print(load_geocard('case.card.yaml').validate().id)"
python -c "from geonexus.recipe import load_recipe; print(load_recipe('recipe.yaml').validate().id)"
python -m geonexus.cli recipe plan recipe.yaml --param <name>=<value>   # 物化一次，看清会发生什么
```

如果这个案例是**复现已发表研究**，另按 `docs/case-template.md` 第七节处理：
数据分三层（公开结果 / 受限原始 / 我们的推断）、两份公开材料不一致时两个数都报、
至少用一个可复算的统计量证明方法真的在跑、论文点名的"未来方向"另放 `extensions/`。

> 占位符形如 `<...>`：**填完必须全部消失**。留着占位符的案例等于没有契约。
