"""Tests for voice message transcription helpers."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

import tunapi.discord.voice_messages as voice_messages


def test_is_audio_attachment_detects_by_content_type() -> None:
    attachment = MagicMock()
    attachment.content_type = "audio/ogg"
    attachment.filename = "voice.ogg"
    assert voice_messages.is_audio_attachment(attachment) is True


def test_is_audio_attachment_detects_by_extension() -> None:
    attachment = MagicMock()
    attachment.content_type = None
    attachment.filename = "clip.MP3"
    assert voice_messages.is_audio_attachment(attachment) is True


def test_is_audio_attachment_rejects_unknown() -> None:
    attachment = MagicMock()
    attachment.content_type = "image/png"
    attachment.filename = "image.png"
    assert voice_messages.is_audio_attachment(attachment) is False


@dataclass(frozen=True, slots=True)
class _Seg:
    text: str


@pytest.mark.anyio
async def test_transcriber_cleans_whisper_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *, capture_output: bool):
        calls.append(cmd)

        class Result:
            returncode = 0
            stderr = b""

        return Result()

    class DummyModel:
        def __init__(self, model_name: str) -> None:
            self.model_name = model_name

        def transcribe(self, _path: str):
            return [
                _Seg(text="[Silence]"),
                _Seg(text=" hello "),
                _Seg(text="(Music)"),
                _Seg(text="world"),
            ]

    monkeypatch.setattr(voice_messages.subprocess, "run", fake_run)

    transcriber = voice_messages.WhisperAttachmentTranscriber("base")
    transcriber._model = DummyModel("base")
    text = await transcriber.transcribe_bytes(b"123", suffix=".ogg")

    assert text == "hello world"
    assert calls
    assert "ffmpeg" in calls[0][0]
