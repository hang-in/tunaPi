"""Voice message attachment transcription helpers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord
    from pywhispercpp.model import Model as WhisperModel

logger = logging.getLogger("tunapi.discord.voice_messages")

WHISPER_SAMPLE_RATE = 16000

_AUDIO_EXTENSIONS = (
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
)


def is_audio_attachment(attachment: discord.Attachment) -> bool:
    """Return True when the attachment looks like an audio file/voice message."""
    content_type = getattr(attachment, "content_type", None)
    if isinstance(content_type, str) and content_type.startswith("audio/"):
        return True

    filename = getattr(attachment, "filename", None)
    if not isinstance(filename, str) or not filename:
        return False
    suffix = Path(filename).suffix.lower()
    return suffix in _AUDIO_EXTENSIONS


def _combine_whisper_segments(segments: Iterable[object]) -> str:
    segments_list = list(segments)
    if not segments_list:
        return ""

    cleaned_segments: list[str] = []
    for seg in segments_list:
        seg_text = getattr(seg, "text", "")
        if not isinstance(seg_text, str):
            continue
        seg_text = seg_text.strip()
        if not seg_text:
            continue
        # Skip segments that are only artifacts like [Silence] or (BLANK_AUDIO)
        if re.fullmatch(r"[\[\(].*?[\]\)]", seg_text):
            continue
        cleaned_segments.append(seg_text)

    text = " ".join(cleaned_segments)
    text = re.sub(r"\[.*?\]", "", text)  # Remove [bracketed] text
    text = re.sub(r"\(.*?\)", "", text)  # Remove (parenthesized) text
    text = re.sub(r"\s+", " ", text)  # Normalize whitespace
    return text.strip()


class WhisperAttachmentTranscriber:
    """Transcribes audio attachments using local Whisper (pywhispercpp)."""

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model: WhisperModel | None = None  # type: ignore[assignment]
        self._lock = asyncio.Lock()

    def _get_whisper_model(self) -> WhisperModel:  # type: ignore[return]
        if self._model is None:
            try:
                from pywhispercpp.model import Model as WhisperModel
            except ImportError:
                raise ImportError(
                    "pywhispercpp is required for voice transcription. "
                    "Install with: pip install tunapi[discord-voice]"
                ) from None
            logger.info("Loading Whisper model: %s", self._model_name)
            self._model = WhisperModel(self._model_name)
            logger.info("Whisper model loaded")
        return self._model

    def _transcribe_sync(self, payload: bytes, *, suffix: str) -> str:
        model = self._get_whisper_model()

        in_path: Path | None = None
        wav_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
                handle.write(payload)
                in_path = Path(handle.name)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                wav_path = Path(handle.name)

            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(in_path),
                    "-ar",
                    str(WHISPER_SAMPLE_RATE),
                    "-ac",
                    "1",
                    "-f",
                    "wav",
                    str(wav_path),
                ],
                capture_output=True,
            )
            if result.returncode != 0:
                stderr = result.stderr.decode(errors="replace")
                logger.warning("ffmpeg.convert_failed", stderr=stderr[:500])
                return ""

            segments = model.transcribe(str(wav_path))
            return _combine_whisper_segments(segments)
        finally:
            if in_path is not None:
                with contextlib.suppress(OSError):
                    in_path.unlink()
            if wav_path is not None:
                with contextlib.suppress(OSError):
                    wav_path.unlink()

    async def transcribe_bytes(self, payload: bytes, *, suffix: str) -> str:
        """Transcribe audio bytes into text."""
        if not payload:
            return ""
        async with self._lock:
            return await asyncio.to_thread(
                self._transcribe_sync, payload, suffix=suffix
            )
