"""Tests for the pure-logic helpers in discord/voice.py.

The module imports the optional ``pywhispercpp`` native dependency lazily,
so it can be imported (and its audio math exercised) without that package
installed. These tests cover AudioBuffer energy/silence detection and the
PCM->WAV header construction — no Discord/voice runtime required.
"""

from __future__ import annotations

import struct
import time
from typing import Any

from tunapi.discord.voice import (
    CHANNELS,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    SILENCE_AMPLITUDE_THRESHOLD,
    AudioBuffer,
    VoiceManager,
)


def _pcm(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


class TestAudioBufferRms:
    def test_silence_chunk_has_zero_rms(self):
        buf = AudioBuffer(user_id=1)
        assert buf._calculate_rms(_pcm([0] * 100)) == 0.0

    def test_loud_chunk_has_high_rms(self):
        buf = AudioBuffer(user_id=1)
        rms = buf._calculate_rms(_pcm([10000] * 100))
        assert rms == 10000.0
        assert rms > SILENCE_AMPLITUDE_THRESHOLD

    def test_too_short_chunk_returns_zero(self):
        buf = AudioBuffer(user_id=1)
        assert buf._calculate_rms(b"\x00") == 0.0


class TestAudioBufferSpeech:
    def test_loud_chunk_marks_speaking(self):
        buf = AudioBuffer(user_id=1)
        buf.add_chunk(_pcm([10000] * 100))
        assert buf.is_speaking is True
        assert buf.silence_start_time == 0.0

    def test_silence_after_speech_marks_silence_start(self):
        buf = AudioBuffer(user_id=1)
        buf.add_chunk(_pcm([10000] * 100))  # speech
        buf.add_chunk(_pcm([0] * 100))  # silence
        assert buf.is_speaking is True
        assert buf.silence_start_time > 0.0

    def test_get_audio_and_clear_resets_state(self):
        buf = AudioBuffer(user_id=1)
        buf.add_chunk(_pcm([10000] * 10))
        buf.add_chunk(_pcm([10000] * 10))
        audio = buf.get_audio_and_clear()
        assert audio == _pcm([10000] * 10) + _pcm([10000] * 10)
        assert buf.chunks == []
        assert buf.is_speaking is False
        assert buf.silence_start_time == 0.0


class TestAudioBufferSilenceDetection:
    def test_no_chunks_is_not_silence(self):
        assert AudioBuffer(user_id=1).is_silence_detected() is False

    def test_not_speaking_is_not_silence(self):
        buf = AudioBuffer(user_id=1)
        buf.chunks.append(_pcm([0] * 10))
        buf.is_speaking = False
        assert buf.is_silence_detected() is False

    def test_silence_past_threshold_is_detected(self):
        buf = AudioBuffer(user_id=1, silence_threshold_ms=300)
        buf.chunks.append(_pcm([10000] * 10))
        buf.is_speaking = True
        # Silence started well beyond the threshold ago.
        buf.silence_start_time = time.monotonic() - 1.0
        assert buf.is_silence_detected() is True

    def test_chunk_gap_past_threshold_is_detected(self):
        buf = AudioBuffer(user_id=1, silence_threshold_ms=300)
        buf.chunks.append(_pcm([10000] * 10))
        buf.is_speaking = True
        buf.silence_start_time = 0.0  # no in-stream silence marker
        buf.last_chunk_time = time.monotonic() - 1.0  # push-to-talk gap
        assert buf.is_silence_detected() is True


class TestAudioBufferDuration:
    def test_duration_matches_pcm_rate(self):
        buf = AudioBuffer(user_id=1)
        one_second = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH  # bytes for 1s
        buf.chunks.append(b"\x00" * one_second)
        assert buf.duration_ms() == 1000.0

    def test_empty_duration_is_zero(self):
        assert AudioBuffer(user_id=1).duration_ms() == 0.0


class TestPcmToWav:
    def test_wav_header_and_size(self):
        pcm = _pcm([1, 2, 3, 4] * 25)  # 200 bytes
        wav = VoiceManager._pcm_to_wav(_dummy_self(), pcm)
        assert wav[:4] == b"RIFF"
        assert wav[8:12] == b"WAVE"
        assert b"fmt " in wav
        assert b"data" in wav
        # 44-byte canonical PCM WAV header + payload.
        assert len(wav) == 44 + len(pcm)
        assert wav.endswith(pcm)


def _dummy_self() -> Any:
    """_pcm_to_wav does not touch self; a placeholder is enough."""
    return object()
