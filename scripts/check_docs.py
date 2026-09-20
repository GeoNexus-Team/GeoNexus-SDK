#!/usr/bin/env python3
"""文档 ↔ 实现一致性检查（路线图 Phase 5 的验收工具）。

"文档里列的公开名必须真实存在"——这句话得有人真的去查，否则半年后文档里会留着
一堆已经改名的类。本脚本做三件事：

1. **公开名存在性**：``API_STABILITY.md`` 的 Stable public API 表里，每个模块格
   声明的每个名字都必须能被 ``getattr`` 到。
2. **全文点号名**：所有 ``docs/*.md`` 与 ``README.md`` 里出现的 ``geonexus.*``
   点号名都要能解析（先找最长可导入前缀，再逐级 ``getattr``）。
3. **协议方法名**：`` `geo.x` `` 必须是 ``geonexus.geomcp.protocol.METHODS``
   里注册过的方法。
4. **相对链接**：``[text](path)`` 的相对路径必须真实存在。

只读、无副作用、退出码即结论（0 = 一致）。CI 直接跑它。
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent
DOCS = CORE / "docs"

#: 文档里合法但不指向 ``geonexus.*`` 的模块名（示例/占位）
SKIP_PREFIXES = ("geonexus.*", "geonexus.<")

BACKTICK = re.compile(r"`([^`]+)`")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DOTTED = re.compile(r"\bgeonexus(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
GEO_METHOD = re.compile(r"`(geo\.[A-Za-z_][A-Za-z0-9_]*)`")
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
#: 反引号里的仓库内路径引用，例如 `docs/GEOTASK.md`、`scripts/check_docs.py`。
#: 只认**带已知扩展名**的：否则 `` `queued/running/done` `` 这类状态列表、
#: `` `application/json` `` 这类 MIME 都会被误判成路径。
PATH_REF = re.compile(r"`([A-Za-z0-9_./-]+\.(?:md|py|json|ya?ml|toml|sh|mjs|txt))`")
TABLE_MODULE = re.compile(r"^\|\s*`(geonexus\.[A-Za-z_.]+)`\s*\|(.+)\|\s*$")


class Problems:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, where: str, what: str) -> None:
        self.items.append(f"{where}: {what}")

    def report(self) -> int:
        if not self.items:
            return 0
        print(f"\n❌ 文档与实现不一致（{len(self.items)} 处）：\n")
        for item in self.items:
            print(f"  - {item}")
        return 1


def resolve(dotted: str) -> str | None:
    """解析 ``geonexus.a.b.C``。返回 ``None`` 表示解析成功，否则返回原因。"""
    parts = dotted.split(".")
    if parts[0] != "geonexus":
        return None
    for cut in range(len(parts), 0, -1):
        module_name = ".".join(parts[:cut])
        try:
            obj: object = importlib.import_module(module_name)
        except ImportError:
            continue
        try:
            for attr in parts[cut:]:
                obj = getattr(obj, attr)
        except AttributeError as exc:
            return f"{dotted} 不存在（{exc}）"
        return None
    return f"{dotted} 无法导入任何前缀"


def check_api_table(problems: Problems) -> int:
    """检查 API_STABILITY.md 的公开 API 表。"""
    path = DOCS / "API_STABILITY.md"
    if not path.exists():
        problems.add("API_STABILITY.md", "文件不存在")
        return 0
    checked = 0
    in_table = False
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("## "):
            in_table = "Stable public API" in line
            continue
        if not in_table:
            continue
        match = TABLE_MODULE.match(line)
        if not match:
            continue
        module, names_cell = match.group(1), match.group(2)
        if resolve(module) is not None:
            problems.add(f"API_STABILITY.md:{lineno}", f"模块 {module} 不存在")
            continue
        for token in BACKTICK.findall(names_cell):
            token = token.strip()
            # 只查"看起来就是一个名字"的；`register(status=)`、`/cards/{id}` 这类跳过
            if not IDENT.match(token):
                continue
            if resolve(f"{module}.{token}") is not None:
                problems.add(f"API_STABILITY.md:{lineno}", f"{module} 没有公开名 {token}")
            checked += 1
    return checked


def _docs() -> list[Path]:
    files = sorted(DOCS.glob("*.md"))
    readme = CORE / "README.md"
    if readme.exists():
        files.append(readme)
    return files


def check_dotted_names(problems: Problems) -> int:
    checked = 0
    for path in _docs():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "```" in line:
                continue
            for token in DOTTED.findall(line):
                if token.startswith(SKIP_PREFIXES):
                    continue
                # 去掉句尾标点/括号带来的碎片
                token = token.rstrip(".,;:)")
                if len(token.split(".")) < 2:
                    continue
                reason = resolve(token)
                if reason:
                    problems.add(f"{path.name}:{lineno}", reason)
                checked += 1
    return checked


def check_geo_methods(problems: Problems) -> int:
    from geonexus.geomcp.protocol import METHODS

    checked = 0
    for path in _docs():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for token in GEO_METHOD.findall(line):
                checked += 1
                if token not in METHODS:
                    problems.add(
                        f"{path.name}:{lineno}", f"协议方法 {token} 未注册（可用: {list(METHODS)}）"
                    )
    return checked


def check_links(problems: Problems) -> int:
    checked = 0
    for path in _docs():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for target in LINK.findall(line):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                rel = target.split("#", 1)[0]
                if not rel:
                    continue
                checked += 1
                if not (path.parent / rel).exists():
                    problems.add(f"{path.name}:{lineno}", f"链接指向不存在的 {rel}")
    return checked


_REPO_FILES: list[str] | None = None


def _repo_files() -> list[str]:
    global _REPO_FILES
    if _REPO_FILES is None:
        skip = {"__pycache__", "node_modules", ".git", ".venv-py39", "target", "dist"}
        _REPO_FILES = [
            str(p)
            for p in CORE.rglob("*")
            if p.is_file() and not any(part in skip for part in p.parts)
        ]
    return _REPO_FILES


def _resolve_ref(ref: str, doc_dir: Path) -> bool:
    """解析一个仓库内路径引用。

    文档里的相对基准不统一（有时相对仓库根、有时相对 ``core/``、有时省略
    ``src/geonexus/`` 前缀），所以按这几个基准依次尝试，最后退化成后缀匹配。
    """
    for root in (CORE, CORE.parent, doc_dir):
        if (root / ref).exists():
            return True
    tail = "/" + ref
    return any(path.endswith(tail) for path in _repo_files())


def check_path_refs(problems: Problems) -> int:
    """反引号里的仓库内路径（``docs/X.md``、``scripts/y.py``）必须存在。

    文档互相引用最常用的写法就是 ``see `GEOTASK.md` ``，它比 markdown 链接更常见，
    所以不能只查 ``[text](path)``。
    """
    checked = 0
    for path in _docs():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for ref in PATH_REF.findall(line):
                if "/" not in ref and not ref.endswith(".md"):
                    continue  # 裸的 .py 名字太容易误判，只认带目录的
                checked += 1
                if _resolve_ref(ref, path.parent):
                    continue
                problems.add(f"{path.name}:{lineno}", f"引用了不存在的路径 {ref}")
    return checked


def main() -> int:
    problems = Problems()
    names = check_api_table(problems)
    dotted = check_dotted_names(problems)
    methods = check_geo_methods(problems)
    links = check_links(problems)
    paths = check_path_refs(problems)
    code = problems.report()
    if code == 0:
        print(
            f"✅ 文档一致性检查通过：公开名 {names} 个、点号名 {dotted} 处、"
            f"协议方法 {methods} 处、相对链接 {links} 条、路径引用 {paths} 处"
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
