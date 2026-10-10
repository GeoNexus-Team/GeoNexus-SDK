"""示例插件：证明"第三方能力 = 一个 pip 包"这条路是通的。

装法：``pip install -e .``（或市场安装）；节点启动时经 entry-point 组
``geonexus.plugins`` 自动发现并加载，``/health`` 里能看到它的状态。
"""

from geonexus.geocard import GeoCardBuilder
from geonexus.geonode import Skill


def _ndvi_stats(params, context):
    red = [float(v) for v in params.get("red", [])]
    nir = [float(v) for v in params.get("nir", [])]
    if not red or len(red) != len(nir):
        raise ValueError("red / nir 必须等长且非空")
    # zip 安全：上一行已保证等长；strict= 是 py3.10+ 参数，本插件支持 3.9
    ndvi = [(n - r) / (n + r) for n, r in zip(nir, red) if (n + r) != 0]  # noqa: B905
    if not ndvi:
        raise ValueError("所有像元的 red+nir 都为 0，无法计算 NDVI")
    return {"count": len(ndvi), "mean": round(sum(ndvi) / len(ndvi), 4),
            "min": round(min(ndvi), 4), "max": round(max(ndvi), 4)}


def _card():
    return (
        GeoCardBuilder(
            id="geocard.demo.ndvi-sample", type="data", name="NDVI 演示样例",
            description="示例插件自带的合成样例卡片（演示用，非真实数据）。",
        )
        .spatial(bbox=[120.0, 30.0, 121.0, 31.0], crs="EPSG:4326")
        .build()
    )


class plugin:  # noqa: N801 - entry-point 指向的小写对象，保持导入路径稳定
    name = "demo-ndvi"
    version = "0.1.0"
    description = "NDVI 统计技能 + 演示数据卡（插件加载器示例）"

    @staticmethod
    def cards():
        return [_card()]

    @staticmethod
    def skills():
        return [Skill(
            name="demo-ndvi-stats",
            description="NDVI mean/min/max from red/nir arrays (plugin demo)",
            input_schema={"required": ["red", "nir"]},
            handler=_ndvi_stats,
        )]
