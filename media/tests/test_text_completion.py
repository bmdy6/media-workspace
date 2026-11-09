import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from media.core.errors import MediaError
from media.core.text_completion import stream_text


@pytest.fixture(autouse=True)
def reset_shared_text_client(optional_provider_catalog):
    import media.core.text_completion as module

    module._close_shared_client()
    yield
    module._close_shared_client()


def test_stream_text_uses_fast_off_params_and_hides_reasoning(monkeypatch):
    import media.core.text_completion as module

    class Response:
        status_code = 200

        def __init__(self, body: bytes):
            self.body = body
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

        def iter_bytes(self):
            midpoint = len(self.body) // 2
            yield self.body[:midpoint]
            yield self.body[midpoint:]

    body = (
        b'data: {"choices":[{"delta":{"reasoning_content":"hidden"}}]}\n\n'
        + 'data: {"choices":[{"delta":{"content":"你"}}]}\n\n'.encode()
        + 'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'.encode()
        + b"data: [DONE]\n\n"
    )
    response = Response(body)
    captured = {}
    timing_stages = []
    transport_stages = []

    class Client:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        def stream(self, method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured.update(kwargs)
            trace = kwargs["extensions"]["trace"]
            trace("connection.connect_tcp.started", {"host": "fixture-only"})
            trace("connection.connect_tcp.complete", {"return_value": "discarded"})
            return response

    monkeypatch.setattr(module.httpx, "Client", Client)
    monkeypatch.setattr(module, "get_config", lambda *args, **kwargs: "test-value")

    chunks = list(stream_text(
        [{"role": "user", "content": "hello"}],
        model_id="deepseek/deepseek-v4.1-flash",
        thinking_mode="off",
        on_timing=timing_stages.append,
        on_transport_timing=lambda stage, elapsed_ms: transport_stages.append(
            (stage, elapsed_ms)
        ),
    ))

    assert chunks == ["你", "好"]
    assert "hidden" not in json.dumps(chunks, ensure_ascii=False)
    assert captured["json"]["stream"] is True
    assert captured["json"]["thinking"] == {"type": "disabled"}
    assert captured["json"]["reasoning_effort"] == "off"
    assert captured["timeout"] > 0
    assert response.closed is True
    assert timing_stages == ["upstream_headers", "first_nonempty_delta"]
    assert transport_stages[0][0] == "connect_tcp"
    assert type(transport_stages[0][1]) is int
    assert transport_stages[0][1] >= 0


def test_httpcore_trace_handles_http2_event_names_and_retry_attempts():
    import media.core.text_completion as module

    times_ns = iter(
        [
            0, 4_000_000,
            10_000_000, 13_000_000,
            16_000_000, 18_000_000,
            20_000_000, 21_000_000,
            30_000_000, 34_000_000,
            40_000_000, 45_000_000,
            50_000_000, 56_000_000,
            60_000_000, 62_000_000,
        ]
    )
    observed = []
    trace = module._HttpcoreTransportTrace(
        lambda stage, elapsed_ms: observed.append((stage, elapsed_ms)),
        clock_ns=lambda: next(times_ns),
    )
    sensitive_info = {
        "host": "fixture-secret-host",
        "request": "fixture-secret-body",
        "headers": "fixture-secret-header",
        "exception": "fixture-secret-token",
    }

    # Synthetic events exercise accumulation across attempts. The shared
    # text client does not configure HTTPX transport retries today.
    trace("connection.connect_tcp.started", sensitive_info)
    trace("connection.connect_tcp.failed", sensitive_info)
    trace("connection.connect_tcp.started", sensitive_info)
    trace("connection.connect_tcp.complete", sensitive_info)
    trace("connection.start_tls.started", sensitive_info)
    trace("connection.start_tls.complete", sensitive_info)
    # These are the current httpcore HTTP/2 event names; the trace parser
    # intentionally does not depend on the http1/http2 protocol prefix.
    trace("http2.send_request_headers.started", sensitive_info)
    trace("http2.send_request_headers.complete", sensitive_info)
    trace("http2.send_request_body.started", sensitive_info)
    trace("http2.send_request_body.complete", sensitive_info)
    trace("http2.receive_response_headers.started", sensitive_info)
    trace("http2.receive_response_headers.complete", sensitive_info)
    # Current HTTP/1.1 uses receive_response_headers; retain the older alias.
    trace("http11.receive_response_headers.started", sensitive_info)
    trace("http11.receive_response_headers.complete", sensitive_info)
    trace("http11.receive_response.started", sensitive_info)
    trace("http11.receive_response.complete", sensitive_info)
    trace("connection.pool_wait.started", sensitive_info)
    trace("connection.pool_wait.complete", sensitive_info)

    assert observed == [
        ("connect_tcp", 4),
        ("connect_tcp", 7),
        ("start_tls", 2),
        ("send_request_headers", 1),
        ("send_request_body", 4),
        ("receive_response_headers", 5),
        ("receive_response_headers", 11),
        ("receive_response_headers", 13),
    ]
    assert "fixture-secret" not in json.dumps(observed)


def test_stream_text_uses_request_local_httpcore_timings_with_local_server(monkeypatch):
    import media.core.text_completion as module

    sse_body = (
        b'data: {"choices":[{"delta":{"content":"fixture"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    slow_started = threading.Event()
    release_slow = threading.Event()
    request_counts = {"slow": 0, "fast": 0, "status_failure": 0}
    protocol_versions = []
    request_counts_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            label = self.path.lstrip("/")
            with request_counts_lock:
                request_counts[label] += 1
                protocol_versions.append(self.request_version)

            if label == "slow":
                slow_started.set()
                if not release_slow.wait(timeout=5):
                    return
            elif label in {"fast", "status_failure"}:
                if not slow_started.wait(timeout=5):
                    return

            status = 503 if label == "status_failure" else 200
            body = b"unavailable" if status != 200 else sse_body
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            # Distinguish response-header wait from waiting for the first body
            # delta without involving a model or external provider.
            time.sleep(0.08)
            self.wfile.write(body)
            self.wfile.flush()

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    runtime = {
        "request": {
            "chat_total_budget_seconds": 5,
            "chat_max_output_chars": 100,
            "chat_max_sse_event_bytes": 512,
        }
    }

    def fake_chat_request(messages, *, model_id, thinking_mode, stream):
        label = messages[-1]["content"]
        return (
            runtime,
            {},
            "off",
            f"http://127.0.0.1:{server.server_port}/{label}",
            {"model": "fixture", "messages": messages, "stream": stream},
            {},
            2.0,
        )

    monkeypatch.setattr(module, "_chat_request", fake_chat_request)
    module._SHARED_CLIENT = module.httpx.Client(trust_env=False)
    timings_by_request = {}
    transport_by_request = {}

    def run_stream(label):
        timing_events = {}
        transport_events = []
        chunks = list(
            module.stream_text(
                [{"role": "user", "content": label}],
                on_timing=lambda stage: timing_events.setdefault(
                    stage, time.perf_counter()
                ),
                on_transport_timing=lambda stage, elapsed_ms: transport_events.append(
                    (stage, elapsed_ms)
                ),
            )
        )
        timings_by_request[label] = timing_events
        transport_by_request[label] = transport_events
        return chunks

    pool = ThreadPoolExecutor(max_workers=2)
    try:
        slow_future = pool.submit(run_stream, "slow")
        assert slow_started.wait(timeout=3)
        time.sleep(0.1)
        fast_future = pool.submit(run_stream, "fast")
        fast_chunks = fast_future.result(timeout=5)
        release_slow.set()
        slow_chunks = slow_future.result(timeout=5)

        # HTTP status failures are surfaced once; the text-completion client
        # has no automatic status retry, avoiding hidden duplicate requests.
        with pytest.raises(MediaError, match="HTTP 503"):
            run_stream("status_failure")
        assert request_counts["status_failure"] == 1
    finally:
        release_slow.set()
        pool.shutdown(wait=True)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    assert fast_chunks == slow_chunks == ["fixture"]
    assert request_counts == {"slow": 1, "fast": 1, "status_failure": 1}
    assert protocol_versions == ["HTTP/1.1"] * 3
    for label in ("slow", "fast"):
        transport = dict(transport_by_request[label])
        assert {
            "connect_tcp",
            "send_request_headers",
            "send_request_body",
            "receive_response_headers",
        } <= set(transport)
        timings = timings_by_request[label]
        assert list(timings) == ["upstream_headers", "first_nonempty_delta"]
        assert (
            timings["first_nonempty_delta"] - timings["upstream_headers"]
            >= 0.05
        )

    slow_wait = dict(transport_by_request["slow"])["receive_response_headers"]
    fast_wait = dict(transport_by_request["fast"])["receive_response_headers"]
    assert slow_wait >= fast_wait + 50


def test_stream_text_requires_done_and_visible_content(monkeypatch):
    import media.core.text_completion as module

    class Response:
        status_code = 200

        def __init__(self, body: bytes):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self):
            yield self.body

    class Client:
        body = b""

        def __init__(self, **kwargs):
            pass

        def stream(self, method, url, **kwargs):
            return Response(self.body)

    monkeypatch.setattr(module.httpx, "Client", Client)
    monkeypatch.setattr(module, "get_config", lambda *args, **kwargs: "test-value")

    Client.body = b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
    with pytest.raises(MediaError, match="意外结束"):
        list(stream_text(
            [{"role": "user", "content": "hello"}],
            model_id="deepseek/deepseek-v4.1-flash",
            thinking_mode="off",
        ))

    Client.body = b'data: {"choices":[{"delta":{"reasoning_content":"hidden"}}]}\n\ndata: [DONE]\n\n'
    with pytest.raises(MediaError, match="响应为空"):
        list(stream_text(
            [{"role": "user", "content": "hello"}],
            model_id="deepseek/deepseek-v4.1-flash",
            thinking_mode="off",
        ))


def test_stream_text_reuses_one_http_client(monkeypatch):
    import media.core.text_completion as module

    body = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
    created = []

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self):
            yield body

    class Client:
        def __init__(self, **kwargs):
            created.append(self)

        def stream(self, method, url, **kwargs):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(module.httpx, "Client", Client)
    monkeypatch.setattr(module, "get_config", lambda *args, **kwargs: "test-value")

    for _ in range(2):
        assert list(stream_text(
            [{"role": "user", "content": "hello"}],
            model_id="deepseek/deepseek-v4.1-flash",
            thinking_mode="off",
        )) == ["ok"]

    assert len(created) == 1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reason", ["stop", "length", "content_filter", "tool_calls"])
def test_text_completion_rejects_noncomplete_finish_reason(monkeypatch, stream, reason):
    import httpx
    import media.core.text_completion as module

    choice = {"message": {"content": "partial"}, "delta": {"content": "partial"},
              "finish_reason": reason}

    def handle(_request):
        if stream:
            body = 'data: ' + json.dumps({"choices": [choice]}) + '\n\ndata: [DONE]\n\n'
            return httpx.Response(200, content=body.encode())
        return httpx.Response(200, json={"choices": [choice]})

    monkeypatch.setattr(module, "get_config", lambda key, **kwargs: "https://provider.example/v1" if key.endswith("BASE_URL") else "fixture-only")
    with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False) as client:
        monkeypatch.setattr(module, "_get_shared_client", lambda: client)

        def request():
            kwargs = {"model_id": "deepseek/deepseek-v4.1-flash", "thinking_mode": "off"}
            messages = [{"role": "user", "content": "synthetic"}]
            return ''.join(module.stream_text(messages, **kwargs)) if stream else module.complete_text(messages, **kwargs)

        if reason == "stop":
            assert request() == "partial"
        else:
            with pytest.raises(MediaError, match="未完整返回正文"):
                request()
