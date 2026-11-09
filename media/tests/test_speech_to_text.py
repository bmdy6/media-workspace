from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media.core.speech_to_text import (
    GroqSpeechToTextProvider,
    SpeechToTextError,
    Transcript,
    WhisperCppSpeechToTextProvider,
    transcribe_audio,
)


def _provider(tmp_path):
    binary = tmp_path / "whisper-cli"
    model = tmp_path / "ggml-base-q5_1.bin"
    binary.write_text("binary")
    model.write_text("model")
    return WhisperCppSpeechToTextProvider(
        binary_path=str(binary),
        model_path=str(model),
        timeout_seconds=5,
        threads=2,
    )


def test_whisper_cpp_provider_returns_cli_transcript(tmp_path):
    provider = _provider(tmp_path)
    result = SimpleNamespace(
        returncode=0,
        stdout="我觉得这个语音输入挺有用的，不对，是三点。\n",
        stderr="timings...",
    )

    with (
        patch("media.core.speech_to_text._convert_audio_to_wav") as convert,
        patch("media.core.speech_to_text.subprocess.run", return_value=result) as run,
    ):
        transcript = provider.transcribe(
            b"browser-audio",
            filename="voice.webm",
            content_type="audio/webm",
            language="zh",
        )

    assert transcript == Transcript(
        text="我觉得这个语音输入挺有用的，不对，是三点。",
        language="zh",
    )
    convert.assert_called_once()
    command = run.call_args.args[0]
    assert command[0] == str(provider.binary_path)
    assert command[command.index("-m") + 1] == str(provider.model_path)
    assert command[command.index("-l") + 1] == "zh"
    assert command[command.index("-t") + 1] == "2"


def test_whisper_cpp_provider_rejects_empty_cli_output(tmp_path):
    provider = _provider(tmp_path)
    result = SimpleNamespace(returncode=0, stdout="", stderr="failed to read audio data")

    with (
        patch("media.core.speech_to_text._convert_audio_to_wav"),
        patch("media.core.speech_to_text.subprocess.run", return_value=result),
    ):
        with pytest.raises(SpeechToTextError) as exc:
            provider.transcribe(
                b"browser-audio",
                filename="voice.webm",
                content_type="audio/webm",
            )

    assert exc.value.code == "ASR_LOCAL_FAILED"


def test_transcribe_audio_prefers_local_provider():
    local = MagicMock()
    local.available = True
    local.transcribe.return_value = Transcript(text="本地结果", language="zh")

    with (
        patch(
            "media.core.speech_to_text.WhisperCppSpeechToTextProvider",
            return_value=local,
        ),
        patch("media.core.speech_to_text.GroqSpeechToTextProvider") as groq_cls,
    ):
        result = transcribe_audio(
            b"audio",
            filename="voice.webm",
            content_type="audio/webm",
            language="zh",
        )

    assert result.text == "本地结果"
    groq_cls.assert_not_called()


def test_transcribe_audio_falls_back_to_groq_if_local_fails():
    local = MagicMock()
    local.available = True
    local.transcribe.side_effect = SpeechToTextError(
        "local failed",
        code="ASR_LOCAL_FAILED",
    )
    groq = MagicMock(spec=GroqSpeechToTextProvider)
    groq.transcribe.return_value = Transcript(text="Groq 结果", language="zh")

    with (
        patch(
            "media.core.speech_to_text.WhisperCppSpeechToTextProvider",
            return_value=local,
        ),
        patch(
            "media.core.speech_to_text.GroqSpeechToTextProvider",
            return_value=groq,
        ),
    ):
        result = transcribe_audio(
            b"audio",
            filename="voice.webm",
            content_type="audio/webm",
            language="zh",
        )

    assert result.text == "Groq 结果"


def test_transcribe_audio_hides_provider_details_when_all_fail():
    local = MagicMock()
    local.available = True
    local.transcribe.side_effect = SpeechToTextError(
        "local failed",
        code="ASR_LOCAL_FAILED",
    )
    groq = MagicMock(spec=GroqSpeechToTextProvider)
    groq.transcribe.side_effect = SpeechToTextError(
        "语音转写服务暂时不可用 (HTTP 403)",
        code="ASR_PROVIDER_ERROR",
    )

    with (
        patch(
            "media.core.speech_to_text.WhisperCppSpeechToTextProvider",
            return_value=local,
        ),
        patch(
            "media.core.speech_to_text.GroqSpeechToTextProvider",
            return_value=groq,
        ),
    ):
        with pytest.raises(SpeechToTextError) as exc:
            transcribe_audio(
                b"audio",
                filename="voice.webm",
                content_type="audio/webm",
                language="zh",
            )

    assert exc.value.code == "ASR_ALL_FAILED"
    assert "403" not in exc.value.message
