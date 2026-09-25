"""候鸟—湿地案例：契约、方法链路与可见性分层的测试。

这个案例的价值在于它是一条**真链路**（合成数据 → 四个算子 → 三个交付物），
所以测试不 mock 算子：直接跑，断言"算出来的东西形状与量级正确"，
以及契约层面的规则（参数范围、fixed 参数、组成可见性）确实生效。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from geonexus.geocard import load_geocard
from geonexus.geomcp import build_request
from geonexus.geomcp.server import GeoMCPServer
from geonexus.recipe import (
    STATUS_APPROVED,
    RecipeParamError,
    RecipeRegistry,
    load_recipe,
    materialize,
)

CASE_DIR = Path(__file__).resolve().parent.parent / "examples" / "flightway_wetland"
CARD = CASE_DIR / "case.card.yaml"
RECIPE = CASE_DIR / "recipe.yaml"


def _load_module(name: str):
    """导入案例目录里的脚本（它们不是包，只是可执行示例）。"""
    if str(CASE_DIR) not in sys.path:
        sys.path.insert(0, str(CASE_DIR))
    path = CASE_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"flightway_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sample_data(tmp_path_factory) -> Path:
    """生成一份样例数据（合成，固定种子）。"""
    make_sample_data = _load_module("make_sample_data")
    out = tmp_path_factory.mktemp("flightway-data")
    make_sample_data.generate(out, region="yellow-sea", seed=20260925)
    return out / "yellow-sea"


@pytest.fixture(scope="module")
def executed_case(tmp_path_factory, sample_data) -> dict:
    """按方案真跑一遍，返回运行清单（与 `run_case.py --json` 同一形状）。"""
    import shutil

    run_case = _load_module("run_case")
    workdir = tmp_path_factory.mktemp("flightway-run")
    shutil.copytree(sample_data.parent, workdir / "data")

    card = load_geocard(CARD)
    recipe = load_recipe(RECIPE)
    params = {
        "region": "yellow-sea", "baseline_year": 2010, "target_year": 2024,
        "ndwi_threshold": 0.0, "pc1_threshold": 0.2, "min_count": 0,
        "report_format": "html",
    }
    plan = materialize(recipe, params, run_id="test-run")
    execution = run_case.run_plan(plan, workdir)
    deliverables = run_case.deliverables_of(plan, execution["results"], workdir)
    return {
        "workdir": workdir, "plan": plan, "execution": execution,
        "deliverables": deliverables, "card": card, "recipe": recipe,
        "run_case": run_case,
    }


# --------------------------------------------------------------------------- #
# 契约
# --------------------------------------------------------------------------- #
def test_card_and_recipe_validate() -> None:
    card = load_geocard(CARD)
    card.validate()
    assert card.type == "workflow"
    assert card.recipe_ref.recipe_id == "recipe://geonexus/eaaf-wetland-priority@1.0.0"
    assert card.recipe_ref.version == "1.0.0"
    assert card.recipe_ref.data_snapshot  # 没有数据快照就无法区分"重跑"与"数据变了"
    assert card.provenance.source.startswith("https://mp.weixin.qq.com/")
    recipe = load_recipe(RECIPE)
    recipe.validate()
    assert recipe.entry() == "s1"
    assert recipe.deliverable_names() == ["water_change_map", "priority_sites", "report"]


def test_card_params_mirror_the_recipe_params() -> None:
    """卡片与方案必须是**同一份**参数定义，不是两套说法。"""
    card = load_geocard(CARD)
    recipe = load_recipe(RECIPE)
    card_params = {p.name: p for p in card.params.params}
    recipe_params = {p.name: p for p in recipe.params}
    assert set(card_params) == set(recipe_params)
    for name, spec in recipe_params.items():
        mirror = card_params[name]
        assert mirror.type == spec.type
        assert mirror.scope == spec.scope
        assert mirror.default == spec.default
        assert mirror.minimum == spec.minimum and mirror.maximum == spec.maximum
        assert list(mirror.enum) == list(spec.enum)


def test_the_one_percent_rule_is_a_definition_not_a_knob() -> None:
    """1% 规则是定义：`scope: fixed`；改它必须被拒。"""
    recipe = load_recipe(RECIPE)
    fixed = recipe.param("one_percent_rule")
    assert fixed is not None and fixed.scope == "fixed" and fixed.default is True
    assert "1%" in (fixed.description or "")
    with pytest.raises(RecipeParamError) as exc:
        # 试着把它关掉 —— 关掉算出来的就不是 PC1
        recipe_one = materialize(recipe, {"one_percent_rule": False}, run_id="r")
        del recipe_one
    assert exc.value.code == "param_fixed"


def test_param_contract_rejects_out_of_range_values() -> None:
    recipe = load_recipe(RECIPE)
    with pytest.raises(RecipeParamError) as exc:
        materialize(recipe, {"ndwi_threshold": 3.0}, run_id="r")
    assert (exc.value.field, exc.value.code) == ("ndwi_threshold", "param_out_of_range")
    with pytest.raises(RecipeParamError):
        materialize(recipe, {"region": "amazon"}, run_id="r")
    # 默认值能把整条链填满，不需要调用方逐个给
    plan = materialize(recipe, {}, run_id="r")
    assert plan.params["pc1_threshold"] == 0.2
    assert plan.params["one_percent_rule"] is True


def test_recipe_is_usable_from_the_catalogue_and_geo_plan() -> None:
    """平台侧看到的入口：目录里有它，且 `geo.plan` 能物化出任务图。"""
    registry = RecipeRegistry()
    registry.register(load_recipe(RECIPE), status=STATUS_APPROVED, provider_url="http://node-a")
    server = GeoMCPServer(name="case-node", recipe_registry=registry)
    response = server._dispatcher.dispatch(build_request(
        "geo.plan",
        {"recipe": "recipe://geonexus/eaaf-wetland-priority", "params": {"region": "yangtze"},
         "run_id": "plan-1"},
        id="p1",
    ))
    result = response["result"]
    steps = [t["step"] for t in result["tasks"]]
    assert sorted(steps) == ["s1", "s2", "s3", "s4", "s5"]
    # 顺序必须是**合法拓扑序**：s3 依赖 s1+s2、s5 依赖 s2+s3+s4
    position = {step: index for index, step in enumerate(steps)}
    assert position["s1"] < position["s3"] and position["s2"] < position["s3"]
    assert position["s2"] < position["s5"] and position["s3"] < position["s5"]
    assert position["s4"] < position["s5"]
    assert result["params"]["region"] == "yangtze"
    assert set(result["deliverables"]) == {"water_change_map", "priority_sites", "report"}
    # 路径里的模板已经求值，执行器拿到的是可直接用的相对路径
    s1 = next(t for t in result["tasks"] if t["step"] == "s1")
    assert s1["params"]["stack"] == "data/yangtze/stack_2010.tif"


# --------------------------------------------------------------------------- #
# 真跑一遍
# --------------------------------------------------------------------------- #
def test_every_step_succeeds(executed_case) -> None:
    runs = executed_case["execution"]["runs"]
    assert [r["step"] for r in runs] == ["s1", "s2", "s3", "s4", "s5"] or \
        sorted(r["step"] for r in runs) == ["s1", "s2", "s3", "s4", "s5"]
    assert all(r["status"] == "ok" for r in runs), runs


def test_water_change_is_consistent(executed_case) -> None:
    results = executed_case["execution"]["results"]
    s2, s3 = results["s2"], results["s3"]
    assert s2["water_area_km2"] > 0
    assert s3["baseline_water_km2"] > s3["stable_water_km2"] >= 0
    # 净变化 = 增加 − 损失；损失率 = 损失 / 基线
    assert s3["net_change_km2"] == pytest.approx(
        s3["gained_water_km2"] - s3["lost_water_km2"], abs=1e-6)
    assert s3["loss_rate_pct"] == pytest.approx(
        s3["lost_water_km2"] / s3["baseline_water_km2"] * 100.0, abs=0.01)
    # 合成数据刻意让目标年收缩：损失必须大于增加
    assert s3["lost_water_km2"] > s3["gained_water_km2"]


def test_pc1_ranks_and_thresholds(executed_case) -> None:
    s4 = executed_case["execution"]["results"]["s4"]
    assert s4["site_count"] == 8
    scores = [s["pc1"] for s in s4["top_sites"]]
    assert scores == sorted(scores, reverse=True), "top_sites 必须按 PC1 降序"
    assert 0 < s4["top_sites"][0]["pc1"] < 2, "样例数据只有 20 个种群，PC1 量级不该超过个位数"
    assert 0 <= s4["priority_count"] <= s4["site_count"]
    assert s4["skipped_species"] == [], "计数表里的物种都应在种群量表里找得到"


def test_pc1_table_matches_the_scores(executed_case) -> None:
    results = executed_case["execution"]["results"]
    rows = (Path(results["s4"]["priority_table"]).read_text(encoding="utf-8")
            .strip().splitlines())
    header, *body = rows
    assert header.startswith("rank,site_id,site_name")
    assert len(body) == results["s4"]["site_count"]
    first = body[0].split(",")
    assert first[0] == "1" and float(first[4]) == pytest.approx(
        results["s4"]["top_sites"][0]["pc1"], abs=1e-4)


def test_water_change_raster_is_readable(executed_case) -> None:
    rasterio = pytest.importorskip("rasterio")
    path = Path(executed_case["execution"]["results"]["s3"]["change_raster"])
    assert path.exists() and path.stat().st_size > 0
    with rasterio.open(path) as src:
        assert src.count == 1
        assert src.dtypes[0] == "int8"
        # 合成数据：目标年收缩 ⇒ 损失像元多于增加像元
        lost = int((src.read(1) == -1).sum())
        gained = int((src.read(1) == 1).sum())
    assert lost > gained > 0


# --------------------------------------------------------------------------- #
# 交付物
# --------------------------------------------------------------------------- #
def test_deliverables_are_the_declared_conclusions(executed_case) -> None:
    deliverables = {d["name"]: d for d in executed_case["deliverables"]}
    assert set(deliverables) == {"water_change_map", "priority_sites", "report"}
    for name, item in deliverables.items():
        assert item["exists"], f"{name} 没有落盘"
        assert item["size_bytes"] and item["size_bytes"] > 0
        assert (item["checksum"] or "").startswith("sha256:")
    assert deliverables["report"]["role"] == "knowledge"
    assert deliverables["report"]["media_type"] == "text/html"
    assert deliverables["water_change_map"]["role"] == "data"


def test_report_carries_method_source_and_limits(executed_case) -> None:
    report = Path(next(d for d in executed_case["deliverables"] if d["name"] == "report")["path"])
    html = report.read_text(encoding="utf-8")
    for section in ("结论", "方法", "优先地点", "数据来源与快照", "局限"):
        assert section in html, f"报告缺少「{section}」"
    assert "10.1038/s41598-025-31727-2" in html        # 方法来源可追溯
    assert "合成" in html and "不得用于任何科研" in html  # 合成数据必须显式声明
    assert "PC1" in html and "1%" in html
    # 数值要真的进报告，而不是占位符
    s3 = executed_case["execution"]["results"]["s3"]
    assert f"{s3['lost_water_km2']:,.3f}" in html


# --------------------------------------------------------------------------- #
# 阈值语义（论文的区域校准 vs 样例尺度）
# --------------------------------------------------------------------------- #
def test_paper_threshold_yields_no_priority_site_on_sample_data(tmp_path, sample_data) -> None:
    """用论文的区域校准阈值（10）跑样例数据：0 个入选。

    这不是 bug，而是**尺度差异**的诚实体现：10 是在 270+ 个种群的迁飞区尺度上校准的，
    样例只有 20 个物种。测试把这件事钉住，免得有人把默认值改成 10 之后以为链路坏了。
    """
    import shutil

    skills = _load_module("skills")
    workdir = tmp_path / "run"
    workdir.mkdir()
    shutil.copytree(sample_data.parent, workdir / "data")

    def score(threshold: float) -> dict:
        from types import SimpleNamespace

        context = SimpleNamespace(workdir=str(workdir))
        return skills.pc1_priority_score_handler({
            "counts": "data/yellow-sea/waterbird_counts.csv",
            "populations": "data/yellow-sea/species_populations.csv",
            "threshold": threshold, "one_percent": True, "min_count": 0,
        }, context)

    sample_calibrated = score(0.2)
    paper_calibrated = score(10.0)
    assert sample_calibrated["priority_count"] > 0
    assert paper_calibrated["priority_count"] == 0
    assert paper_calibrated["top_sites"][0]["pc1"] == sample_calibrated["top_sites"][0]["pc1"]


# --------------------------------------------------------------------------- #
# 可见性分层
# --------------------------------------------------------------------------- #
def test_visibility_inherits_from_the_restricted_component(executed_case) -> None:
    card = executed_case["card"]
    run_case = executed_case["run_case"]
    author = run_case.visibility_report(card, restricted_hidden=False)
    viewer = run_case.visibility_report(card, restricted_hidden=True)

    # 案例自己的档位是 public，但组成里有一份受限点位 ⇒ 组合后取最严值
    assert card.compliance is not None and card.compliance.sensitivity == "public"
    assert author["visibility"] == "restricted"
    assert author["visible"] is True
    assert author["levels"]["geocard.eaaf.priority-sites-restricted"] == "restricted"

    # 看不见那份点位的人，看不见这个案例（不存在与无权同等对待）
    assert viewer["visible"] is False
    assert viewer["missing"] == ["geocard.eaaf.priority-sites-restricted"]
    assert viewer["visibility"] == "secret"


def test_the_restricted_component_is_required() -> None:
    """受限项必须是必需项：可选依赖不参与判定，那这条规则就白写了。"""
    card = load_geocard(CARD)
    restricted = [c for c in card.composition.components
                  if c.id == "geocard.eaaf.priority-sites-restricted"]
    assert len(restricted) == 1
    assert restricted[0].required is True


# --------------------------------------------------------------------------- #
# 合成数据必须自带标记
# --------------------------------------------------------------------------- #
def test_sample_data_is_labelled_synthetic(sample_data) -> None:
    rasterio = pytest.importorskip("rasterio")
    manifest = json.loads((sample_data / "sample-data.json").read_text(encoding="utf-8"))
    assert manifest["synthetic"] is True
    assert "不得用于任何科研结论" in manifest["caveat"]
    with rasterio.open(sample_data / "stack_2010.tif") as src:
        assert src.tags().get("GEONEXUS_SYNTHETIC") == "TRUE"
        assert src.count == 2
    counts = (sample_data / "waterbird_counts.csv").read_text(encoding="utf-8")
    assert counts.splitlines()[0].startswith("site_id")     # 表头
    assert "TRUE" in counts.splitlines()[1]                  # 每行都标 synthetic
    populations = (sample_data / "species_populations.csv").read_text(encoding="utf-8")
    assert populations.splitlines()[0].startswith("# GEONEXUS_SYNTHETIC=TRUE")
