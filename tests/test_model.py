"""模型调用契约（Phase 3）—— ``geonexus.model`` + ``geo.invoke``。

这一层的验收标准是**可运行的证据**，不是"接口存在"：

1. 算力门禁在调用**之前**拒绝，且拒绝带理由（用真实 HTTP 服务统计"到底有没有
   发出去"来证明"没进执行"，而不是靠 mock 断言）。
2. 推理走真实 HTTP：``http.server.ThreadingHTTPServer`` 起一个进程内服务，
   ``InferenceProvider`` 用真的 httpx 请求打上去。
3. 批作业走完 提交 → 轮询 → 回收 三步。
4. 模型调用能被 GeoTask 调度（同一套状态机/错误码/重试判定）。
5. ``geo.invoke`` 是**加法式**扩展：老客户端与老节点的行为一个字都没变。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from geonexus.geocard import GeoCardBuilder
from geonexus.geocard.model import IOField
from geonexus.geomcp import GeoMCPServer
from geonexus.geomcp.models import InvokeParams
from geonexus.geomcp.protocol import METHODS, WRITE_METHODS, GeoMCPDispatcher
from geonexus.geotask.backend import ExecutionContext
from geonexus.geotask.model import GeoTask, TaskKind, TaskState
from geonexus.model import (
    BatchProvider,
    InferenceProvider,
    ModelBackend,
    ModelError,
    ModelInvocation,
    choose_provider,
    invoke_model,
    validate_inputs,
    validate_outputs,
)
from geonexus.resource import ComputeCapability


# --------------------------------------------------------------------------- #
# 进程内"真实"HTTP 服务
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def _handle(self) -> None:
        service = self.server.service  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode() or "{}")
        except ValueError:
            body = {}
        with service.lock:
            service.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "body": body,
                    "authorization": self.headers.get("Authorization"),
                    "api_key": self.headers.get("X-API-Key"),
                }
            )
            seq = len(service.requests)
        status, payload = service.responder(self.command, self.path, body, seq)
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_POST = _handle
    do_GET = _handle

    def log_message(self, *args: Any) -> None:  # noqa: D102 - 静音
        return


class _Service:
    """一个真的在监听端口的 HTTP 服务；``responder`` 决定它怎么答。"""

    def __init__(self, responder: Any) -> None:
        self.responder = responder
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.service = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/infer"

    @property
    def hits(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.requests)

    def __enter__(self) -> _Service:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()


def _dead_url() -> str:
    """一个**确定**没人监听的地址：拿一个临时端口再立刻放掉。

    不要写死 ``127.0.0.1:9``——某些环境上那个端口有人应答，测试会变成
    "测我自己机器的服务列表"。
    """
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}/infer"


def _failing_client(exc: Exception) -> Any:
    """一个必定失败的 httpx 客户端——注入传输层，而不是替换被测代码。"""
    import httpx

    def handler(request: Any) -> Any:
        raise exc

    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)


def _ok_responder(outputs: dict[str, Any]) -> Any:
    def responder(method: str, path: str, body: dict[str, Any], seq: int) -> Any:
        return 200, {"outputs": outputs}

    return responder


# --------------------------------------------------------------------------- #
# 卡片与算力
# --------------------------------------------------------------------------- #
def _model_card(
    model_id: str = "clip-vit-b32",
    *,
    protocol: str = "http",
    endpoint: str = "http://127.0.0.1:9/infer",
    runtime: dict[str, Any] | None = None,
    inputs: list[dict[str, Any]] | None = None,
    outputs: list[dict[str, Any]] | None = None,
) -> Any:
    builder = (
        GeoCardBuilder(model_id, "model", f"Model {model_id}", "test model")
        .capability("embedding")
        .interface(type="geomcp-skill", version="1.0")
        .access(protocol=protocol, endpoint=endpoint, format="json")
    )
    if runtime:
        builder.runtime(**runtime)
    card = builder.build()
    card.inputs = [IOField(**f) for f in (inputs or [])]
    card.outputs = [IOField(**f) for f in (outputs or [])]
    return card


def _caps(
    url: str, *, cpu: int = 8, memory_gb: float = 16.0, gpu: str = "none"
) -> list[ComputeCapability]:
    return [ComputeCapability(node_url=url, cpu=cpu, memory_gb=memory_gb, gpu=gpu)]


# --------------------------------------------------------------------------- #
# ① 算力门禁：不匹配就不执行
# --------------------------------------------------------------------------- #
class TestComputeGate:
    def test_no_compute_at_all(self) -> None:
        card = _model_card(runtime={"cpu": 2, "memory": "4Gi", "gpu": "none"})
        outcome = invoke_model(card, {}, capabilities=[])
        assert outcome.status == "failed"
        assert outcome.error_code == "no_compatible_compute"
        assert outcome.reasons, "拒绝必须带理由"

    def test_gpu_mismatch_rejected_before_any_call(self) -> None:
        """最重要的路径：门禁不过时**一个字节都没有发出去**。"""
        with _Service(_ok_responder({"embedding": [1.0]})) as svc:
            card = _model_card(
                endpoint=svc.url,
                runtime={"cpu": 2, "memory": "4Gi", "gpu": "cuda"},
                outputs=[{"name": "embedding", "type": "vector", "required": True}],
            )
            outcome = invoke_model(card, {}, capabilities=_caps(svc.url, gpu="none"))

            assert outcome.error_code == "no_compatible_compute"
            assert any("gpu" in r for r in outcome.reasons), outcome.reasons
            assert svc.hits == [], "门禁在调用前就该拒绝"

    def test_match_succeeds_and_targets_the_gated_node(self) -> None:
        """门禁结论必须**真的**决定调用目标：卡片 endpoint 是死的，算力节点是真的。"""
        with _Service(_ok_responder({"embedding": [0.5]})) as svc:
            card = _model_card(
                endpoint="http://127.0.0.1:9/dead",
                runtime={"cpu": 2, "memory": "4Gi", "gpu": "none"},
                outputs=[{"name": "embedding", "type": "vector", "required": True}],
            )
            outcome = invoke_model(card, {"text": "hi"}, capabilities=_caps(svc.url))

            assert outcome.ok, outcome.error
            assert outcome.outputs == {"embedding": [0.5]}
            # 结果里能回答"它到底跑在哪"——门禁结论必须是可观测的
            assert outcome.compute_node == svc.url
            assert outcome.to_dict()["compute_node"] == svc.url
            assert len(svc.hits) == 1
            assert svc.hits[0]["body"]["model"] == "clip-vit-b32"

    def test_no_gate_means_no_compute_node(self) -> None:
        with _Service(_ok_responder({})) as svc:
            outcome = invoke_model(
                _model_card(endpoint=svc.url),
                {},
                capabilities=_caps(svc.url),
                require_compute=False,
            )
            assert outcome.ok
            assert outcome.compute_node is None, "没过门禁就不该报一个节点"

    def test_unreachable_registry_becomes_a_reasoned_rejection(self) -> None:
        card = _model_card(runtime={"cpu": 2, "memory": "4Gi", "gpu": "none"})
        outcome = invoke_model(card, {}, registry_url="http://127.0.0.1:9")
        assert outcome.error_code == "no_compatible_compute"
        assert outcome.reasons


# --------------------------------------------------------------------------- #
# ② 输入/输出契约
# --------------------------------------------------------------------------- #
class TestIOContract:
    def test_missing_required_input_never_calls(self) -> None:
        with _Service(_ok_responder({"embedding": [1.0]})) as svc:
            card = _model_card(
                endpoint=svc.url,
                inputs=[{"name": "text", "type": "string", "required": True}],
                outputs=[{"name": "embedding", "type": "vector", "required": True}],
            )
            outcome = invoke_model(card, {}, capabilities=_caps(svc.url))
            assert outcome.error_code == "input_missing"
            assert any("text" in r for r in outcome.reasons)
            assert svc.hits == []

    def test_missing_required_output_is_reported(self) -> None:
        with _Service(_ok_responder({"something_else": 1})) as svc:
            card = _model_card(
                endpoint=svc.url,
                outputs=[{"name": "embedding", "type": "vector", "required": True}],
            )
            outcome = invoke_model(card, {}, capabilities=_caps(svc.url))
            assert outcome.error_code == "invalid_spec"
            assert any("embedding" in r for r in outcome.reasons)
            # 产物仍然带回来，便于排查"它到底给了什么"
            assert outcome.outputs == {"something_else": 1}

    def test_validate_helpers_ignore_undeclared_extras(self) -> None:
        card = _model_card(
            inputs=[{"name": "text", "type": "string", "required": True}],
            outputs=[{"name": "embedding", "type": "vector"}],
        )
        assert validate_inputs(card, {"text": "x", "extra": 1}) == []
        assert validate_outputs(card, {"whatever": 1}) == []

    def test_card_without_io_declarations_is_not_gated(self) -> None:
        card = _model_card()
        assert validate_inputs(card, {}) == []
        assert validate_outputs(card, {}) == []


# --------------------------------------------------------------------------- #
# ③ 真实 HTTP 推理
# --------------------------------------------------------------------------- #
class TestInferenceProvider:
    def test_end_to_end_over_real_http(self) -> None:
        with _Service(_ok_responder({"embedding": [1.0, 2.0]})) as svc:
            provider = InferenceProvider(svc.url, timeout=10.0)
            outcome = provider.invoke(
                ModelInvocation(model_id="m1", inputs={"text": "hello"}, params={"top_k": 3})
            )
            provider.close()

            assert outcome.ok
            assert outcome.provider == "inference"
            assert outcome.outputs == {"embedding": [1.0, 2.0]}
            sent = svc.hits[0]["body"]
            assert sent["model"] == "m1"
            assert sent["inputs"] == {"text": "hello"}
            assert sent["params"] == {"top_k": 3}
            assert sent["request_id"].startswith("invoke-")

    def test_flat_response_body_is_accepted(self) -> None:
        """服务直接返回产物（没有 ``outputs`` 包装）也要能用。"""
        with _Service(lambda *a: (200, {"ndvi": 0.71})) as svc:
            provider = InferenceProvider(svc.url, timeout=10.0)
            outcome = provider.invoke(ModelInvocation(model_id="m1"))
            provider.close()
            assert outcome.ok and outcome.outputs == {"ndvi": 0.71}

    def test_api_key_is_sent(self) -> None:
        with _Service(_ok_responder({})) as svc:
            provider = InferenceProvider(svc.url, api_key="s3cret", timeout=10.0)
            provider.invoke(ModelInvocation(model_id="m1"))
            provider.close()
            assert svc.hits[0]["authorization"] == "Bearer s3cret"

    @pytest.mark.parametrize(
        ("status", "code", "retryable"),
        [
            (500, "resource_unavailable", True),
            (503, "resource_unavailable", True),
            (400, "invalid_spec", False),
            (404, "invalid_spec", False),
        ],
    )
    def test_http_error_mapping(self, status: int, code: str, retryable: bool) -> None:
        with _Service(lambda *a: (status, {"error": "nope"})) as svc:
            provider = InferenceProvider(svc.url, timeout=10.0)
            outcome = provider.invoke(ModelInvocation(model_id="m1"))
            provider.close()
            assert outcome.error_code == code
            assert outcome.retryable is retryable, "5xx 可重试、4xx 换机器也一样"

    def test_non_json_body(self) -> None:
        with _Service(lambda *a: (200, b"<html>gateway</html>")) as svc:
            provider = InferenceProvider(svc.url, timeout=10.0)
            outcome = provider.invoke(ModelInvocation(model_id="m1"))
            provider.close()
            assert outcome.error_code == "execution_error"
            assert outcome.retryable is False

    def test_connect_error_maps_to_executor_lost(self) -> None:
        """连不上 → ``executor_lost``（可重试）。

        用注入的传输层制造失败：本机网络中间有拦截设备，连"没人监听的端口"
        也未必返回拒绝，靠真实 socket 测这条分支会是环境相关的假红/假绿。
        """
        provider = InferenceProvider(
            "http://127.0.0.1:1/infer",
            timeout=2.0,
            client=_failing_client(httpx.ConnectError("connection refused")),
        )
        outcome = provider.invoke(ModelInvocation(model_id="m1"))
        provider.close()
        assert outcome.error_code == "executor_lost"
        assert outcome.retryable is True

    def test_timeout_maps_to_timeout(self) -> None:
        provider = InferenceProvider(
            "http://127.0.0.1:1/infer",
            timeout=2.0,
            client=_failing_client(httpx.ReadTimeout("too slow")),
        )
        outcome = provider.invoke(ModelInvocation(model_id="m1"))
        provider.close()
        assert outcome.error_code == "timeout"
        assert outcome.retryable is True

    def test_real_dead_endpoint_is_never_a_success(self) -> None:
        """真实 socket + 真实 httpx：具体错误码由网络层决定，但**绝不能**是成功。"""
        provider = InferenceProvider(_dead_url(), timeout=2.0)
        outcome = provider.invoke(ModelInvocation(model_id="m1"))
        provider.close()
        assert outcome.ok is False
        assert outcome.error_code in {"executor_lost", "resource_unavailable", "timeout"}
        assert outcome.retryable is True, "环境类失败必须是可重试的"

    def test_capabilities_declare_transport(self) -> None:
        provider = InferenceProvider("http://x/infer")
        caps = provider.capabilities()
        provider.close()
        assert caps["transport"] == "http" and caps["modes"] == ["sync"]

    def test_retarget_only_swaps_the_endpoint(self) -> None:
        provider = InferenceProvider("http://old:1/infer", api_key="k", timeout=5.0)
        clone = provider.retarget("http://new:2/infer")
        try:
            assert isinstance(clone, InferenceProvider)
            assert clone.endpoint == "http://new:2/infer"
            assert clone.api_key == "k"
            assert clone._client is provider._client, "复用连接池，不是重新开局"
        finally:
            clone.close()
            provider.close()


# --------------------------------------------------------------------------- #
# ④ 批 / 云作业：提交 → 轮询 → 回收
# --------------------------------------------------------------------------- #
def _batch_responder(script: list[str]) -> Any:
    """``script`` 依次给出每次 GET 轮询的状态。"""
    counter = {"n": 0}

    def responder(method: str, path: str, body: dict[str, Any], seq: int) -> Any:
        if method == "POST":
            return 200, {"job_id": "job-7", "status": "queued"}
        counter["n"] += 1
        status = script[min(counter["n"] - 1, len(script) - 1)]
        if status == "succeeded":
            return 200, {"status": "succeeded", "outputs": {"mask": "s3://out/m.tif"}}
        if status == "failed":
            return 200, {"status": "failed", "error": "CUDA out of memory"}
        return 200, {"status": status}

    return responder


class TestBatchProvider:
    def test_full_submit_poll_collect_path(self) -> None:
        with _Service(_batch_responder(["running", "running", "succeeded"])) as svc:
            provider = BatchProvider(svc.url, poll_interval=0.01, timeout=5.0)
            outcome = provider.invoke(ModelInvocation(model_id="train-x", inputs={"a": 1}))
            provider.close()

            assert outcome.ok
            assert outcome.outputs == {"mask": "s3://out/m.tif"}
            methods = [h["method"] for h in svc.hits]
            assert methods[0] == "POST", "先提交"
            assert set(methods[1:]) == {"GET"}, "再轮询"
            assert len(methods) >= 3, f"轮询到终态为止，实际 {methods}"
            assert svc.hits[0]["body"]["model"] == "train-x"

    def test_submit_is_separable_from_collect(self) -> None:
        """提交与回收可以分开：长作业的调用方要能先持久化 job_id。"""
        with _Service(_batch_responder(["succeeded"])) as svc:
            provider = BatchProvider(svc.url, poll_interval=0.01, timeout=5.0)
            job_id = provider.submit(ModelInvocation(model_id="m1"))
            assert job_id == "job-7"
            assert provider.poll(job_id)["status"] == "succeeded"
            outcome = provider.collect(job_id)
            provider.close()
            assert outcome.ok and outcome.outputs["mask"].endswith("m.tif")

    def test_failed_job_is_reported(self) -> None:
        with _Service(_batch_responder(["failed"])) as svc:
            provider = BatchProvider(svc.url, poll_interval=0.01, timeout=5.0)
            outcome = provider.invoke(ModelInvocation(model_id="m1"))
            provider.close()
            assert outcome.error_code == "execution_error"
            assert "out of memory" in (outcome.error or "")

    def test_deadline_is_enforced(self) -> None:
        with _Service(_batch_responder(["running"])) as svc:
            provider = BatchProvider(svc.url, poll_interval=0.01, timeout=5.0)
            outcome = provider.invoke(ModelInvocation(model_id="m1", timeout=0.15))
            provider.close()
            assert outcome.error_code == "timeout" and outcome.retryable is True

    def test_missing_job_id_is_invalid_spec(self) -> None:
        with _Service(lambda *a: (200, {"status": "queued"})) as svc:
            provider = BatchProvider(svc.url, poll_interval=0.01, timeout=1.0)
            outcome = provider.invoke(ModelInvocation(model_id="m1"))
            provider.close()
            assert outcome.error_code == "invalid_spec"


# --------------------------------------------------------------------------- #
# ⑤ provider 选择
# --------------------------------------------------------------------------- #
class TestChooseProvider:
    def test_batch_protocol_gets_batch_provider(self) -> None:
        for protocol in ("batch", "job", "oge-job"):
            provider = choose_provider(_model_card(protocol=protocol))
            assert isinstance(provider, BatchProvider), protocol
            provider.close()

    def test_http_protocols_get_inference_provider(self) -> None:
        for protocol in ("http", "https", "rest", "inference"):
            provider = choose_provider(_model_card(protocol=protocol))
            assert isinstance(provider, InferenceProvider), protocol
            provider.close()

    def test_unknown_protocol_is_explicit(self) -> None:
        with pytest.raises(ModelError, match="未知的 access.protocol"):
            choose_provider(_model_card(protocol="grpc"))

    def test_missing_endpoint_is_explicit(self) -> None:
        card = GeoCardBuilder("m", "model", "m", "d").build()
        with pytest.raises(ModelError, match="endpoint"):
            choose_provider(card)

    def test_injected_provider_wins(self) -> None:
        with _Service(_ok_responder({"v": 1})) as svc:
            provider = InferenceProvider(svc.url, timeout=5.0)
            outcome = invoke_model(
                _model_card(), {}, capabilities=_caps(svc.url), providers={"inference": provider}
            )
            provider.close()
            assert outcome.ok and outcome.outputs == {"v": 1}

    def test_declared_batch_model_is_not_silently_called_synchronously(self) -> None:
        """声明 batch 却没有注册 batch provider → 明确失败，不退回同步调用。"""
        outcome = invoke_model(
            _model_card(protocol="batch"),
            {},
            capabilities=_caps("http://n:1"),
            providers={"inference": InferenceProvider("http://n:1")},
        )
        assert outcome.error_code == "execution_error"
        assert "batch" in (outcome.error or "")


# --------------------------------------------------------------------------- #
# ⑥ 接到 GeoTask 契约上
# --------------------------------------------------------------------------- #
def _backend_task(task_id: str = "t1", **spec: Any) -> GeoTask:
    task = GeoTask(task_id=task_id, kind=TaskKind.MODEL, spec=spec)
    task.timeout_seconds = 10.0
    return task


class TestModelBackend:
    def test_task_succeeds_with_outputs(self) -> None:
        with _Service(_ok_responder({"embedding": [3.0]})) as svc:
            backend = ModelBackend(
                {"m1": _model_card(endpoint=svc.url)}, capabilities=_caps(svc.url)
            )
            task = _backend_task(model="m1", params={"top_k": 2})
            backend.submit(task, ExecutionContext(task=task))
            outcome = backend.wait("t1", timeout=10)
            backend.close()

            assert outcome is not None
            assert outcome.state is TaskState.SUCCEEDED
            assert outcome.outputs == {"embedding": [3.0]}

    def test_unknown_model_is_invalid_spec(self) -> None:
        backend = ModelBackend(capabilities=_caps("http://n:1"))
        task = _backend_task(model="ghost")
        backend.submit(task, ExecutionContext(task=task))
        outcome = backend.wait("t1", timeout=10)
        backend.close()
        assert outcome is not None and outcome.state is TaskState.FAILED
        assert outcome.error is not None and outcome.error.code == "invalid_spec"

    def test_error_code_and_retryability_cross_the_boundary(self) -> None:
        with _Service(lambda *a: (503, {"error": "unavailable"})) as svc:
            backend = ModelBackend(
                {"m1": _model_card(endpoint=svc.url)}, capabilities=_caps(svc.url)
            )
            task = _backend_task(model="m1")
            backend.submit(task, ExecutionContext(task=task))
            outcome = backend.wait("t1", timeout=10)
            backend.close()
            assert outcome is not None and outcome.error is not None
            assert outcome.error.code == "resource_unavailable"
            assert outcome.error.retryable is True, "5xx 必须能被调度器重试"

    def test_gate_rejection_reaches_the_scheduler_with_reasons(self) -> None:
        backend = ModelBackend(
            {"m1": _model_card(runtime={"cpu": 99, "memory": "999Gi"})},
            capabilities=_caps("http://n:1", cpu=1, memory_gb=1.0),
        )
        task = _backend_task(model="m1")
        backend.submit(task, ExecutionContext(task=task))
        outcome = backend.wait("t1", timeout=10)
        last = backend.last_outcome("t1")
        backend.close()
        assert outcome is not None and outcome.error is not None
        assert outcome.error.code == "no_compatible_compute"
        assert last is not None and last.reasons

    def test_cancel_before_submit(self) -> None:
        backend = ModelBackend({"m1": _model_card()}, capabilities=_caps("http://n:1"))
        task = _backend_task(model="m1")
        ctx = ExecutionContext(task=task, _should_cancel=lambda: True)
        backend.submit(task, ctx)
        outcome = backend.wait("t1", timeout=10)
        backend.close()
        assert outcome is not None and outcome.state is TaskState.CANCELLED

    def test_capabilities_are_honest(self) -> None:
        backend = ModelBackend()
        caps = backend.capabilities()
        backend.close()
        assert caps.kinds == frozenset({"model", "batch"})
        assert caps.supports_cancel is False, "远端计算撤不回来，不能假装支持"
        assert caps.supports_progress is False
        assert set(caps.resource_dims) == {"cpu", "memory_mb", "gpu"}

    def test_cancel_returns_false(self) -> None:
        backend = ModelBackend()
        handle = backend.submit(_backend_task(), ExecutionContext(task=_backend_task()))
        assert backend.cancel(handle) is False
        backend.close()

    def test_task_inputs_are_flattened(self) -> None:
        """``task.inputs`` 的引用直接作为入参名传出，不在这里解引用。"""
        from geonexus.geotask.model import TaskIO

        with _Service(_ok_responder({})) as svc:
            backend = ModelBackend(
                {"m1": _model_card(endpoint=svc.url)}, capabilities=_caps(svc.url)
            )
            task = _backend_task(model="m1")
            task.inputs = {"image": TaskIO(ref="s3://bucket/a.tif")}
            backend.submit(task, ExecutionContext(task=task))
            backend.wait("t1", timeout=10)
            backend.close()
            assert svc.hits[0]["body"]["inputs"] == {"image": "s3://bucket/a.tif"}

    def test_provider_names(self) -> None:
        backend = ModelBackend(providers={"inference": InferenceProvider("http://x")})
        assert backend.provider_names() == ["inference"]
        backend.close()


# --------------------------------------------------------------------------- #
# ⑦ geo.invoke —— 加法式协议扩展
# --------------------------------------------------------------------------- #
class TestGeoInvoke:
    def test_methods_is_a_superset_of_the_original_four(self) -> None:
        assert METHODS[:3] == ("geo.capabilities", "geo.describe", "geo.execute")
        assert "geo.health" in METHODS
        assert "geo.invoke" in METHODS
        assert {"geo.execute", "geo.invoke"} == WRITE_METHODS

    def test_capabilities_advertises_availability(self) -> None:
        off = GeoMCPServer("n").capabilities()
        assert off["model_invocation"]["enabled"] is False
        assert "reason" in off["model_invocation"]

        server = GeoMCPServer(
            "n", model_backend=ModelBackend(providers={"inference": InferenceProvider("http://x")})
        )
        on = server.capabilities()
        assert on["model_invocation"]["enabled"] is True
        assert on["model_invocation"]["providers"] == ["inference"]
        assert on["model_invocation"]["compute_gate"] is True
        server.model_backend.close()

    def test_legacy_keys_unchanged(self) -> None:
        caps = GeoMCPServer("n").capabilities()
        for key in (
            "protocol",
            "version",
            "node",
            "methods",
            "tools",
            "resources",
            "skills",
            "geocards",
            "geocard_ids",
        ):
            assert key in caps, key

    def test_old_dispatcher_without_invoke_returns_method_not_found(self) -> None:
        """老节点（没有注册 geo.invoke）对它的响应是标准 JSON-RPC 错误。"""
        legacy = GeoMCPDispatcher(health_fn=lambda params: {"status": "ok"})
        response = legacy.dispatch(
            {"jsonrpc": "2.0", "id": 1, "method": "geo.invoke", "params": {}}
        )
        assert response["error"]["code"] == -32601
        assert "geo.invoke" not in response["error"]["data"]["supported"]

    def test_invoke_without_backend_is_an_explicit_error(self) -> None:
        server = GeoMCPServer("n")
        response = server._dispatcher.dispatch(
            {"jsonrpc": "2.0", "id": 1, "method": "geo.invoke", "params": {"model": "m1"}}
        )
        assert response["error"]["code"] == 2004
        assert response["error"]["data"]["capability"]["enabled"] is False

    def test_invoke_end_to_end_via_jsonrpc(self) -> None:
        with _Service(_ok_responder({"embedding": [9.0]})) as svc:
            card = _model_card(
                endpoint=svc.url,
                outputs=[{"name": "embedding", "type": "vector", "required": True}],
            )
            server = GeoMCPServer(
                "n",
                model_backend=ModelBackend(
                    {"clip-vit-b32": card},
                    capabilities=_caps(svc.url),
                    providers={"inference": InferenceProvider(svc.url, timeout=10.0)},
                ),
            )
            server.register_geocard(card)

            response = server._dispatcher.dispatch(
                {
                    "jsonrpc": "2.0",
                    "id": "r1",
                    "method": "geo.invoke",
                    "params": {
                        "model": "clip-vit-b32",
                        "inputs": {"text": "hi"},
                        "params": {"top_k": 1},
                    },
                }
            )
            server.model_backend.close()

            assert "error" not in response, response
            result = response["result"]
            assert result["status"] == "succeeded"
            assert result["outputs"] == {"embedding": [9.0]}
            assert result["model_id"] == "clip-vit-b32"
            assert result["provider"] == "inference"
            assert svc.hits[0]["body"]["params"] == {"top_k": 1}

    def test_invoke_on_non_model_card_is_rejected(self) -> None:
        card = GeoCardBuilder("skill-1", "skill", "s", "d").build()
        server = GeoMCPServer("n", model_backend=ModelBackend())
        server.register_geocard(card)
        with pytest.raises(Exception) as exc:
            server.invoke(InvokeParams(model="skill-1"))
        assert getattr(exc.value, "code", None) == 2004
        server.model_backend.close()

    def test_invoke_unknown_card(self) -> None:
        server = GeoMCPServer("n", model_backend=ModelBackend())
        with pytest.raises(Exception) as exc:
            server.invoke(InvokeParams(model="ghost"))
        assert getattr(exc.value, "code", None) == 2002
        server.model_backend.close()

    def test_gate_rejection_surfaces_as_a_result_not_an_error(self) -> None:
        """门禁拒绝是**一次成功的协议调用**、一次说明理由的失败调用。"""
        card = _model_card("m1", runtime={"cpu": 99, "memory": "999Gi"})
        server = GeoMCPServer(
            "n",
            model_backend=ModelBackend(
                {"m1": card}, capabilities=_caps("http://n:1", cpu=1, memory_gb=1.0)
            ),
        )
        server.register_geocard(card)
        response = server._dispatcher.dispatch(
            {"jsonrpc": "2.0", "id": 1, "method": "geo.invoke", "params": {"model": "m1"}}
        )
        server.model_backend.close()
        assert "error" not in response
        assert response["result"]["status"] == "failed"
        assert response["result"]["error_code"] == "no_compatible_compute"
        assert response["result"]["reasons"]


# --------------------------------------------------------------------------- #
# ⑧ HTTP 传输 + 鉴权（geo.invoke 是写方法）
# --------------------------------------------------------------------------- #
def _start_server(app: Any) -> tuple[Any, str]:
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if server.started:
            break
        time.sleep(0.05)
    else:  # pragma: no cover - 启动失败要让测试红，而不是卡住
        raise RuntimeError("server did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, f"http://127.0.0.1:{port}"


class TestGeoInvokeOverHttp:
    def test_requires_api_key_and_works_with_it(self) -> None:
        import httpx

        with _Service(_ok_responder({"v": 1})) as svc:
            card = _model_card("m1", endpoint=svc.url)
            server = GeoMCPServer(
                "auth-node",
                api_keys={"k1"},
                model_backend=ModelBackend(
                    {"m1": card},
                    capabilities=_caps(svc.url),
                    providers={"inference": InferenceProvider(svc.url, timeout=10.0)},
                ),
            )
            server.register_geocard(card)
            _, url = _start_server(server.create_app())
            payload = {"jsonrpc": "2.0", "id": 1, "method": "geo.invoke", "params": {"model": "m1"}}
            try:
                denied = httpx.post(url + "/geomcp", json=payload, timeout=10)
                assert denied.status_code == 401
                assert denied.json()["error"]["code"] == -32602

                allowed = httpx.post(
                    url + "/geomcp", json=payload, timeout=10, headers={"X-API-Key": "k1"}
                )
                assert allowed.status_code == 200
                assert allowed.json()["result"]["outputs"] == {"v": 1}

                # 发现面依旧开放
                caps = httpx.get(url + "/capabilities", timeout=10).json()
                assert caps["model_invocation"]["enabled"] is True
            finally:
                server.model_backend.close()
