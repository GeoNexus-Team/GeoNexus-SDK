"""Tests for the `geonexus` CLI commands."""

from __future__ import annotations

import json

import pytest

from geonexus import __version__
from geonexus.cli import main


def test_cli_version(capsys) -> None:
    assert main(["version"]) == 0
    out = capsys.readouterr().out
    assert out.strip() == f"geonexus {__version__}"


def test_cli_card_validate_ok(capsys) -> None:
    path = "examples/amazon_ndvi/geocard.yaml"
    assert main(["card", "validate", path]) == 0
    assert "OK:" in capsys.readouterr().out


def test_cli_card_validate_invalid(tmp_path, capsys) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("geocard_version: '0.1'\nid: x\ntype: data\nname: X\n", encoding="utf-8")
    assert main(["card", "validate", str(bad)]) == 1
    assert "INVALID" in capsys.readouterr().out


def test_cli_card_inspect(tmp_path, capsys) -> None:
    card = tmp_path / "card.json"
    card.write_text(
        json.dumps(
            {
                "geocard_version": "0.1",
                "id": "inspect-me",
                "type": "data",
                "name": "Inspect",
                "description": "d",
            }
        ),
        encoding="utf-8",
    )
    assert main(["card", "inspect", str(card)]) == 0
    out = capsys.readouterr().out
    assert '"id": "inspect-me"' in out


def test_cli_init(tmp_path, capsys) -> None:
    project = tmp_path / "my-project"
    assert main(["init", str(project)]) == 0
    assert (project / "geocard.yaml").exists()
    assert (project / "README.md").exists()
    assert "Initialized" in capsys.readouterr().out


def test_cli_unknown_command(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["not-a-command"])
    assert exc_info.value.code == 2


def test_cli_web_start_help(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["web", "start", "--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "--registry" in out
    assert "--node-api-key" in out
    assert "--cors-origin" in out
    assert "--user" in out


def test_split_users_parses_and_rejects() -> None:
    """``--user`` 是本地唯一能登上 Web 层的入口，解析必须严格且容忍密码里的 =。"""
    from geonexus.cli import _split_users

    assert _split_users(None) == []
    assert _split_users(["admin=admin"]) == [("admin", "admin")]
    assert _split_users(["d=p@ss=word"]) == [("d", "p@ss=word")]
    assert _split_users(["a=1", "b=2"]) == [("a", "1"), ("b", "2")]

    for bad in (["admin"], ["=x"], ["x="]):
        with pytest.raises(SystemExit):
            _split_users(bad)


def test_cli_card_import_ogc_records_help(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["card", "import-ogc-records", "--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "--record" in out
    assert "--collection" in out


def test_cli_card_import_ogc_tiles_help(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["card", "import-ogc-tiles", "--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "--tileset" in out
    assert "--style" in out


def test_cli_card_import_ogc_legacy_help(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["card", "import-ogc-legacy", "--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "--service" in out
    assert "wms" in out and "wmts" in out


# --------------------------------------------------------------------------- #
# geonexus task —— GeoTask 运维命令
# --------------------------------------------------------------------------- #
def _demo_store(path):
    """造一个"崩溃前"的状态文件：一个完成、一个 running 且租约过期。"""
    import time

    from geonexus.geotask import (
        GeoTask,
        JsonTaskStore,
        RetryPolicy,
        TaskKind,
        TaskResult,
        TaskState,
    )

    store = JsonTaskStore(path)
    store.put(
        GeoTask(task_id="t-ok", kind=TaskKind.SKILL, spec={"handler": "x"}, description="完成的")
    )
    for state in (TaskState.QUEUED, TaskState.SCHEDULED, TaskState.RUNNING):
        store.transition("t-ok", state)
    store.report_progress("t-ok", 0.5, "一半")
    store.transition("t-ok", TaskState.SUCCEEDED, result=TaskResult(outputs={"result": 1}))

    store.put(
        GeoTask(
            task_id="t-dead",
            kind=TaskKind.SKILL,
            spec={"handler": "x"},
            description="崩溃留下的",
            retry=RetryPolicy(max_attempts=1),
        )
    )
    for state in (TaskState.QUEUED, TaskState.SCHEDULED, TaskState.RUNNING):
        store.transition("t-dead", state)
    store.set_lease("t-dead", expires_at=time.time() - 60, executor="dead")
    return path


class TestTaskCli:
    def test_subcommands_are_exactly_inspection_and_reconcile(self) -> None:
        """刻意**没有** submit/cancel —— 并钉住这个决定。

        取消是协作式的：另一个进程改状态文件，没有执行者会去读那个标志。
        加一个 `task cancel` 会让用户以为停住了，而任务照跑。所以要有人加它时，
        这条测试先失败。
        """
        import argparse

        from geonexus.cli import build_parser

        parser = build_parser()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction) and "task" in action.choices:
                assert set(action.choices["task"]._subparsers._group_actions[0].choices) == {
                    "list",
                    "show",
                    "events",
                    "reconcile",
                }
                return
        raise AssertionError("找不到 task 子命令")

    def test_list_shows_states(self, tmp_path, capsys) -> None:
        from geonexus.cli import main

        path = _demo_store(tmp_path / "gt.json")
        assert main(["task", "list", "--state", str(path)]) == 0
        out = capsys.readouterr().out
        assert "t-ok" in out and "t-dead" in out
        assert "succeeded 1" in out and "running 1" in out

    def test_list_json(self, tmp_path, capsys) -> None:
        import json as _json

        from geonexus.cli import main

        path = _demo_store(tmp_path / "gt.json")
        assert main(["task", "list", "--state", str(path), "--json"]) == 0
        payload = _json.loads(capsys.readouterr().out)
        assert {t["task_id"] for t in payload} == {"t-ok", "t-dead"}

    def test_show_prints_error_code_and_retryability(self, tmp_path, capsys) -> None:
        from geonexus.cli import main

        path = _demo_store(tmp_path / "gt.json")
        # 先对账，让 t-dead 带上 executor_lost
        main(["task", "reconcile", "--state", str(path)])
        capsys.readouterr()
        assert main(["task", "show", "t-dead", "--state", str(path)]) == 0
        out = capsys.readouterr().out
        assert "executor_lost" in out
        assert "可重试" in out  # executor_lost 是可重试的

    def test_events_replay_respects_since(self, tmp_path, capsys) -> None:
        import json as _json

        from geonexus.cli import main

        path = _demo_store(tmp_path / "gt.json")
        assert main(["task", "events", "t-ok", "--state", str(path), "--since", "2", "--json"]) == 0
        payload = _json.loads(capsys.readouterr().out)
        assert [e["seq"] for e in payload["events"]] == [3, 4, 5, 6]
        assert payload["latest_seq"] == 6

    def test_reconcile_fixes_dead_runner_and_is_idempotent(self, tmp_path, capsys) -> None:
        from geonexus.cli import main
        from geonexus.geotask import JsonTaskStore

        path = _demo_store(tmp_path / "gt.json")
        assert main(["task", "reconcile", "--state", str(path)]) == 0
        out = capsys.readouterr().out
        assert "判为失败 1" in out
        assert JsonTaskStore(path).get("t-dead").state.value == "failed"

        # 再跑一次：没有可处理的任务（幂等）
        assert main(["task", "reconcile", "--state", str(path), "--json"]) == 0
        payload = __import__("json").loads(capsys.readouterr().out)
        assert payload == {"requeued": 0, "failed": 0, "checked": 0}

    def test_unknown_task_exits_nonzero(self, tmp_path, capsys) -> None:
        from geonexus.cli import main

        path = _demo_store(tmp_path / "gt.json")
        assert main(["task", "show", "nope", "--state", str(path)]) == 1
        assert "未知任务" in capsys.readouterr().err

    def test_missing_store_file_is_not_an_error(self, tmp_path, capsys) -> None:
        """没有状态文件是"还没有任务"，不是失败——退出码保持 0。"""
        from geonexus.cli import main

        assert main(["task", "list", "--state", str(tmp_path / "none.json")]) == 0
        assert "没有任务状态文件" in capsys.readouterr().out

    def test_missing_store_file_fails_for_show(self, tmp_path, capsys) -> None:
        """但"要查一个具体任务却没有库"是失败。"""
        from geonexus.cli import main

        assert main(["task", "show", "x", "--state", str(tmp_path / "none.json")]) == 1
