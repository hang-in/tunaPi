from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import anyio

from tunapi.model import ResumeToken
from tunapi.transport import MessageRef, RenderedMessage


def _make_cfg(
    *,
    startup_msg: str = "bot started",
    transport: AsyncMock | None = None,
    presenter: MagicMock | None = None,
    runtime: MagicMock | None = None,
) -> MagicMock:
    """Build a minimal DiscordBridgeConfig-like mock."""
    cfg = MagicMock()
    cfg.startup_msg = startup_msg

    t = transport or AsyncMock()
    t.send = AsyncMock(return_value=MessageRef(channel_id=1, message_id=99))
    cfg.exec_cfg.transport = t

    p = presenter or MagicMock()
    p.render_progress = MagicMock(
        return_value=RenderedMessage(text="queued...", extra={"show_cancel": False})
    )
    cfg.exec_cfg.presenter = p

    rt = runtime or MagicMock()
    rt.format_context_line = MagicMock(return_value=None)
    cfg.runtime = rt

    return cfg


def _make_running_task(
    *,
    resume: ResumeToken | None = None,
    context=None,
    resume_ready_set: bool = False,
    done_set: bool = False,
) -> MagicMock:
    """Build a minimal RunningTask-like mock with real anyio Events."""
    task = MagicMock()
    task.resume = resume
    task.context = context
    task.resume_ready = anyio.Event()
    task.done = anyio.Event()
    if resume_ready_set:
        task.resume_ready.set()
    if done_set:
        task.done.set()
    return task


class FakeBot:
    def __init__(self, captured: dict | None = None):
        self.captured = captured if captured is not None else {}

    def slash_command(self, **kwargs):
        name = kwargs["name"]

        def decorator(func):
            self.captured[name] = func
            return func

        return decorator


class FakeBotClient:
    def __init__(self, token: str, *, guild_id: int | None = None) -> None:
        self.token = token
        self.guild_id = guild_id


class FakeClock:
    def __init__(self, start: float = 0.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class FakeSleep:
    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


class FakeAttachment:
    def __init__(
        self, *, filename: str, payload: bytes, size: int | None = None
    ) -> None:
        self.filename = filename
        self._payload = payload
        self.size = size if size is not None else len(payload)

    async def read(self) -> bytes:
        return self._payload


class FakeAttachmentOSError(FakeAttachment):
    async def read(self) -> bytes:
        raise OSError("Simulated read error")
