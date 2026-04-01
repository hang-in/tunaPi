"""Tests for engine slash command session behavior."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

from tunapi.model import ResumeToken
from tunapi.transport import MessageRef
from tunapi.discord.overrides import ResolvedOverrides


class DummyThread:
    """Minimal stand-in for discord.Thread for unit tests."""

    def __init__(self, *, parent_id: int | None) -> None:
        self.parent_id = parent_id


@pytest.mark.anyio
async def test_engine_command_restores_and_saves_session_in_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tunapi.discord.handlers as handlers

    # Make isinstance(ctx.channel, discord.Thread) work without real discord objects.
    monkeypatch.setattr(handlers.discord, "Thread", DummyThread)

    ctx = MagicMock()
    ctx.guild = MagicMock()
    ctx.guild.id = 123
    ctx.channel_id = 555  # thread id
    ctx.channel = DummyThread(parent_id=999)  # parent channel id
    ctx.author = MagicMock()
    ctx.author.id = 4242
    ctx.defer = AsyncMock()
    ctx.followup = MagicMock()
    ctx.followup.send = AsyncMock()

    state_store = MagicMock()
    state_store.get_context = AsyncMock(return_value=None)
    state_store.get_session = AsyncMock(return_value="tok123")
    state_store.set_session = AsyncMock()

    prefs_store = MagicMock()

    # Starter message mock
    starter_ref = MessageRef(channel_id=555, message_id=777, thread_id=555)
    cfg = MagicMock()
    cfg.exec_cfg = MagicMock()
    cfg.runtime = MagicMock()
    cfg.show_resume_line = True
    cfg.allowed_user_ids = None
    cfg.session_mode = "chat"
    cfg.bot.send_message = AsyncMock(return_value=starter_ref)

    run_engine = AsyncMock()

    with (
        patch(
            "tunapi.discord.handlers.resolve_overrides",
            new=AsyncMock(return_value=ResolvedOverrides()),
        ),
        patch("tunapi.discord.commands.executor._run_engine", new=run_engine),
    ):
        await handlers._handle_engine_command(
            ctx,
            engine_id="codex",
            prompt="hello",
            cfg=cfg,
            state_store=state_store,
            prefs_store=prefs_store,
            running_tasks={},
        )

        # Let the background task run
        await asyncio.sleep(0)

        state_store.get_session.assert_awaited_once_with(
            123, 555, "codex", author_id=4242
        )
        run_engine.assert_awaited_once()

        kwargs = run_engine.call_args.kwargs
        assert kwargs["channel_id"] == 555
        assert kwargs["thread_id"] == 555
        assert kwargs["engine_override"] == "codex"
        assert kwargs["resume_token"] == ResumeToken(engine="codex", value="tok123")
        assert kwargs["on_thread_known"] is not None

        on_thread_known = kwargs["on_thread_known"]
        await on_thread_known(
            ResumeToken(engine="codex", value="tok456"), anyio.Event()
        )
        state_store.set_session.assert_awaited_with(
            123, 555, "codex", "tok456", author_id=4242
        )
