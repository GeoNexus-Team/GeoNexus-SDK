"""把案例的栅格与表格转成 GeoJSON —— 地图可视化要的是几何，不是 GeoTIFF。

平台侧（Cesium / MapLibre）只认 GeoJSON/矢量；GeoTIFF 要另配瓦片服务才上得了图。
教学场景不需要那套基础设施，所以这里把样例产物**矢量化**成三份文件：

    output/geometry/water_change.geojson   变化面：loss / gain / stable（分级配色）
    output/geometry/water_baseline.geojson 基线水面范围（讲"两期对比"用）
    output/geometry/sites.geojson          地点：PC1 分值、达标种群数、是否优先

坐标一律 EPSG:4326（WGS84），属性里带 `synthetic: true` —— 界面上的"合成为示意"
标注靠它，不能靠人记得。

    python export_geojson.py            # 先跑 run_case.py 再用它
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

#: 变化栅格的类别 → 属性（与 skills.water_change_handler 的约定一致）
CHANGE_CLASSES: dict[int, str] = {-1: "loss", 0: "stable", 1: "gain"}

#: PC1 分级：与前端图例共用一套（前端只负责画，不自己定义分数区间）
PC1_BANDS: list[tuple[float, str]] = [
    (0.5, "high"),
    (0.25, "medium"),
    (0.0, "low"),
]


def pc1_band(value: float) -> str:
    for threshold, label in PC1_BANDS:
        if value >= threshold:
            return label
    return "low"


def _feature(geometry: dict[str, Any], properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def vectorize_raster(path: Path, *, keep: list[int] | None = None) -> dict[str, Any]:
    """栅格 → MultiPolygon 分级要素（用 rasterio 的 shapes，不自造追踪算法）。"""
    rasterio = __import__("rasterio")
    from rasterio import features

    with rasterio.open(path) as src:
        # rasterio 的 shapes 不接受 int8（变化栅格就是 int8）：升到 int16 再矢量化
        data = src.read(1).astype("int16")
        transform = src.transform
        crs = str(src.crs) if src.crs else None
        count = src.count
    if count != 1:
        raise SystemExit(f"{path} 应为单波段，实际 {count} 波段")

    wanted = set(keep) if keep is not None else set(CHANGE_CLASSES)
    buckets: dict[int, list[list[list[list[float]]]]] = {}
    for geometry, value in features.shapes(data, transform=transform):
        value = int(value)
        if value not in wanted or value not in CHANGE_CLASSES:
            continue
        buckets.setdefault(value, []).append(geometry["coordinates"])

    out_features = []
    for value, polygons in sorted(buckets.items()):
        pixels = int((data == value).sum())
        out_features.append(_feature(
            {"type": "MultiPolygon", "coordinates": polygons},
            {
                "class": CHANGE_CLASSES[value],
                "class_code": value,
                "pixels": pixels,
                "synthetic": True,
            },
        ))
    return {
        "type": "FeatureCollection",
        "crs": crs,
        "features": out_features,
    }


def _read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as handle:
        rows = [r for r in csv.reader(handle) if r and not r[0].startswith("#")]
    if not rows:
        return []
    header = rows[0]
    return [dict(zip(header, row)) for row in rows[1:]]  # noqa: B905 (py3.9)


def sites_features(sites_csv: Path, priority_csv: Path) -> dict[str, Any]:
    """地点点要素：坐标来自 sites.csv，分值来自 priority_sites.csv（按 site_id 合并）。"""
    sites = {row["site_id"]: row for row in _read_csv(sites_csv)}
    scores = {row["site_id"]: row for row in _read_csv(priority_csv)}
    features = []
    for site_id, score in scores.items():
        site = sites.get(site_id)
        if not site:
            continue
        pc1 = float(score.get("pc1") or 0)
        features.append(_feature(
            {"type": "Point", "coordinates": [float(site["lon"]), float(site["lat"])]},
            {
                "site_id": site_id,
                "name": site.get("site_name") or site_id,
                "kind": site.get("kind") or None,
                "protected_status": site.get("protected_status") or None,
                "pc1": pc1,
                "pc1_band": pc1_band(pc1),
                "priority": str(score.get("priority", "")).upper() == "TRUE",
                "species_meeting_1pct": int(float(score.get("species_meeting_1pct") or 0)),
                "threatened_species_meeting_1pct":
                    int(float(score.get("threatened_species_meeting_1pct") or 0)),
                "top_species": score.get("top_species") or None,
                "synthetic": True,
            },
        ))
    return {"type": "FeatureCollection", "features": features}


def export(workdir: Path, *, out_dir: Path | None = None) -> dict[str, Any]:
    out = out_dir or (workdir / "geometry")
    out.mkdir(parents=True, exist_ok=True)

    change_raster = workdir / "water_change.tif"
    baseline_mask = workdir / "water_mask_2010.tif"
    if not change_raster.exists():
        raise SystemExit(f"找不到 {change_raster}；先跑 `python run_case.py`")

    files: dict[str, Any] = {}
    change = vectorize_raster(change_raster, keep=[-1, 1])  # stable 面积太大，默认不画
    (out / "water_change.geojson").write_text(
        json.dumps(change, ensure_ascii=False), encoding="utf-8")
    files["water_change"] = {
        "path": str(out / "water_change.geojson"),
        "features": len(change["features"]),
        "classes": [f["properties"]["class"] for f in change["features"]],
    }

    if baseline_mask.exists():
        baseline = vectorize_raster(baseline_mask, keep=[1])
        for feature in baseline["features"]:
            feature["properties"]["class"] = "water"
        (out / "water_baseline.geojson").write_text(
            json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
        files["water_baseline"] = {"path": str(out / "water_baseline.geojson"),
                                   "features": len(baseline["features"])}

    sites_csv = workdir / "data" / "yellow-sea" / "sites.csv"
    priority_csv = workdir / "priority_sites.csv"
    if sites_csv.exists() and priority_csv.exists():
        sites = sites_features(sites_csv, priority_csv)
        (out / "sites.geojson").write_text(json.dumps(sites, ensure_ascii=False), encoding="utf-8")
        files["sites"] = {
            "path": str(out / "sites.geojson"),
            "features": len(sites["features"]),
            "legend": {"pc1_bands": [{"min": t, "band": b} for t, b in PC1_BANDS]},
        }

    manifest = {
        "case": "geocard.case.eaaf-wetland-priority",
        "synthetic": True,
        "crs": "EPSG:4326",
        "note": "全部几何来自合成/示意数据，仅用于平台可视化与教学演示",
        "files": files,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把案例产物转成地图可用的 GeoJSON")
    parser.add_argument("--workdir", default=str(HERE / "output"))
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    manifest = export(Path(args.workdir), out_dir=Path(args.out) if args.out else None)
    print("GeoJSON 已生成：")
    for key, info in manifest["files"].items():
        extra = f"（{info['features']} 个要素）" if "features" in info else ""
        print(f"  {key:16s} {info['path']} {extra}")
    print("  注意：坐标为 EPSG:4326，属性里带 synthetic=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
