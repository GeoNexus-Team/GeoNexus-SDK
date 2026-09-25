# 案例模版骨架（v1.0）

复制这个目录，按 `docs/case-template.md` 填三份文件：

| 文件 | 填什么 |
|---|---|
| `case.card.yaml` | 案例身份、时空范围、**组成**、**参数契约**、`recipe_ref` |
| `recipe.yaml` | 步骤（DAG）、依赖、`step://` 引用、**交付物**声明 |
| `datasets.md` | 数据槽位表 + 合成/示意标注 + 数据快照 |

填完先自测：

```bash
python -c "from geonexus.geocard import load_geocard; print(load_geocard('case.card.yaml').validate().id)"
python -c "from geonexus.recipe import load_recipe; print(load_recipe('recipe.yaml').validate().id)"
python -m geonexus.cli recipe plan recipe.yaml --param <name>=<value>   # 物化一次，看清会发生什么
```

> 占位符形如 `<...>`：**填完必须全部消失**。留着占位符的案例等于没有契约。
