"""Tests for attachment media-group buffering."""

from __future__ import annotations

from unittest.mock import MagicMock

import anyio
import pytest

from tunapi.discord.loop_state import MediaGroupBuffer


class _ControlledSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []
        self.events: list[anyio.Event] = []

    async def __call__(self, duration: float) -> None:
        self.calls.append(duration)
        event = anyio.Event()
        self.events.append(event)
        await event.wait()


@pytest.mark.anyio
async def test_media_group_buffer_coalesces_multiple_adds() -> None:
    dispatched: list[object] = []
    dispatched_event = anyio.Event()

    async def dispatch(state) -> None:
        dispatched.append(state)
        dispatched_event.set()

    sleep = _ControlledSleep()

    async with anyio.create_task_group() as tg:
        buffer = MediaGroupBuffer(
            task_group=tg,
            debounce_s=0.75,
            dispatch=dispatch,
            sleep=sleep,
        )

        msg1 = MagicMock()
        msg1.author = MagicMock()
        msg1.author.id = 123
        buffer.add(
            msg1,
            prompt="",
            guild_id=1,
            channel_id=2,
            thread_id=3,
            job_channel_id=3,
            engine_id="claude",
            resume_token=None,
            context=None,
        )

        while not sleep.events:
            await anyio.sleep(0)

        msg2 = MagicMock()
        msg2.author = MagicMock()
        msg2.author.id = 123
        buffer.add(
            msg2,
            prompt="caption",
            guild_id=1,
            channel_id=2,
            thread_id=3,
            job_channel_id=3,
            engine_id="claude",
            resume_token=None,
            context=None,
        )

        sleep.events[0].set()
        while len(sleep.events) < 2:
            await anyio.sleep(0)
        sleep.events[1].set()

        await dispatched_event.wait()

    assert len(dispatched) == 1
    state = dispatched[0]
    assert len(state.items) == 2
