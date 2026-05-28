from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..engine_models import get_models
from ..transport import RenderedMessage


async def handle_model_command(
    args: str,
    *,
    channel_id: str,
    runtime: Any,
    chat_prefs: Any | None,
    send: Any,
    describe_model: Callable[[str], str] | None = None,
    include_models_hint: bool = True,
) -> None:
    parts = args.strip().split(None, 1)
    available = list(runtime.available_engine_ids())
    engine_map = {e.lower(): e for e in available}

    def _model_display(model: str) -> str:
        if describe_model is None:
            return f"`{model}`"
        return f"`{model}` ({describe_model(model)})"

    if not parts or not parts[0]:
        current_engine = None
        if chat_prefs:
            current_engine = await chat_prefs.get_default_engine(channel_id)
        current_display = current_engine or runtime.default_engine
        model_display = ""
        if chat_prefs:
            model = await chat_prefs.get_engine_model(channel_id, current_display)
            if model:
                model_display = f"\nModel: {_model_display(model)}"
        engine_list = ", ".join(f"`{e}`" for e in available)
        hint = (
            "\n전체 모델 목록: `!models` | 특정 엔진: `!models <engine>`"
            if include_models_hint
            else ""
        )
        await send(
            RenderedMessage(
                text=(
                    f"Current engine: `{current_display}`{model_display}\n"
                    f"Available: {engine_list}\n\n"
                    "Usage: `!model <engine>` | `!model <engine> <model>` | "
                    f"`!model <engine> clear`{hint}"
                )
            )
        )
        return

    first = parts[0].lower()
    second = parts[1].strip() if len(parts) > 1 else ""

    if first not in engine_map:
        await send(
            RenderedMessage(
                text=f"Unknown engine `{first}`. Available: {', '.join(f'`{e}`' for e in available)}"
            )
        )
        return

    canonical_engine = engine_map[first]

    if not second:
        if chat_prefs:
            await chat_prefs.set_default_engine(channel_id, canonical_engine)
        model_display = ""
        if chat_prefs:
            model = await chat_prefs.get_engine_model(channel_id, canonical_engine)
            if model:
                model_display = f" (model: `{model}`)"
        await send(
            RenderedMessage(
                text=f"Default engine set to `{canonical_engine}`{model_display}"
            )
        )
        return

    if second.lower() == "clear":
        if chat_prefs:
            await chat_prefs.clear_engine_model(channel_id, canonical_engine)
        await send(
            RenderedMessage(text=f"Model override cleared for `{canonical_engine}`")
        )
        return

    if chat_prefs:
        await chat_prefs.set_engine_model(channel_id, canonical_engine, second)
    await send(
        RenderedMessage(
            text=f"Model for `{canonical_engine}` set to {_model_display(second)}"
        )
    )


async def handle_models_command(
    args: str,
    *,
    channel_id: str,
    runtime: Any,
    chat_prefs: Any | None,
    send: Any,
    title: str,
    engine_bold: Callable[[str], str],
) -> None:
    available = list(runtime.available_engine_ids())
    target = args.strip().lower() if args.strip() else None

    if target:
        engine_map = {e.lower(): e for e in available}
        if target not in engine_map:
            await send(
                RenderedMessage(
                    text=f"Unknown engine `{target}`. Available: {', '.join(f'`{e}`' for e in available)}"
                )
            )
            return
        engines_to_show = [engine_map[target]]
    else:
        engines_to_show = available

    lines: list[str] = [title, ""]
    current_models: dict[str, str] = {}
    if chat_prefs:
        current_models = await chat_prefs.get_all_engine_models(channel_id)

    for engine in engines_to_show:
        models, source = get_models(engine)
        current = current_models.get(engine)
        current_marker = f" ← current: `{current}`" if current else ""

        if models:
            model_list = ", ".join(f"`{m}`" for m in models)
            lines.append(f"{engine_bold(engine)} ({source}){current_marker}")
            lines.append(f"  {model_list}")
        else:
            lines.append(f"{engine_bold(engine)} (no known models){current_marker}")
        lines.append("")

    lines.append("Set: `!model <engine> <model>` | Clear: `!model <engine> clear`")
    await send(RenderedMessage(text="\n".join(lines)))
