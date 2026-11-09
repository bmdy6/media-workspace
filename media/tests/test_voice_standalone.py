"""Optional owner ASR transport tests without microphones or model downloads."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from media import voice_stream_server as voice


class FakeWebSocket:
    def __init__(self, messages=(), header="media-bearer, synthetic-token"):
        self.messages = messages
        self.request = SimpleNamespace(headers={"Sec-WebSocket-Protocol": header})
        self.sent = []
        self.closed = []

    def __aiter__(self):
        async def messages():
            for message in self.messages:
                yield message
        return messages()

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def close(self, **kwargs):
        self.closed.append(kwargs)


def test_bad_voice_token_never_starts_recognizer(monkeypatch):
    async def reject(_token):
        raise ValueError("synthetic rejection")
    monkeypatch.setattr(voice, "_validate_token", reject)
    monkeypatch.setattr(voice, "_load_recognizer", lambda: pytest.fail("unauthenticated recognizer startup"))
    ws = FakeWebSocket()
    asyncio.run(voice.handler(ws))
    assert ws.sent == [] and ws.closed == [{"code": 4401, "reason": "unauthorized"}]


def test_stream_owner_partial_finish_and_invalid_pcm(monkeypatch):
    class Stream:
        def accept_waveform(self, rate, samples):
            assert rate == 16000
        def input_finished(self):
            pass

    engine = SimpleNamespace(create_stream=Stream, is_ready=lambda stream: False,
                             get_result=lambda stream: "合成<unk>转写", is_endpoint=lambda stream: False,
                             reset=lambda stream: None)
    monkeypatch.setattr(voice, "_load_recognizer", lambda: engine)
    async def accept(token):
        assert token == "synthetic-token"
        return "synthetic-owner"
    monkeypatch.setattr(voice, "_validate_token", accept)
    ws = FakeWebSocket([b"x", b"\x00\x00" * 16, '{"type":"finish"}'])
    asyncio.run(voice.handler(ws))
    assert [message["type"] for message in ws.sent] == ["ready", "error", "partial", "final", "done"]
    assert ws.sent[2]["text"] == ws.sent[3]["text"] == "合成转写"
    assert ws.sent[0]["user"] == "synthetic-owner"
    assert "synthetic-token" not in json.dumps(ws.sent)
    assert ws.closed == [{"code": 1000}]


def test_websocket_negotiation_does_not_echo_access_token():
    assert voice._select_subprotocol(None, ["media-bearer", "synthetic-token"]) == "media-bearer"
    assert voice._select_subprotocol(None, ["synthetic-token"]) is None


def test_asr_requires_explicit_model_path(monkeypatch):
    monkeypatch.delenv("VOICE_STREAM_MODEL_DIR", raising=False)
    monkeypatch.setattr(voice, "_load_recognizer", lambda: pytest.fail("implicit host model discovery"))
    with pytest.raises(RuntimeError, match="VOICE_STREAM_MODEL_DIR"):
        asyncio.run(voice.main())
