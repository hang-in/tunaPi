import json
import re
import anyio
import anyio.abc
import websockets
from pathlib import Path
from typing import Any
from functools import partial

from ..transport import MessageRef, RenderedMessage
from ..runner_bridge import RunningTask, handle_message  # noqa: F401
from ..transport_runtime import TransportRuntime
from ..journal import Journal
from ..core.chat_prefs import ChatPrefsStore
from ..core.project_sessions import ProjectSessionStore
from ..core.memory_facade import ProjectMemoryFacade
from ..core.commands import parse_command
from ..logging import get_logger
from ..utils.paths import reset_run_base_dir, set_run_base_dir  # noqa: F401

from .commands import dispatch_command
from .context_store import ConversationContextStore
from .transport import TunadishTransport
from .presenter import TunadishPresenter
from . import backend_delegates

logger = get_logger(__name__)

# rawq 컨텍스트 블록을 히스토리에서 제거하는 패턴
_RAWQ_CONTEXT_RE = re.compile(r"<relevant_code>.*?</relevant_code>\s*---\s*", re.DOTALL)
# 크로스 세션 요약 블록을 히스토리에서 제거하는 패턴
_SIBLING_CONTEXT_RE = re.compile(
    r"<sibling_sessions>.*?</sibling_sessions>\s*---\s*", re.DOTALL
)


class TunadishBackend:
    id = "tunadish"
    description = "Tunadish WebSocket Transport"

    def __init__(self):
        self._conv_locks: dict[str, anyio.Lock] = {}
        self.run_map: dict[str, MessageRef] = {}
        self.running_tasks: dict[MessageRef, RunningTask] = {}
        self.presenter = TunadishPresenter()
        self._task_group: anyio.abc.TaskGroup | None = None
        self._prepare_only: bool = False
        self._active_transports: set[TunadishTransport] = set()

    def check_setup(
        self, engine_backend: Any, *, transport_override: str | None = None
    ) -> Any:
        try:
            from ..transports import SetupResult

            return SetupResult(issues=[], config_path=Path("."))
        except ImportError:

            class DummyResult:
                issues = []
                ok = True

            return DummyResult()

    async def interactive_setup(self, *, force: bool = False) -> bool:
        return True

    def lock_token(
        self, *, transport_config: dict[str, Any], _config_path: Any
    ) -> str | None:
        return None

    def _discover_projects(self, configured_aliases: list[str]) -> list[str]:
        """projects_root 하위에서 .git 디렉토리를 가진 폴더 탐색 (미설정 프로젝트만)"""
        try:
            import tomllib

            if not self._config_path or not Path(self._config_path).exists():
                return []
            with open(self._config_path, "rb") as f:
                config = tomllib.load(f)
            projects_root = config.get("projects_root")
            if not projects_root:
                return []
            root = Path(projects_root).expanduser()
            if not root.exists():
                return []
            configured_paths: set[Path] = set()
            for proj in config.get("projects", {}).values():
                p = proj.get("path")
                if p:
                    configured_paths.add(Path(p).expanduser().resolve())
            return [
                d.name
                for d in sorted(root.iterdir())
                if d.is_dir()
                and (d / ".git").exists()
                and d.name not in configured_aliases
                and d.resolve() not in configured_paths
            ]
        except Exception as e:  # noqa: BLE001
            logger.warning("Project discovery failed: %s", e)
            return []

    def _get_projects_root(self) -> str | None:
        """toml에서 projects_root 읽기"""
        try:
            import tomllib

            if not self._config_path or not Path(self._config_path).exists():
                return None
            with open(self._config_path, "rb") as f:
                config = tomllib.load(f)
            return config.get("projects_root")
        except Exception:  # noqa: BLE001
            return None

    def build_and_run(
        self,
        *,
        transport_config: dict[str, Any],
        config_path: Any,
        runtime: TransportRuntime,
        final_notify: bool,
        default_engine_override: str | None,
    ) -> None:
        ctx_path = Path.home() / ".tunapi" / "tunadish_context.json"
        self.context_store = ConversationContextStore(ctx_path)
        self._journal = Journal(Path.home() / ".tunapi" / "tunadish_journals")
        self._cross_journals = [
            ("mattermost", Journal(Path.home() / ".tunapi" / "journals")),
            ("slack", Journal(Path.home() / ".tunapi" / "slack_journals")),
        ]
        self._config_path = config_path
        self._chat_prefs = ChatPrefsStore(
            Path.home() / ".tunapi" / "tunadish_prefs.json"
        )
        self._project_sessions = ProjectSessionStore(
            Path.home() / ".tunapi" / "sessions.json"
        )
        self._facade = ProjectMemoryFacade()

        from .session_store import ConversationSessionStore

        self._conv_sessions = ConversationSessionStore(
            Path.home() / ".tunapi" / "tunadish_conv_sessions.json"
        )

        self._transport_config = transport_config
        self._runtime = runtime
        self._final_notify = final_notify

        if not self._prepare_only:
            anyio.run(self.async_run)

    async def async_run(self) -> None:
        """멀티 transport 병렬 실행용 엔트리포인트."""
        transport_config = self._transport_config
        runtime = self._runtime
        final_notify = self._final_notify

        port = transport_config.get("port", 8765) if transport_config else 8765
        host = (
            transport_config.get("host", "127.0.0.1")
            if transport_config
            else "127.0.0.1"
        )
        logger.info("Starting tunadish websocket server on ws://%s:%s", host, port)
        async with anyio.create_task_group() as tg:
            self._task_group = tg
            tg.start_soon(self._rawq_startup_check)
            async with websockets.serve(
                partial(self._ws_handler, runtime, final_notify), host, port
            ):
                await anyio.sleep_forever()

    async def _broadcast(self, method: str, params: dict[str, Any]) -> None:
        """모든 연결된 클라이언트에 알림 전송 (멀티윈도우 동기화)."""
        dead: list[TunadishTransport] = []
        for t in list(self._active_transports):
            try:
                await t._send_notification(method, params)
            except Exception:  # noqa: BLE001
                dead.append(t)
        for t in dead:
            self._active_transports.discard(t)
            logger.debug(
                "broadcast: removed dead transport (remaining=%d)",
                len(self._active_transports),
            )

    async def _ws_handler(
        self, runtime: TransportRuntime, final_notify: bool, websocket
    ):
        transport = TunadishTransport(websocket)
        self._active_transports.add(transport)
        remote = getattr(websocket, "remote_address", None)
        logger.info(
            "tunadish ws connected: %s (active=%d)",
            remote,
            len(self._active_transports),
        )

        # 재연결 시 진행 중인 run이 있으면 클라이언트에 통지
        for conv_id, ref in list(self.run_map.items()):
            task = self.running_tasks.get(ref)
            if task is not None and not task.done.is_set():
                await transport._send_notification(
                    "run.status",
                    {
                        "conversation_id": conv_id,
                        "status": "running",
                    },
                )
                logger.info(
                    "Notified reconnected client of running task for %s", conv_id
                )

        try:
            async with anyio.create_task_group() as ws_tg:
                try:
                    async for message in websocket:
                        try:
                            data = json.loads(message)
                            method = data.get("method")
                            params = data.get("params", {})
                            logger.debug(
                                "tunadish ws recv: method=%s id=%s",
                                method,
                                data.get("id"),
                            )
                            rpc_id = data.get("id")

                            # JSON-RPC 2.0: rpc_id가 있고 fire-and-forget이 아닌 메서드는
                            # 다음 _send_notification 호출을 표준 response로 자동 변환
                            if rpc_id is not None and method not in (
                                "ping",
                                "chat.send",
                                "run.cancel",
                            ):
                                transport.set_rpc_id(rpc_id)

                            if method == "ping":
                                if rpc_id is not None:
                                    await transport._send_response(
                                        rpc_id, {"pong": True}
                                    )
                                else:
                                    await websocket.send(json.dumps({"method": "pong"}))
                            elif method == "chat.send":
                                if rpc_id is not None:
                                    await transport._send_response(
                                        rpc_id, {"accepted": True}
                                    )
                                ws_tg.start_soon(
                                    self.handle_chat_send, params, runtime, transport
                                )
                            elif method == "run.cancel":
                                await self.handle_run_cancel(params, websocket)
                                if rpc_id is not None:
                                    await transport._send_response(
                                        rpc_id, {"cancelled": True}
                                    )
                            elif method == "project.list":
                                from . import context_handlers

                                await context_handlers.handle_project_list(
                                    self, params, runtime, transport
                                )
                            elif method == "conversation.create":
                                from . import context_handlers

                                await context_handlers.handle_conversation_create(
                                    self, params, transport
                                )
                            elif method == "conversation.delete":
                                from . import context_handlers

                                await context_handlers.handle_conversation_delete(
                                    self, params, transport
                                )
                            elif method == "conversation.list":
                                from . import context_handlers

                                await context_handlers.handle_conversation_list(
                                    self, params, runtime, transport
                                )
                            elif method == "conversation.history":
                                from . import context_handlers

                                await context_handlers.handle_conversation_history(
                                    self, params, transport
                                )
                            # --- Structured JSON RPC (for context panel) ---
                            elif method == "project.context":
                                await self._handle_project_context(
                                    params, runtime, transport
                                )
                            elif method == "branch.list.json":
                                await self._handle_branch_list_json(
                                    params, runtime, transport
                                )
                            elif method == "memory.list.json":
                                await self._handle_memory_list_json(params, transport)
                            elif method == "review.list.json":
                                await self._handle_review_list_json(params, transport)
                            # --- rawq code search/map ---
                            elif method == "code.search":
                                await self._handle_code_search(
                                    params, runtime, transport
                                )
                            elif method == "code.map":
                                await self._handle_code_map(params, runtime, transport)
                            # --- JSON-RPC direct command methods ---
                            elif method == "help":
                                await self._dispatch_rpc_command(
                                    "help", "", params, runtime, transport
                                )
                            elif method == "model.set":
                                engine = params.get("engine", "")
                                model = params.get("model", "")
                                # Auto-detect engine from model if not specified
                                if model and not engine:
                                    from ..engine_models import find_engine_for_model

                                    detected = find_engine_for_model(model)
                                    if detected:
                                        engine = detected
                                args = f"{engine} {model}".strip() if model else engine
                                await self._dispatch_rpc_command(
                                    "model", args, params, runtime, transport
                                )
                            elif method == "model.list":
                                engine = params.get("engine", "")
                                await self._dispatch_rpc_command(
                                    "models", engine, params, runtime, transport
                                )
                            elif method == "trigger.set":
                                mode = params.get("mode", "")
                                await self._dispatch_rpc_command(
                                    "trigger", mode, params, runtime, transport
                                )
                            elif method == "project.set":
                                name = params.get("name", "")
                                await self._dispatch_rpc_command(
                                    "project", f"set {name}", params, runtime, transport
                                )
                                # rawq 인덱싱 트리거 (백그라운드, 실패 무시)
                                if self._task_group is not None:
                                    self._task_group.start_soon(
                                        self._rawq_ensure_index,
                                        name,
                                        runtime,
                                        transport,
                                    )
                            elif method == "project.info":
                                await self._dispatch_rpc_command(
                                    "project", "info", params, runtime, transport
                                )
                            elif method == "persona.set":
                                await self._dispatch_rpc_command(
                                    "persona",
                                    params.get("args", ""),
                                    params,
                                    runtime,
                                    transport,
                                )
                            elif method == "persona.list":
                                await self._dispatch_rpc_command(
                                    "persona", "list", params, runtime, transport
                                )
                            elif method == "memory.list":
                                entry_type = params.get("type", "")
                                await self._dispatch_rpc_command(
                                    "memory",
                                    f"list {entry_type}".strip(),
                                    params,
                                    runtime,
                                    transport,
                                )
                            elif method == "memory.add":
                                t = params.get("type", "")
                                title = params.get("title", "")
                                content = params.get("content", "")
                                await self._dispatch_rpc_command(
                                    "memory",
                                    f"add {t} {title} {content}",
                                    params,
                                    runtime,
                                    transport,
                                )
                            elif method == "memory.search":
                                query = params.get("query", "")
                                await self._dispatch_rpc_command(
                                    "memory",
                                    f"search {query}",
                                    params,
                                    runtime,
                                    transport,
                                )
                            elif method == "memory.delete":
                                entry_id = params.get("id", "")
                                await self._dispatch_rpc_command(
                                    "memory",
                                    f"delete {entry_id}",
                                    params,
                                    runtime,
                                    transport,
                                )
                            elif method == "branch.list":
                                status = params.get("status", "")
                                await self._dispatch_rpc_command(
                                    "branch",
                                    f"list {status}".strip(),
                                    params,
                                    runtime,
                                    transport,
                                )
                            elif method == "branch.merge":
                                bid = params.get("id", "")
                                await self._dispatch_rpc_command(
                                    "branch", f"merge {bid}", params, runtime, transport
                                )
                            elif method == "branch.discard":
                                bid = params.get("id", "")
                                await self._dispatch_rpc_command(
                                    "branch",
                                    f"discard {bid}",
                                    params,
                                    runtime,
                                    transport,
                                )
                            elif method == "review.list":
                                status = params.get("status", "")
                                await self._dispatch_rpc_command(
                                    "review",
                                    f"list {status}".strip(),
                                    params,
                                    runtime,
                                    transport,
                                )
                            elif method == "review.approve":
                                rid = params.get("id", "")
                                comment = params.get("comment", "")
                                await self._dispatch_rpc_command(
                                    "review",
                                    f"approve {rid} {comment}".strip(),
                                    params,
                                    runtime,
                                    transport,
                                )
                            elif method == "review.reject":
                                rid = params.get("id", "")
                                comment = params.get("comment", "")
                                await self._dispatch_rpc_command(
                                    "review",
                                    f"reject {rid} {comment}".strip(),
                                    params,
                                    runtime,
                                    transport,
                                )
                            elif method == "context.get":
                                await self._dispatch_rpc_command(
                                    "context", "", params, runtime, transport
                                )
                            elif method == "session.new":
                                await self._dispatch_rpc_command(
                                    "new", "", params, runtime, transport
                                )
                            elif method == "status":
                                await self._dispatch_rpc_command(
                                    "status", "", params, runtime, transport
                                )
                            elif method == "roundtable.start":
                                topic = params.get("topic", "")
                                await self._dispatch_rpc_command(
                                    "rt", f'"{topic}"', params, runtime, transport
                                )
                            # --- Branch actions ---
                            elif method == "branch.create":
                                await self._handle_branch_create(params, transport)
                            elif method == "branch.switch":
                                await self._handle_branch_switch(params, transport)
                            elif method == "branch.adopt":
                                await self._handle_branch_adopt(params, transport)
                            elif method == "branch.archive":
                                await self._handle_branch_archive(params, transport)
                            elif method == "branch.delete":
                                await self._handle_branch_delete(params, transport)
                            # --- Message actions ---
                            elif method == "message.retry":
                                await self._handle_message_retry(
                                    params, runtime, transport, ws_tg
                                )
                            elif method == "message.save":
                                await self._handle_message_save(params, transport)
                            elif method == "message.delete":
                                await self._handle_message_delete(params, transport)
                            elif method == "message.adopt":
                                await self._handle_message_adopt(params, transport)
                            # --- Phase 4: Write API + Handoff ---
                            elif method == "discussion.save_roundtable":
                                await self._handle_discussion_save(params, transport)
                            elif method == "discussion.link_branch":
                                await self._handle_discussion_link_branch(
                                    params, transport
                                )
                            elif method == "synthesis.create_from_discussion":
                                await self._handle_synthesis_create(params, transport)
                            elif method == "review.request":
                                await self._handle_review_request(params, transport)
                            elif method == "handoff.create":
                                await self._handle_handoff_create(
                                    params, runtime, transport
                                )
                            elif method == "handoff.parse":
                                await self._handle_handoff_parse(params, transport)
                            elif method == "engine.list":
                                await self._handle_engine_list(runtime, transport)
                            else:
                                logger.warning("Unknown JSON-RPC method: %s", method)
                                if rpc_id is not None:
                                    transport._pending_rpc_id = (
                                        None  # 소비 안 된 rpc_id 정리
                                    )
                                    await transport._send_error(
                                        rpc_id, -32601, f"Method not found: {method}"
                                    )
                        except Exception as e:  # noqa: BLE001
                            logger.error("Error handling websocket message: %s", e)
                            # 미소비 rpc_id가 남아있으면 에러 response 전송
                            if (
                                rpc_id is not None
                                and transport._pending_rpc_id is not None
                            ):
                                transport._pending_rpc_id = None
                                await transport._send_error(rpc_id, -32000, str(e))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("ws_handler receive loop exited: %s", exc)
        except* Exception as eg:  # noqa: BLE001
            logger.debug("ws_handler task group exited with exceptions: %s", eg)
        finally:
            transport._closed = True
            self._active_transports.discard(transport)
            logger.info(
                "tunadish ws disconnected: %s (remaining=%d)",
                remote,
                len(self._active_transports),
            )
            # WS disconnect 시: 마지막 transport가 떠날 때만 run cancel
            if not self._active_transports:
                for conv_id, ref in list(self.run_map.items()):
                    task = self.running_tasks.get(ref)
                    if task is not None and not task.cancel_requested.is_set():
                        task.cancel_requested.set()
                        logger.info(
                            "Cancelled orphan run for %s (no active transports)",
                            conv_id,
                        )
            else:
                logger.info(
                    "ws disconnected but %d transports remain, runs continue",
                    len(self._active_transports),
                )

    async def _dispatch_rpc_command(
        self,
        cmd: str,
        args: str,
        params: dict[str, Any],
        runtime: TransportRuntime,
        transport: TunadishTransport,
    ) -> bool:
        """RPC 메서드를 커맨드 핸들러로 라우팅. 응답은 command.result notification으로 전송."""
        conv_id = params.get("conversation_id", "__rpc__")

        # 설정 변경 커맨드: conv settings도 동시 업데이트
        settings_update: dict[str, str | None] = {}
        if cmd == "model" and args.strip():
            parts = args.strip().split(None, 1)
            engine = parts[0] if parts else None
            model = parts[1].strip() if len(parts) > 1 else None
            if engine:
                settings_update["engine"] = engine
            if model and model.lower() != "clear":
                settings_update["model"] = model
            elif model and model.lower() == "clear":
                settings_update["model"] = None
        elif cmd == "trigger" and args.strip():
            settings_update["trigger_mode"] = args.strip()
        elif cmd == "persona" and args.strip() and not args.strip().startswith("list"):
            # persona set/add — 첫 토큰이 persona 이름
            persona_name = args.strip().split()[0] if args.strip() else None
            if persona_name and persona_name not in ("list", "remove", "delete"):
                settings_update["persona"] = persona_name

        async def send(msg: RenderedMessage) -> None:
            payload: dict[str, Any] = {
                "command": cmd,
                "conversation_id": conv_id,
                "text": msg.text or "",
            }
            # 설정 변경 성공 시 conv settings 업데이트 + 응답에 포함
            if settings_update and conv_id != "__rpc__":
                updated = await self.context_store.update_conv_settings(
                    conv_id, **settings_update
                )
                payload["settings"] = updated.to_dict()
            payload_settings = self.context_store.get_conv_settings(conv_id).to_dict()
            if payload_settings:
                payload.setdefault("settings", payload_settings)
            await transport._send_notification("command.result", payload)

        return await dispatch_command(
            cmd,
            args,
            channel_id=conv_id,
            runtime=runtime,
            chat_prefs=self._chat_prefs,
            facade=self._facade,
            journal=self._journal,
            context_store=self.context_store,
            conv_sessions=self._conv_sessions,
            running_tasks=self.running_tasks,
            projects_root=self._get_projects_root(),
            config_path=Path(self._config_path) if self._config_path else None,
            send=send,
        )

    async def _resolve_context_conv_id(self, conv_id: str) -> str:
        """branch:{branch_id} → 원래 대화 conv_id로 변환. 일반 conv_id는 그대로 반환."""
        if not conv_id.startswith("branch:"):
            return conv_id
        branch_id = conv_id.split(":", 1)[1]
        # 모든 프로젝트에서 해당 branch의 session_id(부모 conv_id) 조회
        for cid, meta in self.context_store._cache.items():
            if cid.startswith("branch:"):
                continue
            project = meta.project
            if project:
                branch_obj = await self._facade.conv_branches.get(project, branch_id)
                if branch_obj and branch_obj.session_id:
                    return branch_obj.session_id
        # fallback: branch: 프리픽스 제거 후 context_store에서 직접 조회
        return conv_id

    async def handle_chat_send(
        self,
        params: dict[str, Any],
        runtime: TransportRuntime,
        transport: TunadishTransport,
    ):
        try:
            conv_id = params.get("conversation_id")
            text = params.get("text", "")
            if not conv_id:
                logger.error("chat.send missing conversation_id")
                return

            # ! 커맨드 파싱 — 커맨드이면 dispatch하고 리턴
            cmd, cmd_args = parse_command(text)
            if cmd is not None:

                async def send(msg: RenderedMessage) -> None:
                    await transport._send_notification(
                        "command.result",
                        {
                            "command": cmd,
                            "conversation_id": conv_id,
                            "text": msg.text or "",
                        },
                    )

                handled = await dispatch_command(
                    cmd,
                    cmd_args,
                    channel_id=conv_id,
                    runtime=runtime,
                    chat_prefs=self._chat_prefs,
                    facade=self._facade,
                    journal=self._journal,
                    context_store=self.context_store,
                    conv_sessions=self._conv_sessions,
                    running_tasks=self.running_tasks,
                    projects_root=self._get_projects_root(),
                    config_path=Path(self._config_path) if self._config_path else None,
                    send=send,
                )
                if handled:
                    return
                # unknown command → fall through to AI execution

            lock = self._conv_locks.setdefault(conv_id, anyio.Lock())
            if lock.locked():
                logger.warning("Run already in progress for conversation %s", conv_id)
                return

            run_timeout = params.get("timeout")
            async with lock:
                await self._execute_run(
                    conv_id, text, runtime, transport, timeout=run_timeout
                )
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error in handle_chat_send")

    _RUN_TIMEOUT: int = 300  # 기본 실행 타임아웃 (초)

    async def _execute_run(
        self,
        conv_id: str,
        text: str,
        runtime: TransportRuntime,
        transport: TunadishTransport,
        *,
        timeout: int | None = None,
    ):
        from . import run_handlers

        await run_handlers.execute_run(
            self, conv_id, text, runtime, transport, timeout=timeout
        )

    def _make_conv_token_saver(self, conv_id: str):
        from . import run_handlers

        return run_handlers.make_conv_token_saver(self, conv_id)

    async def _build_cross_session_summary(
        self, conv_id: str, project: str
    ) -> str | None:
        from . import run_handlers

        return await run_handlers.build_cross_session_summary(self, conv_id, project)

    _handle_project_context = backend_delegates._handle_project_context
    _handle_branch_list_json = backend_delegates._handle_branch_list_json
    _handle_memory_list_json = backend_delegates._handle_memory_list_json
    _handle_review_list_json = backend_delegates._handle_review_list_json
    _handle_branch_create = backend_delegates._handle_branch_create
    _handle_branch_switch = backend_delegates._handle_branch_switch
    _handle_branch_adopt = backend_delegates._handle_branch_adopt
    _build_adopt_summary = backend_delegates._build_adopt_summary
    _build_branch_context = backend_delegates._build_branch_context
    _handle_branch_archive = backend_delegates._handle_branch_archive
    _handle_branch_delete = backend_delegates._handle_branch_delete
    _handle_message_retry = backend_delegates._handle_message_retry
    _handle_message_save = backend_delegates._handle_message_save
    _handle_message_delete = backend_delegates._handle_message_delete
    _handle_message_adopt = backend_delegates._handle_message_adopt
    _rawq_startup_check = backend_delegates._rawq_startup_check
    _resolve_project_path = backend_delegates._resolve_project_path
    _rawq_ensure_index = backend_delegates._rawq_ensure_index
    _rawq_enrich_message = backend_delegates._rawq_enrich_message
    _handle_code_search = backend_delegates._handle_code_search
    _handle_code_map = backend_delegates._handle_code_map
    _handle_discussion_save = backend_delegates._handle_discussion_save
    _handle_discussion_link_branch = backend_delegates._handle_discussion_link_branch
    _handle_synthesis_create = backend_delegates._handle_synthesis_create
    _handle_review_request = backend_delegates._handle_review_request
    _handle_handoff_create = backend_delegates._handle_handoff_create
    _handle_handoff_parse = backend_delegates._handle_handoff_parse
    _handle_engine_list = backend_delegates._handle_engine_list

    async def handle_run_cancel(self, params: dict[str, Any], websocket):
        conv_id = params.get("conversation_id")
        if not isinstance(conv_id, str):
            logger.warning("Cancel requested without conversation_id")
            return
        progress_ref = self.run_map.get(conv_id)
        if progress_ref is None:
            logger.warning("Cancel requested but no active run for %s", conv_id)
            return

        task = self.running_tasks.get(progress_ref)
        if task is not None:
            task.cancel_requested.set()
            logger.info("Cancelled run for conversation %s", conv_id)


BACKEND = TunadishBackend()
