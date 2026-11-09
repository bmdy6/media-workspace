"""Offline Images contract fixtures, not evidence of provider compatibility."""
from __future__ import annotations

import base64
import json
import traceback
from unittest.mock import Mock

import pytest
import requests

from media.core import image_service
from media.core.errors import GenerationError
from media.core.image_service import ImageCore
from media.core.image_stream import consume_image_stream, image_event_budget
from media.core.runtime_config import RuntimeConfigError, load_runtime_config


def event(kind="image_generation.partial_image", image=b"preview", index=0, **extra):
    data = {"type": kind, "b64_json": base64.b64encode(image).decode(), "output_format": "png"}
    if kind.endswith("partial_image"):
        data["partial_image_index"] = index
    return {**data, **extra}


def frame(data, newline=b"\n", named=True):
    name = b"event: " + data["type"].encode() + newline if named else b""
    return name + b"data: " + json.dumps(data).encode() + newline * 2


def completed(image=b"final", operation="image_generation"):
    return event(f"{operation}.completed", image)


def consume(chunks, callback=None, partial_images=2, max_image_bytes=100, check_active=lambda: None):
    return consume_image_stream(chunks, operation="image_generation", partial_images=partial_images,
        max_image_bytes=max_image_bytes, on_partial=callback or (lambda preview: None), check_active=check_active)


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
@pytest.mark.parametrize("chunk_size", [1, 2, 7, 8192])
def test_native_partial_and_final_survive_chunk_boundaries(newline, chunk_size):
    wire = b": keepalive" + newline * 2 + frame(event(), newline) + frame(event(index=1, image=b"newer"), newline) + frame(completed(), newline)
    seen = []
    assert consume([wire[i:i+chunk_size] for i in range(0, len(wire), chunk_size)], seen.append) == b"final"
    assert [(p.index, p.image_bytes, p.output_format) for p in seen] == [(0, b"preview", "png"), (1, b"newer", "png")]
    assert "preview" not in repr(seen[0])


def test_data_only_multiline_event_and_utf8_comment():
    wire = ': 中文注释\n\ndata: {"type":"image_generation.completed",\ndata: "b64_json":"ZmluYWw="}\n\n'.encode()
    assert consume([wire[i:i+1] for i in range(len(wire))]) == b"final"


@pytest.mark.parametrize("partials", [0, 1, 2, 3])
def test_completed_can_arrive_before_requested_partial_count(partials):
    seen = Mock()
    assert consume([frame(completed())], seen, partial_images=partials) == b"final"
    seen.assert_not_called()


def test_completed_is_terminal_and_does_not_emit_late_partials():
    def chunks():
        yield frame(completed()) + frame(event())
        raise AssertionError("Read beyond terminal completed")
    seen = Mock()
    assert consume(chunks(), seen) == b"final"
    seen.assert_not_called()


@pytest.mark.parametrize("wire", [b"", frame(event()), frame(completed()).rstrip(b"\n"), b"data: [DONE]\n\n"])
def test_eof_or_done_without_completed_is_never_success(wire):
    with pytest.raises(GenerationError):
        consume([wire])


@pytest.mark.parametrize("frames", [
    [event(index=1)], [event(index=-1)], [event(index=True)], [event(index="0")],
    [event(), event()], [event(), event(index=2)], [event(), event(index=1), event(index=2)],
    [event(output_format="svg")], [event(b64_json="bad!secret")], [event(b64_json="")],
    [event(b64_json=12)], [event("image_edit.partial_image")], [event("error", message="secret")],
    [event("response.image_generation_call.partial_image")], [event(type="secret\ud800")],
])
def test_invalid_partial_or_order_never_becomes_final(frames):
    wire = b"".join(frame(x, named=False) for x in frames) + frame(completed())
    with pytest.raises(GenerationError) as caught:
        consume([wire])
    assert "secret" not in "".join(traceback.format_exception(caught.value))


def test_event_name_must_match_payload_discriminator():
    with pytest.raises(GenerationError):
        consume([b"event: image_generation.completed\n" + frame(event(), named=False)])


@pytest.mark.parametrize("wire", [
    b"data: not-json-secret\n\n", b"data: {\"secret\": " + b"[" * 9 + b"]" * 9 + b"}\n\n",
    b"data: []\n\n", b"data: \xff\n\n",
])
def test_malformed_json_is_safe(wire):
    with pytest.raises(GenerationError) as caught:
        consume([wire])
    assert "secret" not in "".join(traceback.format_exception(caught.value))


def test_per_image_decoded_budget_includes_padding_boundary():
    with pytest.raises(GenerationError):
        consume([frame(completed(b"four"))], max_image_bytes=3)
    with pytest.raises(GenerationError):
        consume([frame(completed(b"four"))], max_image_bytes=2)


@pytest.mark.parametrize("line", [b"data: ", b": ", b"ignored: "])
def test_unterminated_data_comment_unknown_field_are_bounded(line):
    with pytest.raises(GenerationError, match="大小限制"):
        consume([line + b"x" * image_event_budget(100)])


def test_multiline_event_and_total_keepalive_wire_are_bounded():
    with pytest.raises(GenerationError, match="大小限制"):
        consume([b"data: x\n" * 10000])
    with pytest.raises(GenerationError, match="大小限制"):
        consume([b": x\n\n"] * 60000)


class Response:
    def __init__(self, wire, content_type="text/event-stream", status=200):
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.wire = wire
        self.closed = False
    def iter_content(self, chunk_size):
        if isinstance(self.wire, bytes):
            for i in range(0, len(self.wire), chunk_size):
                yield self.wire[i:i+chunk_size]
        else:
            yield from self.wire
    def close(self):
        self.closed = True


@pytest.fixture
def configured(monkeypatch):
    runtime = load_runtime_config()
    model = next(x for x in runtime["models"]["image"] if x["id"] == "gpt-image-2.5-flare")
    model.update(enabled=True, available=True, verified=True)
    model["partial_streaming"]["enabled"] = True
    monkeypatch.setattr(image_service, "get_model", lambda *_args: model)
    monkeypatch.setattr(image_service, "get_runtime_config", lambda: runtime)
    monkeypatch.setattr(ImageCore, "_provider_api_key", lambda *_args: "test-key")
    return runtime, model


def call(core, **kwargs):
    return core._call_transit_api(final_prompt="synthetic", size="1024x1024", quality="low",
        model_id="gpt-image-2.5-flare", defer_download=True, on_partial_image=kwargs.pop("on_partial_image", lambda preview: None), **kwargs)


@pytest.mark.parametrize("edit", [False, True])
def test_service_stream_integrates_final_result_and_ephemeral_partial(configured, edit):
    operation = "image_edit" if edit else "image_generation"
    response = Response(frame(event(f"{operation}.partial_image")) + frame(completed(operation=operation)))
    post, seen = Mock(return_value=response), Mock()
    core = ImageCore(stream_transport=post)
    result = call(core, ref_bytes=b"reference" if edit else None, on_partial_image=seen)
    assert result.image_bytes == b"final" and result.model_id == "gpt-image-2.5-flare"
    assert response.closed and post.call_count == 1 and seen.call_count == 1
    arguments = post.call_args.kwargs
    assert arguments["stream"] is True
    assert isinstance(arguments["deadline"], float)
    assert arguments["is_cancelled"]() is False
    body = arguments["data" if edit else "json"]
    assert body["stream"] == ("true" if edit else True)
    assert body["partial_images"] == 2 and body["quality"] == "low" and body["n"] == 1
    assert post.call_args.args[0].endswith("/images/edits" if edit else "/images/generations")


def test_json_same_response_fallback_has_no_partial_and_no_second_request(configured):
    wire = json.dumps({"data": [{"b64_json": "ZmluYWw="}]}).encode()
    response = Response(wire, "application/json; charset=utf-8")
    post, partial = Mock(return_value=response), Mock()
    result = call(ImageCore(stream_transport=post), on_partial_image=partial)
    assert result.image_bytes == b"final" and response.closed
    partial.assert_not_called()
    assert post.call_count == 1


@pytest.mark.parametrize("response", [
    Response(frame(event())), Response(b"secret", "text/html"), Response(b"secret", "application/json"),
    Response(b"secret", status=400), Response(b"secret", status=503),
])
def test_service_failures_close_response_and_never_resubmit(configured, response):
    post = Mock(return_value=response)
    with pytest.raises(GenerationError) as caught:
        call(ImageCore(stream_transport=post))
    assert response.closed and post.call_count == 1
    assert "secret" not in "".join(traceback.format_exception(caught.value))


def test_transport_disconnect_after_partial_never_uses_last_frame(configured):
    def chunks():
        yield frame(event())
        raise requests.ConnectionError("secret")
    response = Response(chunks())
    post, partial = Mock(return_value=response), Mock()
    with pytest.raises(GenerationError, match="传输中断") as caught:
        call(ImageCore(stream_transport=post), on_partial_image=partial)
    assert partial.call_count == 1 and post.call_count == 1 and response.closed
    assert "secret" not in "".join(traceback.format_exception(caught.value))


def test_cancel_before_request_never_reads_credentials(configured, monkeypatch):
    credentials = Mock(side_effect=AssertionError("Credential access"))
    monkeypatch.setattr(ImageCore, "_provider_api_key", credentials)
    post = Mock()
    with pytest.raises(GenerationError, match="取消"):
        call(ImageCore(stream_transport=post), is_cancelled=lambda: True)
    post.assert_not_called()
    credentials.assert_not_called()


def test_cancel_after_partial_closes_without_final_or_resubmit(configured):
    response = Response(frame(event()) + frame(completed()))
    cancelled = False
    def partial(_):
        nonlocal cancelled
        cancelled = True
    post = Mock(return_value=response)
    with pytest.raises(GenerationError, match="取消"):
        call(ImageCore(stream_transport=post), on_partial_image=partial, is_cancelled=lambda: cancelled)
    assert post.call_count == 1 and response.closed


def test_deadline_covers_stream_reads(configured, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(image_service.time, "monotonic", lambda: now[0])
    def chunks():
        yield frame(event())
        now[0] = 600
        yield frame(completed())
    response = Response(chunks())
    with pytest.raises(GenerationError, match="总时限"):
        call(ImageCore(stream_transport=Mock(return_value=response)))
    assert response.closed


def test_callback_error_still_closes_transport(configured):
    response = Response(frame(event()))
    callback = Mock(side_effect=RuntimeError("secret response body"))
    with pytest.raises(GenerationError, match="预览处理失败") as caught:
        call(ImageCore(stream_transport=Mock(return_value=response)), on_partial_image=callback)
    assert response.closed
    assert "secret" not in "".join(traceback.format_exception(caught.value))


def test_catalog_stays_disabled_and_cannot_be_bypassed_with_callback():
    runtime = load_runtime_config()
    flare = next(x for x in runtime["models"]["image"] if x["id"] == "gpt-image-2.5-flare")
    assert runtime["defaults"]["image_model"] == "gpt-image-2"
    assert runtime["defaults"]["quality"] == "low"
    assert flare["partial_streaming"] == {"enabled": False, "partial_images": 2}
    assert all(flare[x] is False for x in ("enabled", "available", "verified"))
    post = Mock()
    with pytest.raises(RuntimeConfigError, match="not available"):
        call(ImageCore(stream_transport=post))
    post.assert_not_called()


def test_disabled_capability_uses_existing_nonstream_path(configured, monkeypatch):
    runtime, model = configured
    model["partial_streaming"]["enabled"] = False
    response = Mock(status_code=200)
    response.json.return_value = {"data": [{"b64_json": "ZmluYWw="}]}
    ordinary, stream, partial = Mock(return_value=response), Mock(), Mock()
    monkeypatch.setattr(image_service.requests, "post", ordinary)
    assert call(ImageCore(stream_transport=stream), on_partial_image=partial).image_bytes == b"final"
    assert "stream" not in ordinary.call_args.kwargs
    assert "stream" not in ordinary.call_args.kwargs["json"]
    stream.assert_not_called()
    partial.assert_not_called()


@pytest.mark.parametrize("flag", ["enabled", "available", "verified"])
def test_inconsistent_stream_gate_rejected_before_request(configured, flag):
    _, model = configured
    model[flag] = False
    post = Mock()
    with pytest.raises(RuntimeConfigError, match="requires verified"):
        call(ImageCore(stream_transport=post))
    post.assert_not_called()


@pytest.mark.parametrize("change", [
    {"enabled": True}, {"partial_images": 4}, {"partial_images": -1}, {"partial_images": True},
    {"enabled": "false"}, {"unknown": 1},
])
def test_runtime_rejects_unsafe_stream_configuration(tmp_path, change):
    runtime = load_runtime_config()
    model = next(x for x in runtime["models"]["image"] if x["id"] == "gpt-image-2.5-flare")
    model["partial_streaming"].update(change)
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(runtime))
    with pytest.raises(RuntimeConfigError, match="streaming"):
        load_runtime_config(path)


def test_partial_images_cannot_be_smuggled_through_request_parameters(tmp_path):
    runtime = load_runtime_config()
    runtime["models"]["image"][0]["request_parameters"]["generations"]["partial_images"] = 2
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(runtime))
    with pytest.raises(RuntimeConfigError, match="reserved"):
        load_runtime_config(path)


@pytest.mark.parametrize("field,value", [("id", "gpt-image-2"), ("api_protocol", "ohmygpt_images_polling")])
def test_stream_requires_exact_flare_and_images_protocol(configured, field, value):
    _, model = configured
    model[field] = value
    post = Mock()
    with pytest.raises(RuntimeConfigError, match="requires verified"):
        call(ImageCore(stream_transport=post))
    post.assert_not_called()


def test_stream_cannot_upgrade_provider_quality(configured):
    _, model = configured
    model["quality_map"] = {"low": "high"}
    post = Mock()
    with pytest.raises(RuntimeConfigError, match="quality must remain low"):
        call(ImageCore(stream_transport=post))
    post.assert_not_called()


def test_missing_callback_keeps_nonstream_path_even_if_capability_configured(configured, monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {"data": [{"b64_json": "ZmluYWw="}]}
    ordinary, stream = Mock(return_value=response), Mock()
    monkeypatch.setattr(image_service.requests, "post", ordinary)
    assert call(ImageCore(stream_transport=stream), on_partial_image=None).image_bytes == b"final"
    assert "stream" not in ordinary.call_args.kwargs
    stream.assert_not_called()


def test_transport_failure_before_headers_never_retries(configured):
    post = Mock(side_effect=requests.Timeout("secret"))
    with pytest.raises(GenerationError, match="传输中断") as caught:
        call(ImageCore(stream_transport=post))
    assert post.call_count == 1
    assert "secret" not in "".join(traceback.format_exception(caught.value))


def test_json_same_response_fallback_has_decoded_size_budget(configured):
    runtime, _ = configured
    runtime["image_jobs"]["result_max_bytes"] = 2
    response = Response(json.dumps({"data": [{"b64_json": "ZmluYWw="}]}).encode(), "application/json")
    post = Mock(return_value=response)
    with pytest.raises(GenerationError, match="大小限制"):
        call(ImageCore(stream_transport=post))
    assert response.closed and post.call_count == 1



def test_missing_bounded_transport_fails_before_credentials_or_network(configured, monkeypatch):
    credentials = Mock(side_effect=AssertionError("credential access"))
    network = Mock(side_effect=AssertionError("network access"))
    monkeypatch.setattr(ImageCore, "_provider_api_key", credentials)
    monkeypatch.setattr(image_service.requests, "post", network)
    with pytest.raises(GenerationError, match="有界可取消传输"):
        call(ImageCore())
    credentials.assert_not_called()
    network.assert_not_called()


@pytest.mark.parametrize("stage", ["open", "read", "close", "cancel"])
def test_unexpected_transport_or_cancel_errors_are_redacted(configured, monkeypatch, stage):
    secret = "private prompt provider body secret"
    def throw():
        raise RuntimeError(secret)
    response = Response(frame(completed()))
    if stage == "read":
        response.iter_content = lambda **_: throw()
    if stage == "close":
        response.close = throw
    transport = Mock(side_effect=RuntimeError(secret)) if stage == "open" else Mock(return_value=response)
    cancel = throw if stage == "cancel" else None
    credentials = Mock(return_value="fake")
    monkeypatch.setattr(ImageCore, "_provider_api_key", credentials)
    with pytest.raises(GenerationError) as caught:
        call(ImageCore(stream_transport=transport), is_cancelled=cancel)
    assert secret not in "".join(traceback.format_exception(caught.value))
    assert transport.call_count <= 1
    if stage == "cancel":
        transport.assert_not_called()
        credentials.assert_not_called()


def test_transport_receives_guarded_cancellation_and_deadline(configured):
    def transport(url, *, deadline, is_cancelled, **options):
        assert deadline > image_service.time.monotonic()
        assert is_cancelled() is False
        assert options["timeout"] <= configured[0]["request"]["timeout_seconds"]
        return Response(frame(completed()))
    assert call(ImageCore(stream_transport=transport)).image_bytes == b"final"


@pytest.mark.parametrize("stage", ["open", "read"])
def test_adapter_generation_error_is_untrusted_and_redacted(configured, stage):
    sensitive = "secret in adapter exception"
    response = Response(frame(completed()))
    def throw(**_):
        raise GenerationError(sensitive)
    if stage == "read":
        response.iter_content = throw
    transport = Mock(side_effect=GenerationError(sensitive)) if stage == "open" else Mock(return_value=response)
    with pytest.raises(GenerationError) as caught:
        call(ImageCore(stream_transport=transport))
    assert sensitive not in "".join(traceback.format_exception(caught.value))
    assert transport.call_count == 1
