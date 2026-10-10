#!/usr/bin/env bash
# 在**临时副本**里构建示例插件 wheel。
#
# 为什么不能就地构建：setuptools 会把 build/ 与 *.egg-info 写进源码目录；一旦
# 该目录进了 sys.path（例如测试里 sys.path.insert），importlib.metadata 就会把
# 这些残留当成"已安装的 entry point"——整个 pytest 会话里每个 GeoNode 都会被自动
# 加载这个插件，别处的计数断言集体错位。构建产物必须落在源码树之外。
#
# 用法: scripts/build_plugin_wheel.sh examples/plugin_geokg [输出目录]
set -euo pipefail

SRC="${1:?用法: build_plugin_wheel.sh examples/plugin_xxx [输出目录]}"
OUT="${2:-/tmp/gnx-plugin-wheels}"
# 解释器必须能读懂 pyproject（setuptools>=61）：优先项目 venv，其次环境变量
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for cand in ../.venv-py39/bin/python ./.venv/bin/python ./.venv-py39/bin/python; do
    if [ -x "$cand" ]; then PY="$cand"; break; fi
  done
fi
PY="${PY:-python3}"
NAME="$(basename "$SRC")"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cp -R "$SRC" "$TMP/$NAME"
if [ ! -f "$TMP/$NAME/pyproject.toml" ]; then
  echo "✗ 临时副本里没有 pyproject.toml（复制出错？）" >&2
  exit 1
fi
mkdir -p "$OUT"
"$PY" -m pip wheel --no-deps --no-build-isolation --ignore-requires-python \
  -w "$OUT" "$TMP/$NAME" >/dev/null
WHEEL="$(ls -1t "$OUT"/*.whl | head -1)"
if echo "$WHEEL" | grep -q 'UNKNOWN-0.0.0'; then
  echo "✗ 构建出的 wheel 没有元数据（解释器 $PY 不支持 pyproject？）" >&2
  exit 1
fi
echo "$WHEEL"

# 自检：源码树里不许留下构建残留
if find "$SRC" -maxdepth 2 \( -name '*.egg-info' -o -name build \) | grep -q .; then
  echo "✗ 源码树里出现了构建残留，请检查 $SRC" >&2
  exit 1
fi
