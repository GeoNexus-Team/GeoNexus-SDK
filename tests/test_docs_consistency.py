"""文档一致性检查器自己的测试（Phase 5 的验收工具必须可证伪）。

`scripts/check_docs.py` 是"文档里列的公开名必须真实存在"这句话的执行者。一个只会
通过的检查器等于没有检查，所以这里既验证它在**正确**的文档上通过，也验证它在
**撒谎**的文档上一定会红——对每一类检查各造一个假。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

CORE = Path(__file__).resolve().parent.parent
SCRIPT = CORE / "scripts" / "check_docs.py"


@pytest.fixture(scope="module")
def checker() -> Any:
    spec = importlib.util.spec_from_file_location("check_docs", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestRealDocs:
    def test_repository_docs_are_consistent(self) -> None:
        result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True,
                                text=True, cwd=CORE)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "文档一致性检查通过" in result.stdout

    def test_it_actually_checked_something(self, checker: Any) -> None:
        """防止"检查了 0 项"式的假绿。"""
        problems = checker.Problems()
        assert checker.check_api_table(problems) > 100
        assert checker.check_dotted_names(problems) > 10
        assert checker.check_geo_methods(problems) > 10
        assert checker.check_path_refs(problems) > 10
        assert problems.items == []


class TestResolve:
    def test_real_name(self, checker: Any) -> None:
        assert checker.resolve("geonexus.model.invoke_model") is None
        # 只认类级名字（实例属性不在此列）：property 与类属性都可以
        assert checker.resolve("geonexus.geotask.Scheduler.stats") is None
        assert checker.resolve("geonexus.model.ModelOutcome.ok") is None

    def test_renamed_name_is_caught(self, checker: Any) -> None:
        assert checker.resolve("geonexus.model.invoke_model_v2") is not None

    def test_missing_module(self, checker: Any) -> None:
        assert checker.resolve("geonexus.nope.Thing") is not None

    def test_non_geonexus_is_ignored(self, checker: Any) -> None:
        assert checker.resolve("os.path.join") is None


class TestFalsifiability:
    """每一类检查都要能抓到对应的谎言。"""

    def _lying_docs(self, checker: Any, tmp_path: Path, name: str, text: str) -> None:
        (tmp_path / name).write_text(text, encoding="utf-8")
        checker.DOCS = tmp_path

    def test_fake_public_name_in_api_table(self, checker: Any, tmp_path: Path) -> None:
        self._lying_docs(checker, tmp_path, "API_STABILITY.md",
                         "# API\n\n## Stable public API\n\n"
                         "| Module | Exported names |\n|---|---|\n"
                         "| `geonexus.model` | `invoke_model`, `invoke_model_v2` |\n")
        problems = checker.Problems()
        checker.check_api_table(problems)
        assert any("invoke_model_v2" in item for item in problems.items)

    def test_fake_module_in_api_table(self, checker: Any, tmp_path: Path) -> None:
        self._lying_docs(checker, tmp_path, "API_STABILITY.md",
                         "# API\n\n## Stable public API\n\n"
                         "| Module | Exported names |\n|---|---|\n"
                         "| `geonexus.nowhere` | `Thing` |\n")
        problems = checker.Problems()
        checker.check_api_table(problems)
        assert any("geonexus.nowhere" in item for item in problems.items)

    def test_fake_dotted_name_in_prose(self, checker: Any, tmp_path: Path) -> None:
        self._lying_docs(checker, tmp_path, "FAKE.md",
                         "用 `geonexus.geotask.TaskRunnerr` 跑任务。\n")
        problems = checker.Problems()
        checker.check_dotted_names(problems)
        assert any("TaskRunnerr" in item for item in problems.items)

    def test_fake_geo_method(self, checker: Any, tmp_path: Path) -> None:
        self._lying_docs(checker, tmp_path, "FAKE.md",
                         "新方法是 `geo.solve_everything`。\n")
        problems = checker.Problems()
        checker.check_geo_methods(problems)
        assert any("geo.solve_everything" in item for item in problems.items)

    def test_fake_path_reference(self, checker: Any, tmp_path: Path) -> None:
        self._lying_docs(checker, tmp_path, "FAKE.md",
                         "见 `docs/GHOST.md` 与 `tests/test_ghost.py`。\n")
        problems = checker.Problems()
        checker.check_path_refs(problems)
        assert len([i for i in problems.items if "GHOST" in i or "ghost" in i]) == 2

    def test_fake_markdown_link(self, checker: Any, tmp_path: Path) -> None:
        self._lying_docs(checker, tmp_path, "FAKE.md",
                         "见 [契约](GEOTASK_OLD.md)。\n")
        problems = checker.Problems()
        checker.check_links(problems)
        assert any("GEOTASK_OLD.md" in item for item in problems.items)

    def test_status_code_is_nonzero_on_lies(self, checker: Any, tmp_path: Path) -> None:
        checker.DOCS = tmp_path
        (tmp_path / "API_STABILITY.md").write_text(
            "# API\n\n## Stable public API\n\n"
            "| Module | Exported names |\n|---|---|\n"
            "| `geonexus.model` | `totally_made_up` |\n", encoding="utf-8")
        assert checker.Problems().report() == 0        # 空问题集 → 0
        problems = checker.Problems()
        checker.check_api_table(problems)
        assert problems.report() == 1, "有问题必须返回非零"
