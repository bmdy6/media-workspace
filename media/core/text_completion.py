"""Authenticated text completion helpers for Media Workspace."""
from __future__ import annotations

import atexit
import json
import os
from contextlib import contextmanager, ExitStack
import threading
import time
from typing import Any, Callable, Generator, Literal, Optional

import httpx

from .config import get_config
from .errors import ConfigError, MediaError
from .runtime_config import RuntimeConfigError, get_model, get_provider, get_runtime_config

_StreamTimingStage = Literal["upstream_headers", "first_nonempty_delta"]
_TransportTimingStage = Literal[
    "connect_tcp",
    "start_tls",
    "send_request_headers",
    "send_request_body",
    "receive_response_headers",
]
_TransportTimingCallback = Callable[[_TransportTimingStage, int], None]


class _HttpcoreTransportTrace:
    """Collect allowlisted httpcore stage durations without retaining event details."""

    # httpcore exposes connect_tcp as one interval; it does not split DNS from TCP.
    _STAGES = frozenset(
        {
            "connect_tcp",
            "start_tls",
            "send_request_headers",
            "send_request_body",
            "receive_response_headers",
            "receive_response",
        }
    )

    def __init__(
        self,
        callback: _TransportTimingCallback,
        *,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        self._callback = callback
        self._clock_ns = clock_ns
        self._started_ns: dict[str, list[int]] = {}
        self._elapsed_ns: dict[str, int] = {}

    def __call__(self, event_name: str, _info: dict[str, Any]) -> None:
        # httpcore's info includes hostnames, request objects, streams, and
        # exceptions. Deliberately inspect only the event name and our clock.
        parts = event_name.rsplit(".", 2)
        if len(parts) != 3:
            return
        stage, event = parts[-2], parts[-1]
        if stage not in self._STAGES:
            return
        if stage == "receive_response":
            stage = "receive_response_headers"
        if event == "started":
            self._started_ns.setdefault(stage, []).append(self._clock_ns())
            return
        if event not in {"complete", "failed"}:
            return
        starts = self._started_ns.get(stage)
        if not starts:
            return

        elapsed_ns = max(0, self._clock_ns() - starts.pop())
        self._elapsed_ns[stage] = self._elapsed_ns.get(stage, 0) + elapsed_ns
        try:
            self._callback(stage, round(self._elapsed_ns[stage] / 1_000_000))
        except Exception:
            # Timing must never turn a successful upstream request into a failure.
            return


_SHARED_CLIENT: httpx.Client | None = None
_SHARED_CLIENT_LOCK = threading.Lock()
_OFFICIAL_CLIENTS: dict[str, httpx.Client] = {}


class OfficialConnectionError(MediaError):
    """Safe, actionable error; never include credentials or upstream bodies."""


@contextmanager
def _official_errors(model):
    try:
        yield
    except httpx.ReadTimeout as exc:
        if model.get("provider") == "openai_official":
            raise OfficialConnectionError("OpenAI 模型响应等待超过30秒，请稍后重试") from exc
        raise
    except httpx.TransportError as exc:
        if model.get("provider") == "openai_official":
            raise OfficialConnectionError("OpenAI 连接不可用，请稍后重试或检查服务配置") from exc
        raise


def _text_transport(model, timeout):
    if model.get("provider") != "openai_official":
        return _get_shared_client(), timeout
    # Dedicated direct HTTPS pool; never inherit an ambient proxy.
    with _SHARED_CLIENT_LOCK:
        if "relay" not in _OFFICIAL_CLIENTS:
            _OFFICIAL_CLIENTS["relay"] = httpx.Client(trust_env=False)
        client = _OFFICIAL_CLIENTS["relay"]
    # Connection setup and model response waiting have separate budgets.
    return client, httpx.Timeout(connect=3.0, read=30.0, write=5.0, pool=3.0)


@contextmanager
def _text_response(client, model, url, *, stream, headers, payload, timeout, extensions=None):
    """Retry once only before any request headers/body have started sending.

    The yielded response/body is outside the retry loop: never replay a request
    after HTTP status, read/write/pool failure, partial output, or caller error.
    """
    if model.get("provider") != "openai_official":
        if stream:
            with client.stream("POST", url, headers=headers, json=payload,
                               timeout=timeout, extensions=extensions) as response:
                yield response
        else:
            yield client.post(url, headers=headers, json=payload, timeout=timeout)
        return
    # Spend at most ~3s across connection retries; a second attempt gets only
    # the remaining connection budget, never a fresh three seconds.
    connect_deadline = time.monotonic() + timeout.connect
    sent = False
    original_trace = (extensions or {}).get("trace")

    def trace(name, info):
        nonlocal sent
        if name.endswith((".send_request_headers.started", ".send_request_body.started")):
            sent = True
        if original_trace is not None:
            original_trace(name, info)

    request_extensions = {**(extensions or {}), "trace": trace}
    for attempt in range(2):
        stack = ExitStack()
        attempt_timeout = timeout
        if attempt:
            attempt_timeout = httpx.Timeout(connect=max(0.001, connect_deadline - time.monotonic()),
                                            read=timeout.read, write=timeout.write, pool=timeout.pool)
        try:
            if stream:
                response = stack.enter_context(client.stream(
                    "POST", url, headers=headers, json=payload, timeout=attempt_timeout,
                    extensions=request_extensions,
                ))
            else:
                response = client.post(url, headers=headers, json=payload, timeout=attempt_timeout,
                                       extensions=request_extensions)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            stack.close()
            if model.get("provider") != "openai_official" or sent or attempt:
                raise
            continue
        except BaseException:
            stack.close()
            raise
        with stack:
            yield response
        return


def _get_shared_client() -> httpx.Client:
    """Reuse HTTP connections across text requests within one worker process."""
    global _SHARED_CLIENT
    if _SHARED_CLIENT is None:
        with _SHARED_CLIENT_LOCK:
            if _SHARED_CLIENT is None:
                _SHARED_CLIENT = httpx.Client()
    return _SHARED_CLIENT


def _close_shared_client() -> None:
    global _SHARED_CLIENT
    with _SHARED_CLIENT_LOCK:
        client = _SHARED_CLIENT
        _SHARED_CLIENT = None
        official_clients = list(_OFFICIAL_CLIENTS.values())
        _OFFICIAL_CLIENTS.clear()
    for official in official_clients:
        official.close()
    if client is not None:
        close = getattr(client, "close", None)
        if close is not None:
            close()


atexit.register(_close_shared_client)


def _chat_request(
    messages: list[dict[str, str]],
    *,
    model_id: Optional[str],
    thinking_mode: Optional[str],
    stream: bool,
) -> tuple[dict[str, Any], dict[str, Any], str, str, dict[str, Any], dict[str, str], float]:
    runtime = get_runtime_config()
    model = get_model(model_id, "chat")
    selected_id = model["id"]
    selected_thinking = (
        thinking_mode if thinking_mode is not None
        else runtime["defaults"].get("thinking_mode")
    )

    if selected_thinking not in model.get("thinking_modes", []):
        raise RuntimeConfigError(
            f"Thinking mode is not supported by {selected_id}: {selected_thinking}"
        )

    provider = get_provider(model["provider"])
    api_key = get_config(
        provider["api_key_env"],
        required=True,
        project=provider.get("api_key_vault_project"),
    )
    base_url = provider.get("base_url")
    if provider.get("base_url_env"):
        base_url = get_config(
            provider["base_url_env"],
            default=provider.get("default_base_url"),
        )
    if not base_url:
        raise ConfigError(f"Provider base URL is not configured: {model['provider']}")

    payload: dict[str, Any] = {
        **model.get("request_parameters", {}),
        **model.get("thinking_mode_parameters", {}).get(selected_thinking, {}),
        "model": selected_id,
        "messages": messages,
        "stream": stream,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = min(
        runtime["request"]["chat_timeout_seconds"],
        runtime["request"]["chat_total_budget_seconds"],
    )
    url = f"{str(base_url).rstrip('/')}/chat/completions"
    return runtime, model, selected_thinking, url, payload, headers, timeout


def _iter_sse_data(
    response: httpx.Response,
    max_event_bytes: int,
    *,
    deadline: Optional[float] = None,
) -> Generator[str, None, None]:
    buffer = bytearray()
    data_lines: list[bytes] = []
    event_bytes = 0

    def consume_line(raw_line: bytes) -> Optional[str]:
        nonlocal event_bytes
        line = raw_line.rstrip(b"\r")
        if not line:
            if not data_lines:
                event_bytes = 0
                return None
            payload = b"\n".join(data_lines).decode("utf-8", errors="strict")
            data_lines.clear()
            event_bytes = 0
            return payload
        if line.startswith(b":"):
            return None
        field, separator, value = line.partition(b":")
        if field == b"data":
            if separator and value.startswith(b" "):
                value = value[1:]
            data_lines.append(value)
            event_bytes += len(value) + 1
            if event_bytes > max_event_bytes:
                raise MediaError("模型流事件超过大小限制")
        return None

    for chunk in response.iter_bytes():
        if deadline is not None and time.monotonic() > deadline:
            raise MediaError("模型响应超过时间限制")
        buffer.extend(chunk)
        while True:
            line_end = buffer.find(b"\n")
            if line_end < 0:
                if len(buffer) > max_event_bytes:
                    raise MediaError("模型流事件超过大小限制")
                break
            if line_end > max_event_bytes:
                raise MediaError("模型流事件超过大小限制")
            line = bytes(buffer[:line_end])
            del buffer[:line_end + 1]
            payload = consume_line(line)
            if payload is not None:
                yield payload

    if buffer:
        payload = consume_line(bytes(buffer))
        if payload is not None:
            yield payload
    if data_lines:
        payload = b"\n".join(data_lines).decode("utf-8", errors="strict")
        if len(payload.encode("utf-8")) > max_event_bytes:
            raise MediaError("模型流事件超过大小限制")
        yield payload


def complete_text(
    messages: list[dict[str, str]],
    *,
    model_id: Optional[str] = None,
    thinking_mode: Optional[str] = None,
) -> str:
    """Call the configured verified chat model without exposing provider secrets to the browser."""
    _runtime, _model, _thinking, url, payload, headers, timeout = _chat_request(
        messages,
        model_id=model_id,
        thinking_mode=thinking_mode,
        stream=False,
    )
    client, timeout = _text_transport(_model, timeout)
    with _official_errors(_model):
        with _text_response(client, _model, url, stream=False, headers=headers,
                            payload=payload, timeout=timeout) as response:
            pass

    if response.status_code != 200:
        raise MediaError(f"AI API 请求失败 (HTTP {response.status_code})")

    data = response.json()
    if not isinstance(data, dict):
        raise MediaError("AI 文本响应格式无效")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise MediaError("AI 文本响应格式无效")
    if choices[0].get("finish_reason") not in (None, "stop"):
        raise MediaError("模型未完整返回正文，请缩短文字后重试")
    message = choices[0].get("message")
    result = message.get("content") if isinstance(message, dict) else None
    if not isinstance(result, str) or not result.strip():
        raise MediaError("AI 文本响应格式无效")
    return result


def stream_text(
    messages: list[dict[str, str]],
    *,
    model_id: Optional[str] = None,
    thinking_mode: Optional[str] = None,
    on_timing: Optional[Callable[[_StreamTimingStage], None]] = None,
    on_transport_timing: Optional[_TransportTimingCallback] = None,
) -> Generator[str, None, None]:
    """Yield visible text deltas and optionally report request-local timing stages."""
    runtime, _model, _thinking, url, payload, headers, timeout = _chat_request(
        messages,
        model_id=model_id,
        thinking_mode=thinking_mode,
        stream=True,
    )
    request = runtime["request"]
    deadline = time.monotonic() + request["chat_total_budget_seconds"]
    max_chars = request["chat_max_output_chars"]
    emitted_chars = 0
    stream_complete = False
    saw_content = False

    client, timeout = _text_transport(_model, timeout)
    extensions = None
    if on_transport_timing is not None:
        extensions = {"trace": _HttpcoreTransportTrace(on_transport_timing)}
    with _official_errors(_model), _text_response(
        client, _model, url, stream=True,
        headers=headers,
        payload=payload,
        timeout=timeout,
        extensions=extensions,
    ) as response:
        if on_timing is not None:
            on_timing("upstream_headers")
        if response.status_code != 200:
            raise MediaError(f"AI API 请求失败 (HTTP {response.status_code})")
        for data in _iter_sse_data(
            response,
            request["chat_max_sse_event_bytes"],
            deadline=deadline,
        ):
            if time.monotonic() > deadline:
                raise MediaError("模型响应超过时间限制")
            if data == "[DONE]":
                stream_complete = True
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError as exc:
                raise MediaError("模型流返回格式无效") from exc
            if not isinstance(chunk, dict):
                raise MediaError("模型流返回格式无效")
            choices = chunk.get("choices") or []
            if not choices or not isinstance(choices[0], dict):
                continue
            if choices[0].get("finish_reason") not in (None, "stop"):
                raise MediaError("模型未完整返回正文，请缩短文字后重试")
            delta = choices[0].get("delta") or {}
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            if isinstance(content, str) and content:
                emitted_chars += len(content)
                if emitted_chars > max_chars:
                    raise MediaError("模型响应超过长度限制")
                if not saw_content and on_timing is not None:
                    on_timing("first_nonempty_delta")
                saw_content = True
                yield content

    if not stream_complete:
        raise MediaError("模型响应流意外结束")
    if not saw_content:
        raise MediaError("AI 文本响应为空")
