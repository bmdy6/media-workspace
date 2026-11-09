"""Optional authenticated local streaming ASR, using operator-supplied model files."""
from __future__ import annotations
import asyncio
import json
import logging
import math
import os
from pathlib import Path
import re
import time
import numpy as np
import httpx
from websockets.asyncio.server import ServerConnection, serve
SAMPLE_RATE = 16000
MODEL_ROOT = Path(os.environ.get('VOICE_STREAM_MODEL_DIR', './voice-model'))
AUTH_URL = os.environ.get('VOICE_STREAM_AUTH_URL', 'http://127.0.0.1:8000/api/auth/me')
PORT = int(os.environ.get('VOICE_STREAM_PORT', '18766'))
_clock = time.monotonic
recognizer = None

def clean_asr_text(text: str) -> str:
    return re.sub('[ \\t]*(?:<unk>[ \\t]*)+', lambda match: ' ' if any((c in ' \t' for c in match[0])) else '', text, flags=re.IGNORECASE).strip()

def _load_recognizer():
    """Load once, before listening in main; imports remain model-free for tests."""
    global recognizer
    if recognizer is None:
        import sherpa_onnx
        model = next(MODEL_ROOT.rglob('model.int8.onnx'))
        recognizer = sherpa_onnx.OnlineRecognizer.from_zipformer2_ctc(tokens=str(model.parent / 'tokens.txt'), model=str(model), num_threads=2, decoding_method='greedy_search', enable_endpoint_detection=True, rule1_min_trailing_silence=1.0, rule2_min_trailing_silence=0.6, rule3_min_utterance_length=20)
    return recognizer

def _bearer_from_subprotocol(header: str | None) -> str:
    if not header:
        raise ValueError('missing websocket subprotocol')
    parts = [part.strip() for part in header.split(',') if part.strip()]
    if len(parts) < 2 or parts[0] != 'media-bearer':
        raise ValueError('invalid websocket subprotocol')
    return parts[1]

async def _validate_token(token: str) -> str:
    async with httpx.AsyncClient(timeout=3.0) as client:
        response = await client.get(AUTH_URL, headers={'Authorization': f'Bearer {token}'})
    if response.status_code != 200:
        raise ValueError('invalid token')
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError('invalid auth response') from exc
    username = payload.get('username') if isinstance(payload, dict) else None
    if not isinstance(username, str) or not username:
        raise ValueError('invalid token subject')
    return username

async def _send_json(ws: ServerConnection, payload: dict) -> None:
    await ws.send(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))

async def _recognize(ws: ServerConnection, username: str | None) -> None:
    try:
        engine = _load_recognizer()
        stream = engine.create_stream()
        last = ''
        first_audio: float | None = None
        first_partial: float | None = None
        audio_samples = 0
        started = time.perf_counter()
        ready = {'type': 'ready', 'sample_rate': SAMPLE_RATE}
        ready['user'] = username
        await _send_json(ws, ready)

        async def send_final(reason: str) -> None:
            nonlocal last
            text = clean_asr_text(engine.get_result(stream))
            now = time.perf_counter()
            if text:
                await _send_json(ws, {'type': 'final', 'text': text, 'reason': reason, 'audio_ms': round(audio_samples / 16, 1), 'first_partial_ms': round((first_partial - first_audio) * 1000, 1) if first_partial is not None and first_audio is not None else None, 'server_ms': round((now - started) * 1000, 1)})
            engine.reset(stream)
            last = ''
        async for message in _messages(ws):
            if isinstance(message, bytes):
                if len(message) % 2:
                    await _send_json(ws, {'type': 'error', 'detail': 'invalid pcm payload'})
                    continue
                if first_audio is None:
                    first_audio = time.perf_counter()
                pcm = np.frombuffer(message, dtype='<i2').astype(np.float32) / 32768.0
                audio_samples += len(pcm)
                stream.accept_waveform(SAMPLE_RATE, pcm)
                while engine.is_ready(stream):
                    engine.decode_stream(stream)
                text = clean_asr_text(engine.get_result(stream))
                if text and text != last:
                    now = time.perf_counter()
                    if text and first_partial is None:
                        first_partial = now
                    last = text
                    await _send_json(ws, {'type': 'partial', 'text': text, 'audio_ms': round(audio_samples / 16, 1), 'from_first_audio_ms': round((now - first_audio) * 1000, 1) if first_audio is not None else None})
                if text and engine.is_endpoint(stream):
                    await send_final('endpoint')
                    first_partial = None
                    first_audio = time.perf_counter()
            else:
                try:
                    data = json.loads(message)
                    if not isinstance(data, dict):
                        raise ValueError
                except (ValueError, TypeError):
                    await _send_json(ws, {'type': 'error', 'detail': 'invalid control payload'})
                    continue
                if data.get('type') == 'finish':
                    stream.accept_waveform(SAMPLE_RATE, np.zeros(SAMPLE_RATE, dtype=np.float32))
                    stream.input_finished()
                    while engine.is_ready(stream):
                        engine.decode_stream(stream)
                    await send_final('stop')
                    await _send_json(ws, {'type': 'done'})
                    await ws.close(code=1000)
                    return
    except Exception:
        try:
            await ws.close(code=1011, reason='stream failed')
        except Exception:
            pass

class _PrivateWebSocketLogs(logging.Filter):

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING:
            return False
        record.msg = 'voice-stream connection failure'
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True

def _websocket_logger() -> logging.Logger:
    logger = logging.getLogger('voice-stream.websocket')
    logger.setLevel(logging.WARNING)
    if not any((isinstance(value, _PrivateWebSocketLogs) for value in logger.filters)):
        logger.addFilter(_PrivateWebSocketLogs())
    return logger

async def _messages(ws):
    async for message in ws:
        yield message

def _select_subprotocol(_connection, offered):
    return 'media-bearer' if offered and offered[0] == 'media-bearer' else None

async def handler(ws):
    try:
        token = _bearer_from_subprotocol(ws.request.headers.get('Sec-WebSocket-Protocol'))
        username = await _validate_token(token)
    except Exception:
        await ws.close(code=4401, reason='unauthorized')
        return
    await _recognize(ws, username)

async def main():
    if not os.environ.get('VOICE_STREAM_MODEL_DIR'):
        raise RuntimeError('Set VOICE_STREAM_MODEL_DIR to an explicitly installed model directory')
    _load_recognizer()
    async with serve(handler, '127.0.0.1', PORT, max_size=512 * 1024, compression=None, subprotocols=['media-bearer'], select_subprotocol=_select_subprotocol, logger=_websocket_logger()):
        print(f'voice-stream ready on 127.0.0.1:{PORT}', flush=True)
        await asyncio.Future()
if __name__ == '__main__':
    asyncio.run(main())
