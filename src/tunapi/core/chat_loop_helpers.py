from __future__ import annotations

import contextlib
import time
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..journal import JournalEntry
from ..logging import get_logger
from ..transport import RenderedMessage
from . import files
from .roundtable import RoundtableSession, RoundtableStore, run_roundtable

logger = get_logger(__name__)

_PERSONA_PREFIX_RE = re.compile(r"^@(\w+)\s+", re.UNICODE)


def render_file_put_results(results: list[Any]) -> str:
    return (
        "\n".join(f"- {result.message}" for result in results)
        if results
        else "No files processed."
    )


def render_saved_file_context(results: list[Any]) -> str:
    saved_paths = [str(result.path) for result in results if result.ok and result.path]
    if not saved_paths:
        return ""
    paths_str = ", ".join(f"`{path}`" for path in saved_paths)
    return f"\n[Attached files saved to: {paths_str}]\n"


def resolve_upload_dir(runtime: Any, channel_id: str) -> Path:
    """Resolve the upload target directory for a channel-bound project."""
    context = runtime.default_context_for_chat(channel_id)
    project = context.project if context else None
    return files.resolve_incoming_dir(project or "default")


async def send_to_channel(cfg: Any, channel_id: str, message: RenderedMessage) -> None:
    await cfg.exec_cfg.transport.send(channel_id=channel_id, message=message)


async def handle_cancel_reaction_by_message_id(
    *,
    emoji: str,
    cancel_emoji: str,
    message_id: str,
    user_id: str,
    running_tasks: Any,
    roundtables: RoundtableStore | None,
    transport_log_event: str,
    message_id_log_key: str,
) -> None:
    if emoji != cancel_emoji:
        return

    if roundtables:
        session = roundtables.get(message_id)
        if session is not None:
            logger.info(
                "roundtable.cancel_by_reaction",
                thread_id=session.thread_id,
                user_id=user_id,
            )
            session.cancel_event.set()
            return

    for ref, task in list(running_tasks.items()):
        if str(ref.message_id) == message_id:
            logger.info(
                transport_log_event,
                **{message_id_log_key: message_id, "user_id": user_id},
            )
            task.cancel_requested.set()
            return


async def resolve_persona_prefix(prompt: str, chat_prefs: Any) -> str | None:
    """If prompt starts with @persona_name, prepend the persona prompt."""
    match = _PERSONA_PREFIX_RE.match(prompt)
    if not match:
        return None
    name = match.group(1).lower()
    persona = await chat_prefs.get_persona(name)
    if persona is None:
        return None
    user_text = prompt[match.end() :]
    return f"[역할: {persona.name}]\n{persona.prompt}\n\n---\n\n{user_text}"


async def archive_roundtable_thread(
    session: RoundtableSession,
    journal: Any | None,
    send: Callable[[RenderedMessage], Awaitable[None]],
    *,
    close_message: str,
    facade: Any | None = None,
    project: str | None = None,
    branch: str | None = None,
) -> None:
    if journal and session.transcript:
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        transcript_lines = [
            f"[{engine}]: {answer[:500]}" for engine, answer in session.transcript
        ]
        entry = JournalEntry(
            run_id=f"rt:{session.thread_id}",
            channel_id=session.channel_id,
            timestamp=timestamp,
            event="roundtable_closed",
            data={
                "topic": session.topic,
                "engines": session.engines,
                "rounds": session.current_round,
                "transcript": "\n\n".join(transcript_lines),
            },
        )
        with contextlib.suppress(Exception):
            await journal.append(entry)

    if facade and project and session.transcript:
        with contextlib.suppress(Exception):
            await facade.save_roundtable(
                session,
                project,
                branch_name=branch,
                auto_synthesis=True,
                auto_structured=True,
            )

    await send(RenderedMessage(text=close_message))


async def dispatch_roundtable_command(
    args: str,
    *,
    runtime: Any,
    channel_id: str,
    thread_id: str | None,
    chat_prefs: Any | None,
    roundtables: RoundtableStore | None,
    send: Callable[[RenderedMessage], Awaitable[None]],
    start_roundtable: Callable[[str, int, list[str]], Awaitable[None]],
    handle_rt_command: Callable[..., Awaitable[None]],
    continue_roundtable_session: Callable[
        [RoundtableSession, str, list[str] | None, Any | None], Awaitable[None]
    ],
    archive_roundtable_session: Callable[
        [RoundtableSession, str | None, str | None], Awaitable[None]
    ],
) -> None:
    """Prepare shared !rt callbacks and dispatch to the transport command handler."""
    continue_rt = None
    close_rt = None

    ambient_context = await chat_prefs.get_context(channel_id) if chat_prefs else None
    project = ambient_context.project if ambient_context else None
    branch = ambient_context.branch if ambient_context else None

    if thread_id and roundtables:
        completed_session = roundtables.get_completed(thread_id)
        if completed_session:

            async def continue_rt(
                topic: str,
                engines_filter: list[str] | None,
                *,
                _session: RoundtableSession = completed_session,
                _context: Any | None = ambient_context,
            ) -> None:
                await continue_roundtable_session(
                    _session,
                    topic,
                    engines_filter,
                    _context,
                )

            async def close_rt(
                *,
                _thread_id: str = thread_id,
                _roundtables: RoundtableStore = roundtables,
                _session: RoundtableSession = completed_session,
            ) -> None:
                await archive_roundtable_session(_session, project, branch)
                _roundtables.remove(_thread_id)

        active_session = roundtables.get(thread_id)
        if active_session and not active_session.completed and close_rt is None:

            async def close_rt(
                *,
                _thread_id: str = thread_id,
                _roundtables: RoundtableStore = roundtables,
            ) -> None:
                session = _roundtables.get(_thread_id)
                if session:
                    session.cancel_event.set()
                    await archive_roundtable_session(session, project, branch)
                _roundtables.remove(_thread_id)

    await handle_rt_command(
        args,
        runtime=runtime,
        send=send,
        start_roundtable=start_roundtable,
        continue_roundtable=continue_rt,
        close_roundtable=close_rt,
        thread_id=thread_id,
    )


async def auto_bind_channel_project(
    channel_id: str,
    runtime: Any,
    *,
    get_channel_name: Callable[[str], Awaitable[str | None]],
    log_event: str,
) -> None:
    """Bind a chat channel to a project whose directory matches the channel name."""
    if runtime.projects_root is None:
        return
    if runtime._projects.project_for_chat(channel_id) is not None:
        return

    channel_name = await get_channel_name(channel_id)
    if not channel_name:
        return

    root = Path(runtime.projects_root).expanduser()
    if not root.is_dir():
        return

    channel_lower = channel_name.lower()
    for candidate in root.iterdir():
        if candidate.is_dir() and candidate.name.lower() == channel_lower:
            runtime._projects.register_discovered(
                alias=candidate.name,
                path=candidate,
                chat_id=channel_id,
            )
            logger.info(
                log_event,
                channel_id=channel_id,
                channel_name=channel_name,
                project=candidate.name,
            )
            return


async def start_roundtable_thread(
    channel_id: str,
    topic: str,
    rounds: int,
    engines: list[str],
    *,
    cfg: Any,
    running_tasks: Any,
    chat_prefs: Any | None,
    roundtables: RoundtableStore,
    render_header: Callable[[str, int, list[str]], str],
) -> None:
    """Create a roundtable thread and run all rounds."""
    ref = await cfg.exec_cfg.transport.send(
        channel_id=channel_id,
        message=RenderedMessage(text=render_header(topic, rounds, engines)),
    )
    if ref is None:
        logger.error("roundtable.header_send_failed", channel_id=channel_id)
        return

    thread_id = str(ref.message_id)
    session = RoundtableSession(
        thread_id=thread_id,
        channel_id=channel_id,
        topic=topic,
        engines=engines,
        total_rounds=rounds,
    )
    roundtables.put(session)

    ambient_context = None
    if chat_prefs:
        ambient_context = await chat_prefs.get_context(channel_id)

    logger.info(
        "roundtable.start",
        thread_id=thread_id,
        topic=topic,
        engines=engines,
        rounds=rounds,
    )

    try:
        await run_roundtable(
            session,
            cfg=cfg,
            chat_prefs=chat_prefs,
            running_tasks=running_tasks,
            ambient_context=ambient_context,
            parallel_first_round=cfg.runtime.roundtable.parallel_first_round,
        )
    finally:
        roundtables.complete(thread_id)
