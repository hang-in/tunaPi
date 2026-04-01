"""Voice chat support for Discord transport using Pycord's native recording."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
import subprocess
import tempfile
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord
from pywhispercpp.model import Model as WhisperModel

from .allowlist import is_user_allowed

if TYPE_CHECKING:
    from openai import AsyncOpenAI

    from .client import DiscordBotClient

logger = logging.getLogger("tunapi.discord.voice")

# Audio processing constants
SILENCE_THRESHOLD_MS = 300  # Time of silence before processing (0.3 seconds)
MIN_AUDIO_DURATION_MS = 500  # Minimum audio duration to process (0.5 seconds)
SAMPLE_RATE = 48000  # Discord uses 48kHz
CHANNELS = 2  # Stereo
SAMPLE_WIDTH = 2  # 16-bit PCM
SILENCE_AMPLITUDE_THRESHOLD = 500  # RMS amplitude below this is considered silence

# Whisper STT constants
WHISPER_MODEL = "base"  # Options: tiny, base, small, medium, large-v3
WHISPER_SAMPLE_RATE = 16000  # Whisper expects 16kHz mono audio


@dataclass
class VoiceSession:
    """Tracks an active voice session."""

    guild_id: int
    voice_channel_id: int
    text_channel_id: int
    voice_client: discord.VoiceClient
    project: str
    branch: str
    delete_on_leave: bool = True  # Delete the voice channel when leaving


@dataclass
class AudioBuffer:
    """Buffers audio chunks and detects speech pauses based on audio energy."""

    user_id: int
    chunks: list[bytes] = field(default_factory=list)
    last_voice_time: float = 0.0  # Last time we received actual speech (not silence)
    last_chunk_time: float = 0.0  # Last time we received any chunk (for push-to-talk)
    silence_start_time: float = 0.0  # When silence started
    is_speaking: bool = False
    silence_threshold_ms: int = SILENCE_THRESHOLD_MS

    def _calculate_rms(self, chunk: bytes) -> float:
        """Calculate RMS (root mean square) amplitude of audio chunk."""
        if len(chunk) < 2:
            return 0.0
        # Convert bytes to 16-bit samples
        samples = struct.unpack(f"<{len(chunk) // 2}h", chunk)
        if not samples:
            return 0.0
        # Calculate RMS
        sum_squares = sum(s * s for s in samples)
        return (sum_squares / len(samples)) ** 0.5

    def add_chunk(self, chunk: bytes) -> None:
        """Add an audio chunk to the buffer."""
        self.chunks.append(chunk)
        now = time.monotonic()
        self.last_chunk_time = now

        # Check if this chunk contains actual speech or silence
        rms = self._calculate_rms(chunk)

        if rms > SILENCE_AMPLITUDE_THRESHOLD:
            # User is speaking
            self.last_voice_time = now
            self.is_speaking = True
            self.silence_start_time = 0.0
        elif self.is_speaking and self.silence_start_time == 0.0:
            # Just went silent - mark the start of silence
            self.silence_start_time = now

    def is_silence_detected(self) -> bool:
        """Check if user stopped speaking (silence after speech or push-to-talk release)."""
        if not self.chunks or not self.is_speaking:
            return False

        now = time.monotonic()

        # Check for silence in the audio stream (voice activity detection)
        if self.silence_start_time > 0.0:
            elapsed_ms = (now - self.silence_start_time) * 1000
            if elapsed_ms >= self.silence_threshold_ms:
                return True

        # Check for push-to-talk: no new chunks received for threshold duration
        # This handles the case where audio stream stops entirely
        if self.last_chunk_time > 0.0:
            chunk_gap_ms = (now - self.last_chunk_time) * 1000
            if chunk_gap_ms >= self.silence_threshold_ms:
                return True

        return False

    def get_audio_and_clear(self) -> bytes:
        """Get all buffered audio and clear the buffer."""
        audio = b"".join(self.chunks)
        self.chunks.clear()
        self.is_speaking = False
        self.silence_start_time = 0.0
        return audio

    def duration_ms(self) -> float:
        """Calculate approximate duration of buffered audio in ms."""
        total_bytes = sum(len(c) for c in self.chunks)
        # PCM: bytes / (sample_rate * channels * sample_width) * 1000
        return (total_bytes / (SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH)) * 1000


VoiceMessageHandler = Callable[
    [
        int,
        int,
        str,
        str,
        str,
        str,
    ],  # guild_id, text_channel_id, transcript, user_name, project, branch
    Coroutine[Any, Any, str | None],  # Returns response text or None
]


class StreamingSink(discord.sinks.Sink):
    """Custom sink that captures audio per-user for real-time processing."""

    def __init__(self, callback: Callable[[int, bytes], None]) -> None:
        super().__init__()
        self._callback = callback

    def write(self, data: bytes, user: int) -> None:
        """Called when audio data is received from a user."""
        self._callback(user, data)

    def cleanup(self) -> None:
        """Clean up resources."""
        pass


class VoiceManager:
    """Manages voice connections and audio processing."""

    def __init__(
        self,
        bot: DiscordBotClient,
        openai_client: AsyncOpenAI,
        *,
        tts_voice: str = "nova",
        tts_model: str = "tts-1",
        whisper_model: str = WHISPER_MODEL,
        allowed_user_ids: frozenset[int] | None = None,
    ) -> None:
        self._bot = bot
        self._openai = openai_client
        self._tts_voice = tts_voice
        self._tts_model = tts_model
        self._whisper_model_name = whisper_model
        self._whisper_model: WhisperModel | None = None  # Lazy init
        self._allowed_user_ids = allowed_user_ids
        self._sessions: dict[int, VoiceSession] = {}  # guild_id -> session
        self._audio_buffers: dict[
            tuple[int, int], AudioBuffer
        ] = {}  # (guild_id, user_id) -> buffer
        self._processing_lock = asyncio.Lock()
        self._silence_check_task: asyncio.Task[None] | None = None
        self._message_handler: VoiceMessageHandler | None = None
        self._last_process_time: dict[int, float] = {}  # guild_id -> timestamp
        self._process_cooldown_s = 1.0  # Minimum seconds between processing
        self._is_responding: dict[int, bool] = {}  # guild_id -> is currently responding

    def _get_whisper_model(self) -> WhisperModel:
        """Lazy-load Whisper model."""
        if self._whisper_model is None:
            logger.info("Loading Whisper model: %s", self._whisper_model_name)
            self._whisper_model = WhisperModel(self._whisper_model_name)
            logger.info("Whisper model loaded")
        return self._whisper_model

    def set_message_handler(self, handler: VoiceMessageHandler) -> None:
        """Set the handler for processing transcribed voice messages."""
        self._message_handler = handler

    @property
    def sessions(self) -> dict[int, VoiceSession]:
        """Get active voice sessions."""
        return self._sessions

    def is_connected(self, guild_id: int) -> bool:
        """Check if the bot is connected to a voice channel in the guild."""
        session = self._sessions.get(guild_id)
        return session is not None and session.voice_client.is_connected()

    def get_session(self, guild_id: int) -> VoiceSession | None:
        """Get the voice session for a guild."""
        return self._sessions.get(guild_id)

    def _receive_audio(self, guild_id: int, user_id: int, data: bytes) -> None:
        """Receive audio data from a user (called from sink)."""
        if not is_user_allowed(self._allowed_user_ids, user_id):
            return
        # Don't buffer audio while bot is responding - discard it
        if self._is_responding.get(guild_id, False):
            return

        key = (guild_id, user_id)
        if key not in self._audio_buffers:
            self._audio_buffers[key] = AudioBuffer(user_id=user_id)
            logger.info(
                "Started receiving audio from user %s in guild %s", user_id, guild_id
            )
        self._audio_buffers[key].add_chunk(data)

    async def join_channel(
        self,
        voice_channel: discord.VoiceChannel,
        text_channel_id: int,
        project: str,
        branch: str,
    ) -> VoiceSession:
        """Join a voice channel and start listening."""
        guild_id = voice_channel.guild.id

        # Disconnect from existing session if any
        if guild_id in self._sessions:
            await self.leave_channel(guild_id)

        # Connect to the voice channel
        voice_client = await voice_channel.connect()
        logger.info("Connected to voice channel %s", voice_channel.id)

        # Create session
        session = VoiceSession(
            guild_id=guild_id,
            voice_channel_id=voice_channel.id,
            text_channel_id=text_channel_id,
            voice_client=voice_client,
            project=project,
            branch=branch,
        )
        self._sessions[guild_id] = session

        # Create a sink that forwards audio to our buffer
        def on_audio(user_id: int, data: bytes) -> None:
            self._receive_audio(guild_id, user_id, data)

        sink = StreamingSink(on_audio)

        # Callback when recording stops
        async def on_recording_stop(sink: discord.sinks.Sink, *args: Any) -> None:
            logger.info("Recording stopped for guild %s", guild_id)

        # Start recording
        voice_client.start_recording(sink, on_recording_stop)
        logger.info("Started recording on voice channel %s", voice_channel.id)

        # Start silence detection task if not running
        if self._silence_check_task is None or self._silence_check_task.done():
            self._silence_check_task = asyncio.create_task(self._silence_check_loop())

        logger.info(
            "Joined voice channel %s in guild %s, linked to text channel %s",
            voice_channel.id,
            guild_id,
            text_channel_id,
        )
        return session

    async def leave_channel(self, guild_id: int) -> None:
        """Leave the voice channel in a guild and optionally delete it."""
        session = self._sessions.pop(guild_id, None)
        if session is None:
            return

        voice_channel_id = session.voice_channel_id
        should_delete = session.delete_on_leave

        # Stop recording if active
        if session.voice_client.recording:
            session.voice_client.stop_recording()

        # Disconnect
        await session.voice_client.disconnect()

        # Clear audio buffers for this guild
        keys_to_remove = [k for k in self._audio_buffers if k[0] == guild_id]
        for key in keys_to_remove:
            del self._audio_buffers[key]

        # Delete the voice channel if it was bot-created
        if should_delete:
            voice_channel = self._bot.bot.get_channel(voice_channel_id)
            if voice_channel is not None:
                with contextlib.suppress(discord.HTTPException):
                    await voice_channel.delete(reason="Voice session ended")
                    logger.info(
                        "Deleted voice channel %s in guild %s",
                        voice_channel_id,
                        guild_id,
                    )

        logger.info("Left voice channel in guild %s", guild_id)

    async def _silence_check_loop(self) -> None:
        """Periodically check for silence and process audio."""
        while self._sessions:
            await asyncio.sleep(0.05)  # Check every 50ms for faster response

            buffers_to_process: list[tuple[int, int, AudioBuffer]] = []

            for (guild_id, user_id), buffer in list(self._audio_buffers.items()):
                if (
                    buffer.is_silence_detected()
                    and buffer.duration_ms() >= MIN_AUDIO_DURATION_MS
                ):
                    buffers_to_process.append((guild_id, user_id, buffer))

            for guild_id, user_id, buffer in buffers_to_process:
                audio = buffer.get_audio_and_clear()
                logger.info(
                    "Processing audio from user %s: %d bytes", user_id, len(audio)
                )
                asyncio.create_task(self._process_audio(guild_id, user_id, audio))

    async def _process_audio(self, guild_id: int, user_id: int, audio: bytes) -> None:
        """Process captured audio: transcribe and handle."""
        session = self._sessions.get(guild_id)
        if session is None:
            return

        # Skip if already responding (shouldn't happen but just in case)
        if self._is_responding.get(guild_id, False):
            logger.debug("Skipping audio processing - already responding")
            return

        # Check cooldown to prevent rapid-fire processing
        now = time.monotonic()
        last_time = self._last_process_time.get(guild_id, 0)
        if now - last_time < self._process_cooldown_s:
            logger.debug("Skipping audio processing - cooldown active")
            return

        async with self._processing_lock:
            try:
                # Mark as responding - stop buffering new audio
                self._is_responding[guild_id] = True

                # Clear any buffered audio that came in before we set the flag
                keys_to_clear = [k for k in self._audio_buffers if k[0] == guild_id]
                for key in keys_to_clear:
                    self._audio_buffers[key].get_audio_and_clear()

                # Update last process time
                self._last_process_time[guild_id] = time.monotonic()

                # Get user info
                guild = self._bot.bot.get_guild(guild_id)
                member = guild.get_member(user_id) if guild else None
                user_name = member.display_name if member else f"User {user_id}"

                # Transcribe audio
                transcript = await self.transcribe(audio)
                if not transcript or not transcript.strip():
                    logger.debug("Empty transcript, skipping")
                    return

                # Skip very short transcripts (likely noise or fragments)
                words = transcript.strip().split()
                if len(words) < 1:
                    logger.debug(
                        "Transcript too short (%d words), skipping: %s",
                        len(words),
                        transcript,
                    )
                    return

                logger.info("Transcribed from %s: %s", user_name, transcript)

                # Say acknowledgment so user knows we heard them
                await self.speak(guild_id, "Hmm, let me think about that.")

                # Call the message handler
                if self._message_handler is not None:
                    response = await self._message_handler(
                        guild_id,
                        session.text_channel_id,
                        transcript,
                        user_name,
                        session.project,
                        session.branch,
                    )

                    # Synthesize and play the final response if we got one
                    if response:
                        await self.speak(guild_id, response)

            except Exception:
                logger.exception("Error processing voice audio")

            finally:
                # Done responding - start listening again
                self._is_responding[guild_id] = False

    def _transcribe_sync(self, wav_bytes: bytes) -> str:
        """Synchronous transcription using local Whisper (runs in thread pool)."""
        model = self._get_whisper_model()

        # Convert 48kHz stereo WAV to 16kHz mono for Whisper
        resampled_wav = self._resample_for_whisper(wav_bytes)
        logger.info(
            "Resampled audio: %d bytes -> %d bytes", len(wav_bytes), len(resampled_wav)
        )

        # Write WAV to temp file (pywhispercpp needs a file path)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(resampled_wav)
            temp_path = Path(f.name)

        try:
            # Transcribe
            segments = model.transcribe(str(temp_path))
            # Convert to list to check contents
            segments_list = list(segments)
            logger.info("Got %d segments from Whisper", len(segments_list))
            for i, seg in enumerate(segments_list):
                logger.info("Segment %d: '%s'", i, seg.text)
            # Combine all segments, filtering out artifact-only segments
            import re

            cleaned_segments = []
            for seg in segments_list:
                seg_text = seg.text.strip()
                # Skip segments that are only artifacts
                if re.fullmatch(r"[\[\(].*?[\]\)]", seg_text):
                    continue
                cleaned_segments.append(seg_text)

            text = " ".join(cleaned_segments)
            # Remove remaining Whisper artifacts like [Silence], [Music], [BLANK_AUDIO], etc.
            text = re.sub(r"\[.*?\]", "", text)  # Remove [bracketed] text
            text = re.sub(r"\(.*?\)", "", text)  # Remove (parenthesized) text
            text = re.sub(r"\s+", " ", text)  # Normalize whitespace
            text = text.strip()
            logger.info("Combined text: '%s'", text)
            return text
        finally:
            # Clean up
            with contextlib.suppress(OSError):
                temp_path.unlink()

    def _resample_for_whisper(self, wav_bytes: bytes) -> bytes:
        """Resample WAV from 48kHz stereo to 16kHz mono for Whisper."""
        # Use FFmpeg to resample
        result = subprocess.run(
            [
                "ffmpeg",
                "-f",
                "wav",
                "-i",
                "pipe:0",
                "-ar",
                str(WHISPER_SAMPLE_RATE),  # 16kHz
                "-ac",
                "1",  # Mono
                "-f",
                "wav",
                "pipe:1",
            ],
            input=wav_bytes,
            capture_output=True,
        )

        if result.returncode != 0:
            logger.error("FFmpeg resample failed: %s", result.stderr.decode())
            return wav_bytes  # Return original as fallback

        return result.stdout

    async def transcribe(self, audio: bytes) -> str:
        """Transcribe audio bytes to text using local Whisper."""
        # Convert raw PCM to WAV format for Whisper
        wav_bytes = self._pcm_to_wav(audio)

        try:
            logger.info("Transcribing audio with local Whisper...")
            start_time = time.monotonic()
            # Run CPU-bound transcription in thread pool
            text = await asyncio.to_thread(self._transcribe_sync, wav_bytes)
            elapsed_ms = (time.monotonic() - start_time) * 1000
            logger.info(
                "Transcription complete in %.0fms: %s",
                elapsed_ms,
                text[:50] if text else "(empty)",
            )
            return text
        except Exception:
            logger.exception("Error transcribing audio")
            return ""

    async def synthesize(self, text: str) -> bytes:
        """Synthesize text to speech using OpenAI TTS."""
        try:
            logger.info("Synthesizing TTS for text: %s...", text[:50])
            response = await self._openai.audio.speech.create(
                model=self._tts_model,
                voice=self._tts_voice,
                input=text,
                response_format="opus",
            )
            logger.info("TTS synthesis complete, %d bytes", len(response.content))
            return response.content
        except Exception:
            logger.exception("Error synthesizing speech")
            return b""

    async def speak(self, guild_id: int, text: str) -> None:
        """Synthesize and play text in the voice channel."""
        session = self._sessions.get(guild_id)
        if session is None:
            logger.warning("speak: No session for guild %s", guild_id)
            return
        if not session.voice_client.is_connected():
            logger.warning("speak: Voice client not connected for guild %s", guild_id)
            return

        logger.info("speak: Starting TTS for guild %s", guild_id)

        # Synthesize speech
        audio = await self.synthesize(text)
        if not audio:
            logger.warning("speak: No audio data from synthesis")
            return

        # Write to temp file for FFmpeg
        with tempfile.NamedTemporaryFile(suffix=".opus", delete=False) as f:
            f.write(audio)
            temp_path = Path(f.name)

        logger.info("speak: Wrote %d bytes to %s", len(audio), temp_path)

        try:
            # Wait if already playing
            while session.voice_client.is_playing():
                await asyncio.sleep(0.1)

            # Play the audio
            logger.info("speak: Creating FFmpegOpusAudio source")
            source = discord.FFmpegOpusAudio(str(temp_path))
            logger.info("speak: Playing audio")
            session.voice_client.play(source)

            # Wait for playback to finish
            while session.voice_client.is_playing():
                await asyncio.sleep(0.1)

            logger.info("speak: Playback complete")

        except Exception:
            logger.exception("speak: Error during playback")

        finally:
            # Clean up temp file
            with contextlib.suppress(OSError):
                temp_path.unlink()

    def _pcm_to_wav(self, pcm_data: bytes) -> bytes:
        """Convert raw PCM data to WAV format."""
        # WAV header
        data_size = len(pcm_data)
        file_size = data_size + 36

        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            file_size,
            b"WAVE",
            b"fmt ",
            16,  # Subchunk1Size (PCM)
            1,  # AudioFormat (PCM)
            CHANNELS,
            SAMPLE_RATE,
            SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH,  # ByteRate
            CHANNELS * SAMPLE_WIDTH,  # BlockAlign
            SAMPLE_WIDTH * 8,  # BitsPerSample
            b"data",
            data_size,
        )

        return header + pcm_data

    async def handle_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Handle voice state updates (users joining/leaving)."""
        guild_id = member.guild.id
        session = self._sessions.get(guild_id)
        if session is None:
            return

        logger.debug(
            "Voice state update: member=%s, before=%s, after=%s, session_channel=%s",
            member.name,
            before.channel.id if before.channel else None,
            after.channel.id if after.channel else None,
            session.voice_channel_id,
        )

        # Check if the bot was disconnected
        bot_user = self._bot.user
        bot_user_id = bot_user.id if bot_user else None
        if (
            member.id == bot_user_id
            and after.channel is None
            and before.channel is not None
        ):
            # Bot was disconnected
            logger.info("Bot was disconnected from voice in guild %s", guild_id)
            self._sessions.pop(guild_id, None)
            return

        # Check if someone left our voice channel (not the bot itself)
        if (
            member.id != bot_user_id
            and before.channel is not None
            and before.channel.id == session.voice_channel_id
            and (after.channel is None or after.channel.id != session.voice_channel_id)
        ):
            # Someone left our channel, check if it's empty
            # Use before.channel directly since it's the channel they left
            voice_channel = before.channel
            if isinstance(voice_channel, discord.VoiceChannel):
                # Count non-bot members remaining in the channel
                human_members = [m for m in voice_channel.members if not m.bot]
                logger.debug(
                    "Channel %s has %d human members remaining: %s",
                    voice_channel.id,
                    len(human_members),
                    [m.name for m in human_members],
                )
                if not human_members:
                    logger.info("Voice channel empty, leaving guild %s", guild_id)
                    await self.leave_channel(guild_id)
