"""Bounded OpenAI Images SSE contract; no transport, persistence, or UI state.

Contract pin: openai/openai-python@86cae50785fb7b7bf2a5d2383370b214b304acd2
src/openai/types/image_{gen,edit}_{partial_image,completed}_event.py
This is the Images API, not Responses API image-generation events.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from itertools import chain
from typing import Any, Callable, Iterable, Iterator, Protocol

from .errors import GenerationError


_STREAM_ERROR = "上游图片流格式无效；未自动重试"
_LINE_END = re.compile(br"\r\n|\r|\n")


class BoundedImageStreamTransport(Protocol):
    """Trusted injected adapter, not arbitrary requests.post.

    MUST interrupt header/body reads at the absolute monotonic deadline or when
    is_cancelled() becomes true, including a slow-drip response. The returned
    response exposes status_code, headers, iter_content and close; iteration and
    close must honor the same bound and release every resource/worker on exit.
    No concrete live transport is shipped or enabled by this source slice.
    """

    def __call__(
        self, url: str, *, deadline: float, is_cancelled: Callable[[], bool],
        **request_options: Any,
    ) -> Any: ...


def stream_cancelled(predicate: Callable[[], bool] | None) -> bool:
    if predicate is None:
        return False
    try:
        cancelled = predicate()
        if type(cancelled) is not bool:
            raise ValueError()
        return cancelled
    except Exception:
        raise GenerationError("图片流取消状态检查失败；未自动重试") from None


@dataclass(frozen=True)
class ImagePartialPreview:
    index: int
    image_bytes: bytes = field(repr=False)
    output_format: str


def decode_stream_json(data: bytes) -> dict:
    """Reject deep/untrusted JSON without exposing its content in exceptions."""
    depth, quoted, escaped = 0, False, False
    for char in data:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted = True
        elif char in (91, 123):
            depth += 1
            if depth > 8:
                raise GenerationError(_STREAM_ERROR)
        elif char in (93, 125):
            depth -= 1
    try:
        result = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise GenerationError(_STREAM_ERROR) from None
    if not isinstance(result, dict):
        raise GenerationError(_STREAM_ERROR)
    return result


def image_event_budget(max_image_bytes: int) -> int:
    return ((max_image_bytes + 2) // 3) * 4 + 64 * 1024


def bounded_chunks(
    chunks: Iterable[bytes], max_bytes: int, check_active: Callable[[], None],
) -> Iterator[bytes]:
    total = 0
    iterator = iter(chunks)
    while True:
        check_active()
        try:
            chunk = next(iterator)
        except StopIteration:
            break
        except Exception:
            raise GenerationError("图片流传输中断；上游接单状态未知，未自动重试") from None
        check_active()
        if not isinstance(chunk, bytes):
            raise GenerationError(_STREAM_ERROR)
        total += len(chunk)
        if total > max_bytes:
            raise GenerationError("上游图片流超过大小限制；未自动重试")
        if chunk:
            yield chunk


def _sse_events(
    chunks: Iterable[bytes], max_event_bytes: int,
) -> Iterator[tuple[bytes, bytes]]:
    pending = bytearray()
    data: list[bytes] = []
    event = b""
    event_bytes = 0
    search_from = 0
    for chunk in chain(chunks, [None]):
        if chunk is not None:
            pending.extend(chunk)
        while match := _LINE_END.search(pending, search_from):
            # A CR at a chunk boundary might be the start of CRLF.
            if chunk is not None and match.group() == b"\r" and match.end() == len(pending):
                search_from = match.start()
                break
            line = bytes(pending[:match.start()])
            del pending[:match.end()]
            search_from = 0
            event_bytes += len(line) + len(match.group())
            if event_bytes > max_event_bytes:
                raise GenerationError("上游图片流事件超过大小限制；未自动重试")
            if not line:
                if data:
                    yield event, b"\n".join(data)
                data, event, event_bytes = [], b"", 0
                continue
            key, separator, value = line.partition(b":")
            if separator and value.startswith(b" "):
                value = value[1:]
            if key == b"data":
                data.append(value)
            elif key == b"event":
                event = value
        else:
            search_from = len(pending)
        if event_bytes + len(pending) > max_event_bytes:
            raise GenerationError("上游图片流事件超过大小限制；未自动重试")
    # No blank-line delimiter means an incomplete event, including at EOF.
    # Never guess that a last partial frame is a completed result.


def consume_image_stream(
    chunks: Iterable[bytes], *, operation: str, partial_images: int,
    max_image_bytes: int, on_partial: Callable[[ImagePartialPreview], None],
    check_active: Callable[[], None],
) -> bytes:
    max_event = image_event_budget(max_image_bytes)
    chunks = bounded_chunks(chunks, max_event * (partial_images + 1) + 65536, check_active)
    previous = -1
    for event_name, data in _sse_events(chunks, max_event):
        check_active()
        payload = decode_stream_json(data)
        kind = payload.get("type")
        if kind not in (f"{operation}.partial_image", f"{operation}.completed"):
            raise GenerationError(_STREAM_ERROR)
        if event_name and event_name != kind.encode("utf-8"):
            raise GenerationError(_STREAM_ERROR)
        encoded = payload.get("b64_json")
        if not isinstance(encoded, str) or len(encoded) > ((max_image_bytes + 2) // 3) * 4:
            raise GenerationError(_STREAM_ERROR)
        try:
            image = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise GenerationError(_STREAM_ERROR) from None
        if not image or len(image) > max_image_bytes:
            raise GenerationError(_STREAM_ERROR)
        if kind.endswith(".completed"):
            check_active()
            return image
        index = payload.get("partial_image_index")
        output_format = payload.get("output_format")
        if (
            type(index) is not int or index != previous + 1 or index >= partial_images
            or output_format not in ("png", "jpeg", "webp")
        ):
            raise GenerationError(_STREAM_ERROR)
        previous = index
        check_active()
        try:
            on_partial(ImagePartialPreview(index, image, output_format))
        except Exception:
            raise GenerationError("临时图片预览处理失败；未自动重试") from None
    check_active()
    raise GenerationError("上游图片流未返回完整最终图；未自动重试")
