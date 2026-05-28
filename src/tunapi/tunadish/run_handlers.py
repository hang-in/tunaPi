from __future__ import annotations

import anyio
from typing import Any

from tunapi.model import ResumeToken
from tunapi.runners.run_options import EngineRunOptions, apply_run_options
from tunapi.transport import RenderedMessage, SendOptions
from tunapi.runner_bridge import ExecBridgeConfig, IncomingMessage, RunningTask
from tunapi.utils.paths import reset_run_base_dir, set_run_base_dir
from ..logging import get_logger

logger = get_logger(__name__)


async def execute_run(
    backend: Any,
    conv_id: str,
    text: str,
    runtime: Any,
    transport: Any,
    *,
    timeout: int | None = None,
) -> None:
    # 실행 시작 알림
    await transport._send_notification(
        "run.status",
        {
            "conversation_id": conv_id,
            "status": "running",
        },
    )

    progress_ref = await transport.send(
        channel_id=conv_id,
        message=RenderedMessage(text="⏳ starting..."),
        options=SendOptions(notify=False),
    )

    running_task = RunningTask()
    if progress_ref is not None:
        backend.running_tasks[progress_ref] = running_task
        backend.run_map[conv_id] = progress_ref

    run_base_token = None
    try:
        # branch: 채널이면 원래 대화의 컨텍스트를 사용
        context_conv_id = await backend._resolve_context_conv_id(conv_id)
        ambient_ctx = await backend.context_store.get_context(context_conv_id)

        # ── rawq 컨텍스트 주입 ──
        enriched_text = text
        if ambient_ctx:
            project_name = getattr(ambient_ctx, "project", None)
            if project_name:
                enriched_text = await backend._rawq_enrich_message(
                    text, project_name, runtime
                )

        # ── 크로스 세션 요약 주입 ──
        if ambient_ctx:
            project_name = getattr(ambient_ctx, "project", None)
            if project_name:
                cross_summary = await backend._build_cross_session_summary(
                    conv_id, project_name
                )
                if cross_summary:
                    enriched_text = f"{cross_summary}\n---\n{enriched_text}"

        # conv settings → ChatPrefs → project default 순으로 엔진/모델 결정
        conv_settings = backend.context_store.get_conv_settings(conv_id)
        engine_override = conv_settings.engine
        if not engine_override and backend._chat_prefs:
            prefs_engine = await backend._chat_prefs.get_default_engine(context_conv_id)
            if prefs_engine:
                engine_override = prefs_engine

        resolved = runtime.resolve_message(
            text=text,
            reply_text=None,
            ambient_context=ambient_ctx,
        )

        # conv별 독립 토큰 조회 (tunadish 전용) — 프로젝트 단위보다 우선
        conv_session = await backend._conv_sessions.get(conv_id)
        if conv_session:
            # conv_settings.engine이 명시적이고 conv_session.engine과 다르면 토큰 폐기
            if engine_override and conv_session.engine != engine_override:
                effective_token = None
            else:
                effective_token = ResumeToken(
                    engine=conv_session.engine, value=conv_session.token
                )
        else:
            effective_token = resolved.resume_token

        # 새 세션(resume token 없음) 시작 시 code map 주입
        if effective_token is None and ambient_ctx:
            _proj = getattr(ambient_ctx, "project", None)
            if _proj:
                from . import rawq_bridge as _rb

                _proj_path = backend._resolve_project_path(_proj, runtime)
                if _proj_path and _rb.is_available():
                    _map = await _rb.get_map(project_path=_proj_path, depth=2)
                    _map_block = _rb.format_map_block(_map) if _map else ""
                    if _map_block:
                        enriched_text = f"{_map_block}\n\n{enriched_text}"
                        logger.info("rawq.session_map_injected", project=_proj)

        # conv settings → ChatPrefs → resolve_message 순으로 엔진 override
        final_engine_override = engine_override or resolved.engine_override
        rr = runtime.resolve_runner(
            resume_token=effective_token,
            engine_override=final_engine_override,
        )

        # conv settings 모델 override → ChatPrefs 모델 override
        # 엔진과 모델 호환성 검증 + 자동 엔진 전환
        resolved_engine = rr.runner.engine if hasattr(rr.runner, "engine") else None
        model_override = conv_settings.model
        if model_override and resolved_engine:
            from ..engine_models import (
                get_models as _get_engine_models,
                find_engine_for_model,
            )

            valid_models, _ = _get_engine_models(resolved_engine)
            if valid_models and model_override not in valid_models:
                # Auto-switch engine if model belongs to another engine
                correct_engine = find_engine_for_model(model_override)
                if correct_engine:
                    logger.info(
                        "tunadish.auto_engine_switch",
                        model=model_override,
                        from_engine=resolved_engine,
                        to_engine=correct_engine,
                    )
                    rr = runtime.resolve_runner(
                        resume_token=None,  # new engine = new session
                        engine_override=correct_engine,
                    )
                    effective_token = None  # discard old engine's resume token
                else:
                    logger.warning(
                        "tunadish.model_override_unknown",
                        model=model_override,
                        engine=resolved_engine,
                    )
                    model_override = None
        if not model_override and backend._chat_prefs and final_engine_override:
            model_override = await backend._chat_prefs.get_engine_model(
                context_conv_id, final_engine_override
            )

        cwd = runtime.resolve_run_cwd(resolved.context)
        run_base_token = set_run_base_dir(cwd)

        # Set engine/model meta on transport for message notifications
        run_engine = rr.runner.engine if hasattr(rr.runner, "engine") else None
        run_model = model_override or getattr(rr.runner, "model", None)
        transport.set_run_meta(run_engine, run_model)

        cfg = ExecBridgeConfig(
            transport=transport,
            presenter=backend.presenter,
            final_notify=False,
        )

        incoming = IncomingMessage(
            channel_id=conv_id,
            message_id=progress_ref.message_id if progress_ref else "tmp_id",
            text=enriched_text,
        )

        run_timeout = timeout or backend._RUN_TIMEOUT
        run_options = EngineRunOptions(model=model_override) if model_override else None

        def _on_started(evt: Any) -> None:
            """CLI started event에서 실제 모델을 캡처하여 transport meta 업데이트."""
            meta = evt.meta or {}
            model = meta.get("model") or run_model
            engine = evt.engine if hasattr(evt, "engine") else run_engine
            transport.set_run_meta(engine, model)

        with apply_run_options(run_options), anyio.fail_after(run_timeout):
            import tunapi.tunadish.backend as backend_mod

            await backend_mod.handle_message(
                cfg=cfg,
                journal=backend._journal,
                runner=rr.runner,
                incoming=incoming,
                resume_token=effective_token,
                context=resolved.context,
                running_tasks=backend.running_tasks,
                progress_ref=progress_ref,
                project_sessions=backend._project_sessions,
                on_thread_known=backend._make_conv_token_saver(conv_id),
                on_started=_on_started,
            )
    except TimeoutError:
        logger.error(
            "Run timed out after %ds for %s", timeout or backend._RUN_TIMEOUT, conv_id
        )
        if progress_ref:
            await transport.edit(
                ref=progress_ref,
                message=RenderedMessage(
                    text=f"**⏱️ 타임아웃:** {timeout or backend._RUN_TIMEOUT}초 초과로 실행이 중단되었습니다."
                ),
            )
    except Exception as e:
        logger.exception("Error during _execute_run")
        if progress_ref:
            await transport.edit(
                ref=progress_ref, message=RenderedMessage(text=f"**❌ 오류 발생:** {e}")
            )
    finally:
        transport.set_run_meta(None, None)
        if run_base_token is not None:
            reset_run_base_dir(run_base_token)
        backend.run_map.pop(conv_id, None)
        # 실행 완료 알림
        await transport._send_notification(
            "run.status",
            {
                "conversation_id": conv_id,
                "status": "idle",
            },
        )


def make_conv_token_saver(backend: Any, conv_id: str) -> Any:
    """handle_message()의 on_thread_known 콜백 생성."""

    async def _on_thread_known(token, done):
        await backend._conv_sessions.set(
            conv_id,
            engine=token.engine,
            token=token.value,
        )

    return _on_thread_known


async def build_cross_session_summary(
    backend: Any, conv_id: str, project: str
) -> str | None:
    """같은 프로젝트의 다른 세션 최근 활동 요약 생성."""
    all_convs = backend.context_store.list_conversations(project=project)
    sibling_ids = [c["id"] for c in all_convs if c["id"] != conv_id]

    if not sibling_ids:
        return None

    summaries = []
    for sib_id in sibling_ids[:3]:
        entries = await backend._journal.recent_entries(sib_id, limit=5)
        if not entries:
            continue

        meta = backend.context_store._cache.get(sib_id)
        label = meta.label if meta and meta.label else sib_id[:8]

        lines = []
        for e in entries:
            if e.event == "prompt":
                text = e.data.get("text", "")[:100]
                lines.append(f"  - [user] {text}")
            elif e.event == "completed" and e.data.get("ok"):
                answer = e.data.get("answer", "")[:100]
                lines.append(f"  - [assistant] {answer}")

        if lines:
            summaries.append(f"세션 '{label}':\n" + "\n".join(lines[-4:]))

    if not summaries:
        return None

    return (
        "<sibling_sessions>\n"
        "같은 프로젝트의 다른 세션 최근 활동:\n\n"
        + "\n\n".join(summaries)
        + "\n</sibling_sessions>"
    )
