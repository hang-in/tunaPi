"""Msgspec schemas for Gemini CLI stream-json output."""

from __future__ import annotations

from typing import Union

import msgspec

__all__ = [
    "GeminiStreamEvent",
    "GeminiInitEvent",
    "GeminiMessageEvent",
    "GeminiToolUseEvent",
    "GeminiToolResultEvent",
    "GeminiResultEvent",
    "decode_stream_json_line",
]


class GeminiInitEvent(msgspec.Struct, tag="init", tag_field="type", forbid_unknown_fields=False):
    timestamp: str = ""
    session_id: str = ""
    model: str = ""


class GeminiMessageEvent(msgspec.Struct, tag="message", tag_field="type", forbid_unknown_fields=False):
    timestamp: str = ""
    session_id: str = ""
    role: str = ""
    content: str = ""
    delta: bool = False


class GeminiToolUseEvent(msgspec.Struct, tag="tool_use", tag_field="type", forbid_unknown_fields=False):
    timestamp: str = ""
    session_id: str = ""
    tool_name: str = ""
    tool_id: str = ""
    parameters: dict[str, object] = msgspec.field(default_factory=dict)


class GeminiToolResultEvent(msgspec.Struct, tag="tool_result", tag_field="type", forbid_unknown_fields=False):
    timestamp: str = ""
    session_id: str = ""
    tool_id: str = ""
    status: str = ""
    output: str = ""


class GeminiResultStats(msgspec.Struct, forbid_unknown_fields=False):
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    tool_calls: int = 0


class GeminiResultEvent(msgspec.Struct, tag="result", tag_field="type", forbid_unknown_fields=False):
    timestamp: str = ""
    session_id: str = ""
    status: str = ""
    stats: GeminiResultStats = msgspec.field(default_factory=GeminiResultStats)


GeminiStreamEvent = Union[
    GeminiInitEvent,
    GeminiMessageEvent,
    GeminiToolUseEvent,
    GeminiToolResultEvent,
    GeminiResultEvent,
]

_DECODER = msgspec.json.Decoder(GeminiStreamEvent)


def decode_stream_json_line(line: bytes) -> GeminiStreamEvent:
    return _DECODER.decode(line)
