"""Speech-to-text providers used by Personal Workbench Voice Draft."""
from __future__ import annotations

import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from .config import get_config
from .errors import MediaError


DEFAULT_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_MODEL = "whisper-large-v3"
DEFAULT_TIMEOUT_SECONDS = 90

DEFAULT_WHISPER_CPP_BIN = "/opt/whispercpp/whisper-cli"
DEFAULT_WHISPER_CPP_MODEL = "/opt/whispercpp-models/ggml-base-q5_1.bin"
DEFAULT_WHISPER_CPP_TIMEOUT_SECONDS = 180

_LOCAL_WHISPER_LOCK = threading.Lock()


class SpeechToTextError(MediaError):
    """Raised when audio cannot be transcribed."""

    def __init__(self, message: str, code: str = "ASR_FAILED"):
        super().__init__(message, code=code)


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None = None


class SpeechToTextProvider(Protocol):
    def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        content_type: str,
        language: str | None = None,
    ) -> Transcript:
        ...


class GroqSpeechToTextProvider:
    """Thin Groq Whisper adapter. Secrets stay server-side in explicit environment variables."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_GROQ_MODEL,
        base_url: str | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.model = model
        self.base_url = (
            base_url
            or get_config("GROQ_BASE_URL", default=DEFAULT_GROQ_BASE_URL)
        ).rstrip("/")
        self.timeout_seconds = timeout_seconds

    def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        content_type: str,
        language: str | None = None,
    ) -> Transcript:
        if not audio:
            raise SpeechToTextError("录音内容为空", code="EMPTY_AUDIO")

        api_key = get_config("GROQ_API_KEY", required=True, project="groq")
        data: dict[str, str] = {
            "model": self.model,
            "response_format": "json",
            "temperature": "0",
        }
        if language and language != "auto":
            data["language"] = language

        headers = {"Authorization": f"Bearer {api_key}"}
        files = {"file": (filename, audio, content_type)}
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(
                    f"{self.base_url}/audio/transcriptions",
                    headers=headers,
                    data=data,
                    files=files,
                )
        except httpx.TimeoutException as exc:
            raise SpeechToTextError("语音转写超时，请重试", code="ASR_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise SpeechToTextError("语音转写网络错误，请重试", code="ASR_NETWORK") from exc

        if response.status_code != 200:
            # Provider details belong in diagnostics, not in the user-facing UI.
            raise SpeechToTextError(
                "语音转写暂时不可用，请重试",
                code="ASR_PROVIDER_ERROR",
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise SpeechToTextError("语音转写响应格式无效") from exc

        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise SpeechToTextError("没有识别到可用文字", code="ASR_EMPTY")

        detected_language = payload.get("language") if isinstance(payload, dict) else None
        return Transcript(
            text=text.strip(),
            language=detected_language if isinstance(detected_language, str) else None,
        )


def _safe_audio_suffix(filename: str, content_type: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".wav", ".webm", ".m4a", ".mp4", ".ogg", ".opus", ".mp3", ".aac", ".flac"}:
        return suffix
    return {
        "audio/webm": ".webm",
        "audio/mp4": ".m4a",
        "video/mp4": ".mp4",
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/mpeg": ".mp3",
        "audio/aac": ".aac",
        "audio/flac": ".flac",
    }.get(content_type.lower(), ".audio")


def _convert_audio_to_wav(
    audio: bytes,
    *,
    filename: str,
    content_type: str,
    wav_path: Path,
) -> None:
    """Decode browser audio to 16kHz mono PCM WAV with PyAV.

    Import is intentionally lazy so development/test environments that never use
    the local provider do not fail at module import time.
    """
    try:
        import av
    except ImportError as exc:
        raise SpeechToTextError(
            "本地语音转写组件未安装完整",
            code="ASR_LOCAL_UNAVAILABLE",
        ) from exc

    suffix = _safe_audio_suffix(filename, content_type)
    source_path = wav_path.with_suffix(suffix)
    source_path.write_bytes(audio)

    try:
        with av.open(str(source_path)) as source:
            if not any(stream.type == "audio" for stream in source.streams):
                raise SpeechToTextError("录音中没有可识别的音轨", code="ASR_DECODE_FAILED")

            with av.open(str(wav_path), mode="w", format="wav") as output:
                output_stream = output.add_stream("pcm_s16le", rate=16000)
                output_stream.layout = "mono"
                resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)

                for frame in source.decode(audio=0):
                    for converted in resampler.resample(frame):
                        for packet in output_stream.encode(converted):
                            output.mux(packet)

                for converted in resampler.resample(None):
                    for packet in output_stream.encode(converted):
                        output.mux(packet)

                for packet in output_stream.encode(None):
                    output.mux(packet)
    except SpeechToTextError:
        raise
    except Exception as exc:
        raise SpeechToTextError(
            "录音格式解析失败，请重新录制",
            code="ASR_DECODE_FAILED",
        ) from exc
    finally:
        source_path.unlink(missing_ok=True)


class WhisperCppSpeechToTextProvider:
    """Self-hosted whisper.cpp provider for small CPU-only production hosts."""

    def __init__(
        self,
        *,
        binary_path: str | None = None,
        model_path: str | None = None,
        timeout_seconds: int = DEFAULT_WHISPER_CPP_TIMEOUT_SECONDS,
        threads: int = 2,
    ):
        self.binary_path = Path(
            binary_path
            or get_config("WHISPER_CPP_BIN", default=DEFAULT_WHISPER_CPP_BIN)
        )
        self.model_path = Path(
            model_path
            or get_config("WHISPER_CPP_MODEL", default=DEFAULT_WHISPER_CPP_MODEL)
        )
        self.timeout_seconds = timeout_seconds
        self.threads = max(1, threads)

    @property
    def available(self) -> bool:
        return self.binary_path.is_file() and self.model_path.is_file()

    def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        content_type: str,
        language: str | None = None,
    ) -> Transcript:
        if not audio:
            raise SpeechToTextError("录音内容为空", code="EMPTY_AUDIO")
        if not self.available:
            raise SpeechToTextError(
                "本地语音转写组件不可用",
                code="ASR_LOCAL_UNAVAILABLE",
            )

        requested_language = None if not language or language == "auto" else language

        with tempfile.TemporaryDirectory(prefix="voice-draft-") as temp_dir:
            wav_path = Path(temp_dir) / "audio.wav"
            _convert_audio_to_wav(
                audio,
                filename=filename,
                content_type=content_type,
                wav_path=wav_path,
            )

            command = [
                str(self.binary_path),
                "-m",
                str(self.model_path),
                "-f",
                str(wav_path),
                "-t",
                str(self.threads),
                "-bo",
                "2",
                "-bs",
                "2",
                "-nt",
            ]
            if requested_language:
                command.extend(["-l", requested_language])

            try:
                # The host only has 2 vCPUs. Serializing local Whisper prevents two
                # simultaneous requests from starving media-api or exhausting RAM.
                with _LOCAL_WHISPER_LOCK:
                    result = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        timeout=self.timeout_seconds,
                        check=False,
                    )
            except subprocess.TimeoutExpired as exc:
                raise SpeechToTextError(
                    "本地语音转写超时，请重试",
                    code="ASR_LOCAL_TIMEOUT",
                ) from exc
            except OSError as exc:
                raise SpeechToTextError(
                    "本地语音转写组件不可用",
                    code="ASR_LOCAL_UNAVAILABLE",
                ) from exc

        text = result.stdout.strip()
        if result.returncode != 0 or not text:
            raise SpeechToTextError(
                "本地语音转写失败，请重试",
                code="ASR_LOCAL_FAILED",
            )

        return Transcript(text=text, language=requested_language)


def transcribe_audio(
    audio: bytes,
    *,
    filename: str,
    content_type: str,
    language: str | None = None,
) -> Transcript:
    """Prefer self-hosted Whisper; fall back to Groq when local ASR is unavailable.

    Browser SpeechRecognition remains a frontend-only final fallback when both
    server providers fail.
    """
    local = WhisperCppSpeechToTextProvider()
    local_error: SpeechToTextError | None = None

    if local.available:
        try:
            return local.transcribe(
                audio,
                filename=filename,
                content_type=content_type,
                language=language,
            )
        except SpeechToTextError as exc:
            local_error = exc

    try:
        return GroqSpeechToTextProvider().transcribe(
            audio,
            filename=filename,
            content_type=content_type,
            language=language,
        )
    except (SpeechToTextError, MediaError) as exc:
        if local_error is not None:
            raise SpeechToTextError(
                "语音转写暂时不可用，请重试",
                code="ASR_ALL_FAILED",
            ) from exc
        raise
