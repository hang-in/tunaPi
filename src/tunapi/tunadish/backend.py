import json
import re
import anyio
import anyio.abc
import websockets
from pathlib import Path
from typing import Any
from functools import partial

from ..transport import MessageRef, RenderedMessage, SendOptions
from ..runner_bridge import handle_message, RunningTask, ExecBridgeConfig, IncomingMessage
from ..transport_runtime import TransportRuntime
from ..api import set_run_base_dir, reset_run_base_dir
from ..journal import Journal, JournalEntry
from ..core.chat_prefs import ChatPrefsStore
from ..core.project_sessions import ProjectSessionStore
from ..core.memory_facade import ProjectMemoryFacade
from ..core.commands import parse_command
from ..runners.run_options import EngineRunOptions, apply_run_options
from ..logging import get_logger

from .transport import TunadishTransport
from .presenter import TunadishPresenter

logger = get_logger(__name__)

# rawq 컨텍스트 블록을 히스토리에서 제거하는 패턴
_RAWQ_CONTEXT_RE = re.compile(r"<relevant_code>.*?</relevant_code>\s*---\s*", re.DOTALL)
# 크로스 세션 요약 블록을 히스토리에서 제거하는 패턴
_SIBLING_CONTEXT_RE = re.compile(r"<sibling_sessions>.*?</sibling_sessions>\s*---\s*", re.DOTALL)

from .commands import dispatch_command
from .context_store import ConversationContextStore


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

    def check_setup(self, engine_backend: Any, *, transport_override: str | None = None) -> Any:
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

    def lock_token(self, *, transport_config: dict[str, Any], _config_path: Any) -> str | None:
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
                d.name for d in sorted(root.iterdir())
                if d.is_dir()
                and (d / ".git").exists()
                and d.name not in configured_aliases
                and d.resolve() not in configured_paths
            ]
        except Exception as e:
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
        except Exception:
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
        self._chat_prefs = ChatPrefsStore(Path.home() / ".tunapi" / "tunadish_prefs.json")
        self._project_sessions = ProjectSessionStore(Path.home() / ".tunapi" / "sessions.json")
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
        host = transport_config.get("host", "127.0.0.1") if transport_config else "127.0.0.1"
        logger.info("Starting tunadish websocket server on ws://%s:%s", host, port)
        async with anyio.create_task_group() as tg:
            self._task_group = tg
            tg.start_soon(self._rawq_startup_check)
            async with websockets.serve(partial(self._ws_handler, runtime, final_notify), host, port):
                await anyio.sleep_forever()

    async def _broadcast(self, method: str, params: dict[str, Any]) -> None:
        """모든 연결된 클라이언트에 알림 전송 (멀티윈도우 동기화)."""
        dead: list[TunadishTransport] = []
        for t in list(self._active_transports):
            try:
                await t._send_notification(method, params)
            except Exception:
                dead.append(t)
        for t in dead:
            self._active_transports.discard(t)
            logger.debug("broadcast: removed dead transport (remaining=%d)", len(self._active_transports))

    async def _ws_handler(self, runtime: TransportRuntime, final_notify: bool, websocket):
        transport = TunadishTransport(websocket)
        self._active_transports.add(transport)
        remote = getattr(websocket, "remote_address", None)
        logger.info("tunadish ws connected: %s (active=%d)", remote, len(self._active_transports))

        try:
            async with anyio.create_task_group() as ws_tg:
                try:
                    async for message in websocket:
                        try:
                            data = json.loads(message)
                            method = data.get("method")
                            params = data.get("params", {})
                            logger.debug("tunadish ws recv: method=%s id=%s", method, data.get("id"))
                            rpc_id = data.get("id")

                            # JSON-RPC 2.0: rpc_id가 있고 fire-and-forget이 아닌 메서드는
                            # 다음 _send_notification 호출을 표준 response로 자동 변환
                            if rpc_id is not None and method not in ("ping", "chat.send", "run.cancel"):
                                transport.set_rpc_id(rpc_id)

                            if method == "ping":
                                if rpc_id is not None:
                                    await transport._send_response(rpc_id, {"pong": True})
                                else:
                                    await websocket.send(json.dumps({"method": "pong"}))
                            elif method == "chat.send":
                                if rpc_id is not None:
                                    await transport._send_response(rpc_id, {"accepted": True})
                                ws_tg.start_soon(self.handle_chat_send, params, runtime, transport)
                            elif method == "run.cancel":
                                await self.handle_run_cancel(params, websocket)
                                if rpc_id is not None:
                                    await transport._send_response(rpc_id, {"cancelled": True})
                            elif method == "project.list":
                                configured_aliases = list(runtime.project_aliases())
                                discovered = self._discover_projects(configured_aliases)
                                # Build rich project objects from ProjectConfig
                                configured = []
                                projects_map = getattr(
                                    getattr(runtime, "_projects", None), "projects", {}
                                )
                                for key, pc in projects_map.items():
                                    # chat_id가 있고 path가 프로젝트 자체가 아니면 channel
                                    p_path = pc.path if pc.path else None
                                    is_channel = bool(
                                        getattr(pc, "chat_id", None)
                                        and p_path
                                        and not (p_path / ".git").is_dir()
                                    )
                                    configured.append({
                                        "key": key,
                                        "alias": pc.alias,
                                        "path": str(p_path) if p_path else None,
                                        "default_engine": pc.default_engine,
                                        "type": "channel" if is_channel else "project",
                                    })
                                # configured aliases without ProjectConfig (fallback)
                                known_keys = {c["key"] for c in configured}
                                for alias in configured_aliases:
                                    if alias.lower() not in known_keys:
                                        configured.append({
                                            "key": alias.lower(),
                                            "alias": alias,
                                            "path": None,
                                            "default_engine": None,
                                        })
                                await transport._send_notification("project.list.result", {
                                    "configured": configured,
                                    "discovered": discovered,
                                })
                            elif method == "conversation.create":
                                conv_id = params.get("conversation_id")
                                project = params.get("project")
                                label = params.get("label")
                                if conv_id and project:
                                    from ..context import RunContext
                                    await self.context_store.set_context(
                                        conv_id,
                                        RunContext(project=project),
                                        label=label,
                                    )
                                    await transport._send_notification("conversation.created", {
                                        "conversation_id": conv_id,
                                        "project": project,
                                        "label": label or conv_id[:8],
                                    })
                            elif method == "conversation.delete":
                                conv_id = params.get("conversation_id")
                                if conv_id:
                                    await self.context_store.clear(conv_id)
                                    # 저널 파일도 정리
                                    journal_path = self._journal._base_dir / f"{conv_id}.jsonl"
                                    if journal_path.exists():
                                        journal_path.unlink()
                                    await transport._send_notification("conversation.deleted", {
                                        "conversation_id": conv_id,
                                    })
                            elif method == "conversation.list":
                                project_filter = params.get("project")
                                convs = self.context_store.list_conversations(project=project_filter)
                                for c in convs:
                                    c["source"] = "tunadish"
                                if project_filter:
                                    try:
                                        chat_ids = runtime.chat_ids_for_project(project_filter)
                                    except Exception:
                                        chat_ids = []
                                    for transport_name, journal in self._cross_journals:
                                        for cid in chat_ids:
                                            try:
                                                cid_str = str(cid)
                                                journal_path = journal._base_dir / f"{cid_str}.jsonl"
                                                if journal_path.exists():
                                                    entries = await journal.recent_entries(cid_str, limit=1)
                                                    last_ts = entries[-1].timestamp if entries else ""
                                                    convs.append({
                                                        "id": cid_str,
                                                        "project": project_filter,
                                                        "branch": None,
                                                        "label": f"{transport_name}",
                                                        "created_at": 0.0,
                                                        "source": transport_name,
                                                        "last_activity": last_ts,
                                                    })
                                            except Exception as cross_err:
                                                logger.debug("Cross-transport journal lookup failed for %s/%s: %s", transport_name, cid, cross_err)
                                await transport._send_notification("conversation.list.result", {
                                    "conversations": convs,
                                })
                            elif method == "conversation.history":
                                conv_id = params.get("conversation_id")
                                branch_id = params.get("branch_id")
                                if conv_id:
                                    # branch_id가 있으면 브랜치 전용 채널의 히스토리 반환
                                    # 브랜치 채널에 메시지가 없으면 (레거시) 빈 배열 반환
                                    history_channel = f"branch:{branch_id}" if branch_id else conv_id

                                    # 모든 journal에서 엔트리를 수집하여 병합
                                    all_entries = []
                                    td_entries = await self._journal.recent_entries(history_channel, limit=200)
                                    if td_entries:
                                        all_entries.extend(td_entries)
                                    if not branch_id:
                                        # 메인 대화만 cross-transport journal 병합
                                        for tname, j in self._cross_journals:
                                            try:
                                                cross_entries = await j.recent_entries(conv_id, limit=200)
                                                if cross_entries:
                                                    all_entries.extend(cross_entries)
                                            except Exception as cross_err:
                                                logger.debug("Cross-transport history failed for %s/%s: %s", tname, conv_id, cross_err)
                                    # timestamp 순 정렬
                                    entries = sorted(all_entries, key=lambda e: e.timestamp)
                                    messages = []
                                    # prompt entries의 engine/model을 run_id별로 추적
                                    run_meta: dict[str, dict[str, str | None]] = {}
                                    for e in entries:
                                        if e.event == "prompt":
                                            raw_text = e.data.get("text", "")
                                            clean_text = _RAWQ_CONTEXT_RE.sub("", raw_text)
                                            clean_text = _SIBLING_CONTEXT_RE.sub("", clean_text)
                                            meta = {"engine": e.engine, "model": e.data.get("model")}
                                            run_meta[e.run_id] = meta
                                            messages.append({
                                                "role": "user",
                                                "content": clean_text,
                                                "timestamp": e.timestamp,
                                            })
                                        elif e.event == "completed" and e.data.get("ok"):
                                            answer = e.data.get("answer")
                                            if answer:
                                                meta = run_meta.get(e.run_id, {})
                                                msg: dict[str, Any] = {
                                                    "role": "assistant",
                                                    "content": answer,
                                                    "timestamp": e.timestamp,
                                                }
                                                if meta.get("engine"):
                                                    msg["engine"] = meta["engine"]
                                                if meta.get("model"):
                                                    msg["model"] = meta["model"]
                                                messages.append(msg)
                                    await transport._send_notification("conversation.history.result", {
                                        "conversation_id": history_channel,
                                        "messages": messages,
                                    })
                            # --- Structured JSON RPC (for context panel) ---
                            elif method == "project.context":
                                await self._handle_project_context(params, runtime, transport)
                            elif method == "branch.list.json":
                                await self._handle_branch_list_json(params, runtime, transport)
                            elif method == "memory.list.json":
                                await self._handle_memory_list_json(params, transport)
                            elif method == "review.list.json":
                                await self._handle_review_list_json(params, transport)
                            # --- rawq code search/map ---
                            elif method == "code.search":
                                await self._handle_code_search(params, runtime, transport)
                            elif method == "code.map":
                                await self._handle_code_map(params, runtime, transport)
                            # --- JSON-RPC direct command methods ---
                            elif method == "help":
                                await self._dispatch_rpc_command("help", "", params, runtime, transport)
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
                                await self._dispatch_rpc_command("model", args, params, runtime, transport)
                            elif method == "model.list":
                                engine = params.get("engine", "")
                                await self._dispatch_rpc_command("models", engine, params, runtime, transport)
                            elif method == "trigger.set":
                                mode = params.get("mode", "")
                                await self._dispatch_rpc_command("trigger", mode, params, runtime, transport)
                            elif method == "project.set":
                                name = params.get("name", "")
                                await self._dispatch_rpc_command("project", f"set {name}", params, runtime, transport)
                                # rawq 인덱싱 트리거 (백그라운드, 실패 무시)
                                if self._task_group is not None:
                                    self._task_group.start_soon(self._rawq_ensure_index, name, runtime, transport)
                            elif method == "project.info":
                                await self._dispatch_rpc_command("project", "info", params, runtime, transport)
                            elif method == "persona.set":
                                await self._dispatch_rpc_command("persona", params.get("args", ""), params, runtime, transport)
                            elif method == "persona.list":
                                await self._dispatch_rpc_command("persona", "list", params, runtime, transport)
                            elif method == "memory.list":
                                entry_type = params.get("type", "")
                                await self._dispatch_rpc_command("memory", f"list {entry_type}".strip(), params, runtime, transport)
                            elif method == "memory.add":
                                t = params.get("type", "")
                                title = params.get("title", "")
                                content = params.get("content", "")
                                await self._dispatch_rpc_command("memory", f"add {t} {title} {content}", params, runtime, transport)
                            elif method == "memory.search":
                                query = params.get("query", "")
                                await self._dispatch_rpc_command("memory", f"search {query}", params, runtime, transport)
                            elif method == "memory.delete":
                                entry_id = params.get("id", "")
                                await self._dispatch_rpc_command("memory", f"delete {entry_id}", params, runtime, transport)
                            elif method == "branch.list":
                                status = params.get("status", "")
                                await self._dispatch_rpc_command("branch", f"list {status}".strip(), params, runtime, transport)
                            elif method == "branch.merge":
                                bid = params.get("id", "")
                                await self._dispatch_rpc_command("branch", f"merge {bid}", params, runtime, transport)
                            elif method == "branch.discard":
                                bid = params.get("id", "")
                                await self._dispatch_rpc_command("branch", f"discard {bid}", params, runtime, transport)
                            elif method == "review.list":
                                status = params.get("status", "")
                                await self._dispatch_rpc_command("review", f"list {status}".strip(), params, runtime, transport)
                            elif method == "review.approve":
                                rid = params.get("id", "")
                                comment = params.get("comment", "")
                                await self._dispatch_rpc_command("review", f"approve {rid} {comment}".strip(), params, runtime, transport)
                            elif method == "review.reject":
                                rid = params.get("id", "")
                                comment = params.get("comment", "")
                                await self._dispatch_rpc_command("review", f"reject {rid} {comment}".strip(), params, runtime, transport)
                            elif method == "context.get":
                                await self._dispatch_rpc_command("context", "", params, runtime, transport)
                            elif method == "session.new":
                                await self._dispatch_rpc_command("new", "", params, runtime, transport)
                            elif method == "status":
                                await self._dispatch_rpc_command("status", "", params, runtime, transport)
                            elif method == "roundtable.start":
                                topic = params.get("topic", "")
                                await self._dispatch_rpc_command("rt", f'"{topic}"', params, runtime, transport)
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
                                await self._handle_message_retry(params, runtime, transport, ws_tg)
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
                                await self._handle_discussion_link_branch(params, transport)
                            elif method == "synthesis.create_from_discussion":
                                await self._handle_synthesis_create(params, transport)
                            elif method == "review.request":
                                await self._handle_review_request(params, transport)
                            elif method == "handoff.create":
                                await self._handle_handoff_create(params, runtime, transport)
                            elif method == "handoff.parse":
                                await self._handle_handoff_parse(params, transport)
                            elif method == "engine.list":
                                await self._handle_engine_list(runtime, transport)
                            else:
                                logger.warning("Unknown JSON-RPC method: %s", method)
                                if rpc_id is not None:
                                    transport._pending_rpc_id = None  # 소비 안 된 rpc_id 정리
                                    await transport._send_error(rpc_id, -32601, f"Method not found: {method}")
                        except Exception as e:
                            logger.error("Error handling websocket message: %s", e)
                            # 미소비 rpc_id가 남아있으면 에러 response 전송
                            if rpc_id is not None and transport._pending_rpc_id is not None:
                                transport._pending_rpc_id = None
                                await transport._send_error(rpc_id, -32000, str(e))
                except Exception:
                    pass
        except* Exception as eg:
            logger.debug("ws_handler task group exited with exceptions: %s", eg)
        finally:
            transport._closed = True
            self._active_transports.discard(transport)
            logger.info("tunadish ws disconnected: %s (remaining=%d)", remote, len(self._active_transports))
            # WS disconnect 시 해당 transport의 활성 run cancel
            for conv_id, ref in list(self.run_map.items()):
                task = self.running_tasks.get(ref)
                if task is not None and not task.cancel_requested.is_set():
                    task.cancel_requested.set()
                    logger.info("Cancelled orphan run for %s on ws disconnect", conv_id)

    # --- Structured JSON RPC handlers (context panel) ---

    async def _handle_project_context(self, params: dict[str, Any], runtime: TransportRuntime, transport: TunadishTransport):
        """project.context → ProjectContextDTO를 JSON 구조로 반환."""
        conv_id = params.get("conversation_id", "__rpc__")
        ctx = await self.context_store.get_context(conv_id)
        project = ctx.project if ctx else params.get("project")
        if not project:
            await transport._send_notification("project.context.result", {"error": "no project"})
            return

        # params.project fallback으로 프로젝트가 결정된 경우, context_store에 자동 바인딩
        # (__rpc__ 같은 가상 채널은 제외)
        if (ctx is None or not ctx.project) and conv_id != "__rpc__" and not conv_id.startswith("branch:"):
            from ..context import RunContext as _BindRC
            await self.context_store.set_context(conv_id, _BindRC(project=project))

        # ChatPrefs에서 엔진/모델/트리거 조회
        engine = await self._chat_prefs.get_default_engine(conv_id) or runtime.default_engine
        model = await self._chat_prefs.get_engine_model(conv_id, engine) if engine else None
        # ChatPrefs에 model override가 없으면 runner의 기본 model 사용
        if not model and engine:
            try:
                rr = runtime.resolve_runner(resume_token=None, engine_override=engine)
                runner_model = getattr(rr.runner, "model", None)
                if runner_model:
                    model = runner_model
                else:
                    # run_options에서 모델 확인
                    from ..runners.run_options import get_run_options
                    opts = get_run_options()
                    if opts and opts.model:
                        model = opts.model
            except Exception:
                pass
        trigger = await self._chat_prefs.get_trigger_mode(conv_id) or "mentions"
        persona = None  # TODO: persona는 현재 global만 지원

        # facade에서 프로젝트 컨텍스트 DTO
        from ..context import RunContext as _RC
        project_path = None
        try:
            cwd = runtime.resolve_run_cwd(_RC(project=project))
            if cwd:
                project_path = str(cwd)
        except Exception as exc:
            logger.debug("resolve_run_cwd failed for project=%s: %s", project, exc)
        # fallback: ProjectConfig.path에서 직접 가져오기
        if not project_path:
            try:
                projects_map = getattr(getattr(runtime, "_projects", None), "projects", {})
                pc = projects_map.get(project)
                if pc and pc.path:
                    project_path = str(pc.path)
            except Exception:
                pass
        dto = await self._facade.get_project_context_dto(
            project,
            project_path=project_path,
            default_engine=engine,
        )

        # 실제 git 현재 브랜치 조회 (비동기 — 이벤트 루프 블로킹 방지)
        git_branch = None
        logger.debug("project_context: project=%s project_path=%s", project, project_path)
        if project_path:
            try:
                import asyncio as _asyncio
                proc = await _asyncio.create_subprocess_exec(
                    "git", "rev-parse", "--abbrev-ref", "HEAD",
                    cwd=project_path,
                    stdout=_asyncio.subprocess.PIPE,
                    stderr=_asyncio.subprocess.PIPE,
                )
                stdout, stderr = await _asyncio.wait_for(proc.communicate(), timeout=3)
                logger.debug("git rev-parse rc=%s stdout=%r stderr=%r", proc.returncode, stdout.decode().strip(), stderr.decode().strip())
                if proc.returncode == 0:
                    git_branch = stdout.decode().strip()
            except Exception as exc:
                logger.debug("git rev-parse failed: %s", exc)

        # 사용 가능한 엔진+모델 목록
        available_engines: dict[str, list[str]] = {}
        try:
            from ..engine_models import get_models as _get_models
            for eid in runtime.available_engine_ids():
                models, _src = _get_models(eid)
                available_engines[eid] = models
        except Exception:
            pass

        # Resume token 조회 — conv별 토큰 우선, fallback: 프로젝트 단위
        resume_token_value = None
        conv_id_for_token = params.get("conversation_id")
        if conv_id_for_token:
            conv_session = await self._conv_sessions.get(conv_id_for_token)
            if conv_session:
                resume_token_value = conv_session.token
        if not resume_token_value:
            try:
                rt = await self._project_sessions.get(project)
                if rt:
                    resume_token_value = rt.value
            except Exception:
                pass

        result = {
            "project": project,
            "engine": engine,
            "model": model,
            "trigger_mode": trigger,
            "persona": persona,
            "resume_token": resume_token_value,
            "git_branch": git_branch,
            "available_engines": available_engines,
            "memory_entries": [
                {"id": e.id, "type": e.type, "title": e.title, "content": e.content[:200],
                 "source": e.source, "tags": list(e.tags), "timestamp": e.timestamp}
                for e in dto.memory_entries
            ],
            "active_branches": [
                {"name": b.branch_name, "description": b.description or "",
                 "status": b.status, "discussion_count": len(b.discussion_ids)}
                for b in dto.active_branches
            ],
            "conv_branches": [
                {"id": cb.branch_id, "label": cb.label, "status": cb.status,
                 "git_branch": cb.git_branch, "parent_branch_id": cb.parent_branch_id,
                 "session_id": cb.session_id, "checkpoint_id": cb.checkpoint_id}
                for cb in (await self._facade.conv_branches.list(project))
            ],
            "pending_review_count": len(dto.pending_reviews),
            "recent_discussions": [
                {"id": d.discussion_id, "topic": d.topic, "status": d.status,
                 "participants": list(d.participants)}
                for d in dto.discussions[:5]
            ],
            "markdown": dto.markdown,
        }
        # conversation별 설정 override
        conv_s = self.context_store.get_conv_settings(conv_id)
        conv_s_dict = conv_s.to_dict()
        if conv_s_dict:
            result["conv_settings"] = conv_s_dict
        await transport._send_notification("project.context.result", result)

    async def _handle_branch_list_json(self, params: dict[str, Any], runtime: TransportRuntime, transport: TunadishTransport):
        """branch.list.json → Git branches + Conversation branches 구조화."""
        conv_id = params.get("conversation_id", "__rpc__")
        ctx = await self.context_store.get_context(conv_id)
        project = ctx.project if ctx else params.get("project")
        if not project:
            await transport._send_notification("branch.list.json.result", {"error": "no project"})
            return

        git_branches = await self._facade.branches.list_branches(project)
        conv_branches = await self._facade.conv_branches.list(project)

        result = {
            "project": project,
            "git_branches": [
                {"name": b.branch_name, "status": b.status, "description": b.description or "",
                 "parent_branch": b.parent_branch, "linked_entry_count": len(b.memory_entry_ids),
                 "linked_discussion_count": len(b.discussion_ids)}
                for b in git_branches
            ],
            "conv_branches": [
                {"id": cb.branch_id, "label": cb.label, "status": cb.status,
                 "git_branch": cb.git_branch, "parent_branch_id": cb.parent_branch_id,
                 "session_id": cb.session_id, "checkpoint_id": cb.checkpoint_id}
                for cb in conv_branches
            ],
        }
        await transport._send_notification("branch.list.json.result", result)

    async def _handle_memory_list_json(self, params: dict[str, Any], transport: TunadishTransport):
        """memory.list.json → MemoryEntry[] 구조화."""
        conv_id = params.get("conversation_id", "__rpc__")
        ctx = await self.context_store.get_context(conv_id)
        project = ctx.project if ctx else params.get("project")
        if not project:
            await transport._send_notification("memory.list.json.result", {"error": "no project"})
            return

        entry_type = params.get("type")
        limit = params.get("limit", 50)
        entries = await self._facade.memory.list_entries(project, type=entry_type, limit=limit)

        result = {
            "project": project,
            "entries": [
                {"id": e.id, "type": e.type, "title": e.title, "content": e.content,
                 "source": e.source, "tags": list(e.tags), "timestamp": e.timestamp}
                for e in entries
            ],
        }
        await transport._send_notification("memory.list.json.result", result)

    async def _handle_review_list_json(self, params: dict[str, Any], transport: TunadishTransport):
        """review.list.json → ReviewEntry[] 구조화."""
        conv_id = params.get("conversation_id", "__rpc__")
        ctx = await self.context_store.get_context(conv_id)
        project = ctx.project if ctx else params.get("project")
        if not project:
            await transport._send_notification("review.list.json.result", {"error": "no project"})
            return

        status = params.get("status")
        reviews = await self._facade.reviews.list(project, status=status)

        result = {
            "project": project,
            "reviews": [
                {"id": r.review_id, "artifact_id": r.artifact_id,
                 "artifact_version": r.artifact_version, "status": r.status,
                 "reviewer_comment": r.reviewer_comment or "", "created_at": r.created_at}
                for r in reviews
            ],
        }
        await transport._send_notification("review.list.json.result", result)

    # --- Branch action handlers ---

    async def _handle_branch_create(self, params: dict[str, Any], transport: TunadishTransport):
        """branch.create → 대화 브랜치 생성."""
        conv_id = params.get("conversation_id")
        label = params.get("label", "")
        checkpoint_id = params.get("checkpoint_id")
        if not conv_id:
            return

        # branch: 채널이면 parent conv_id로 resolve
        if conv_id.startswith("branch:"):
            conv_id = await self._resolve_context_conv_id(conv_id)

        ctx = await self.context_store.get_context(conv_id)
        project = ctx.project if ctx else None
        if not project:
            return

        # 클라이언트가 parent_branch_id를 명시하면 우선, 아니면 active_branch_id 폴백
        if "parent_branch_id" in params:
            parent_id = params["parent_branch_id"]  # null 명시 → 루트 브랜치
        else:
            meta = self.context_store._cache.get(conv_id)
            parent_id = getattr(meta, "active_branch_id", None) if meta else None

        # 라벨 자동 생성: 기존 브랜치(모든 상태 포함) 수 기반 카운터 — 이름 충돌 방지
        if not label:
            all_branches = await self._facade.conv_branches.list(project)
            # 기존 branch-N 패턴에서 최대 N 추출
            max_n = 0
            for b in all_branches:
                if b.label.startswith("branch-"):
                    try:
                        n = int(b.label.split("-", 1)[1])
                        max_n = max(max_n, n)
                    except (ValueError, IndexError):
                        pass
            label = f"branch-{max_n + 1}"

        branch = await self._facade.conv_branches.create(
            project,
            label=label,
            parent_branch_id=parent_id,
            session_id=conv_id,
            checkpoint_id=checkpoint_id,
        )

        # active_branch_id 갱신
        await self.context_store.set_active_branch(conv_id, branch.branch_id)

        # 브랜치 전용 채널 설정: 부모 conv의 context 복사 + settings 상속
        branch_channel = f"branch:{branch.branch_id}"
        from ..context import RunContext as _BranchRC
        await self.context_store.set_context(branch_channel, _BranchRC(project=project), label=label)
        await self.context_store.copy_conv_settings(conv_id, branch_channel)

        # 브랜치 전용 채널에 분기점 컨텍스트를 journal에 저장
        context_summary = await self._build_branch_context(conv_id, checkpoint_id)
        if context_summary:
            import uuid
            import time as _time
            ctx_run_id = str(uuid.uuid4())
            ts = _time.strftime("%Y-%m-%dT%H:%M:%S")
            await self._journal.append(JournalEntry(
                run_id=ctx_run_id,
                channel_id=branch_channel,
                timestamp=ts,
                event="completed",
                data={"ok": True, "answer": context_summary},
            ))
            # 실시간 알림 (이미 열려 있는 창에)
            ctx_msg_id = str(uuid.uuid4())
            await self._broadcast("message.new", {
                "ref": {"channel_id": branch_channel, "message_id": ctx_msg_id},
                "message": {"text": context_summary},
            })

        await self._broadcast("branch.created", {
            "conversation_id": conv_id,
            "branch_id": branch.branch_id,
            "label": branch.label,
            "parent_branch_id": parent_id,
        })

    async def _handle_branch_switch(self, params: dict[str, Any], transport: TunadishTransport):
        """branch.switch → 브랜치 전환."""
        conv_id = params.get("conversation_id")
        branch_id = params.get("branch_id")  # None이면 메인으로 복귀
        if not conv_id:
            return

        await self.context_store.set_active_branch(conv_id, branch_id)

        await transport._send_notification("branch.switched", {
            "conversation_id": conv_id,
            "branch_id": branch_id,
        })

    async def _handle_branch_adopt(self, params: dict[str, Any], transport: TunadishTransport):
        """branch.adopt → 브랜치 채택, 요약 카드 삽입. sibling 브랜치는 건드리지 않음."""
        conv_id = params.get("conversation_id")
        branch_id = params.get("branch_id")
        if not conv_id or not branch_id:
            return

        ctx = await self.context_store.get_context(conv_id)
        project = ctx.project if ctx else None
        if not project:
            return

        # per-conv 락으로 race condition 방지
        lock = self._conv_locks.setdefault(conv_id, anyio.Lock())
        async with lock:
            # 채택 대상 브랜치 조회
            target = await self._facade.conv_branches.get(project, branch_id)
            if not target:
                return

            # conv_id 검증: 브랜치의 session_id와 불일치 시 보정
            effective_conv_id = conv_id
            if hasattr(target, "session_id") and target.session_id and target.session_id != conv_id:
                logger.warning(
                    "branch.adopt.conv_id_mismatch",
                    requested=conv_id,
                    actual=target.session_id,
                    branch_id=branch_id,
                )
                effective_conv_id = target.session_id

            # 요약 카드용: 브랜치 대화에서 마지막 assistant 응답 발췌
            summary_text = await self._build_adopt_summary(target, effective_conv_id)

            # 채택
            await self._facade.conv_branches.adopt(project, branch_id)

            # 메인으로 복귀
            await self.context_store.set_active_branch(effective_conv_id, None)

            # 요약 카드를 메인 타임라인에 삽입
            if summary_text:
                import uuid
                summary_msg_id = str(uuid.uuid4())
                await transport._send_notification("message.new", {
                    "ref": {"channel_id": effective_conv_id, "message_id": summary_msg_id},
                    "message": {"text": summary_text},
                })
                await transport._send_notification("message.update", {
                    "ref": {"channel_id": effective_conv_id, "message_id": summary_msg_id},
                    "message": {"text": summary_text},
                })

            await self._broadcast("branch.adopted", {
                "conversation_id": effective_conv_id,
                "branch_id": branch_id,
            })

    async def _build_adopt_summary(self, branch, conv_id: str) -> str:
        """브랜치 대화에서 요약 텍스트를 생성 (마지막 assistant 응답 발췌)."""
        label = branch.label or branch.branch_id[:8]
        try:
            entries = await self._journal.recent_entries(conv_id, limit=200)
        except Exception:
            entries = []

        last_response = ""
        turn_count = 0
        for e in reversed(entries):
            if hasattr(e, "event"):
                if e.event == "prompt":
                    turn_count += 1
                if e.event == "response" and not last_response:
                    last_response = (e.data.get("text", "") or "")[:200]

        excerpt = f"> {last_response}{'...' if len(last_response) >= 200 else ''}\n\n" if last_response else ""
        turn_info = f"*{turn_count}턴 대화 · {branch.branch_id[:8]}*" if turn_count > 0 else f"*{branch.branch_id[:8]}*"

        return f"<!-- branch-adopt-summary -->\n🔀 **브랜치 '{label}' 채택됨**\n\n{excerpt}{turn_info}"

    async def _build_branch_context(self, conv_id: str, checkpoint_id: str | None) -> str:
        """분기점까지의 대화 요약을 브랜치 컨텍스트로 생성."""
        try:
            entries = await self._journal.recent_entries(conv_id, limit=200)
        except Exception:
            entries = []
        if not entries:
            return ""

        # checkpoint_id가 있으면 해당 메시지까지만, 없으면 마지막 대화까지
        lines: list[str] = []
        for e in entries:
            if e.event == "prompt":
                text = (e.data.get("text", "") or "")[:300]
                lines.append(f"**User:** {text}")
            elif e.event == "completed" and e.data.get("ok"):
                answer = (e.data.get("answer", "") or "")[:300]
                if answer:
                    lines.append(f"**Assistant:** {answer}")
            # checkpoint 도달 시 중단
            if checkpoint_id and hasattr(e, "run_id") and e.run_id == checkpoint_id:
                break

        if not lines:
            return ""

        # 마지막 4개 턴만 표시 (너무 길지 않게)
        visible = lines[-8:] if len(lines) > 8 else lines
        if len(lines) > 8:
            visible = [f"*...{len(lines) - 8}개 이전 메시지 생략...*", ""] + visible

        return "<!-- branch-context -->\n" + "\n\n".join(visible)

    async def _handle_branch_archive(self, params: dict[str, Any], transport: TunadishTransport):
        """branch.archive → 브랜치 보관."""
        conv_id = params.get("conversation_id")
        branch_id = params.get("branch_id")
        if not conv_id or not branch_id:
            return

        ctx = await self.context_store.get_context(conv_id)
        project = ctx.project if ctx else None
        if not project:
            return

        await self._facade.conv_branches.archive(project, branch_id)

        # 현재 보고 있던 브랜치가 archived되면 메인으로 복귀
        meta = self.context_store._cache.get(conv_id)
        if meta and getattr(meta, "active_branch_id", None) == branch_id:
            await self.context_store.set_active_branch(conv_id, None)

        await self._broadcast("branch.archived", {
            "conversation_id": conv_id,
            "branch_id": branch_id,
        })

    async def _handle_branch_delete(self, params: dict[str, Any], transport: TunadishTransport):
        """branch.delete → 브랜치 영구 삭제 (remove)."""
        conv_id = params.get("conversation_id")
        branch_id = params.get("branch_id")
        if not conv_id or not branch_id:
            return

        # branch: 채널이 들어올 경우 parent conv_id로 resolve
        if conv_id.startswith("branch:"):
            conv_id = await self._resolve_context_conv_id(conv_id)

        ctx = await self.context_store.get_context(conv_id)
        project = ctx.project if ctx else None
        if not project:
            logger.warning("branch.delete: no project context for conv_id=%s", conv_id)
            return

        # 브랜치 기록 영구 삭제
        await self._facade.conv_branches.remove(project, branch_id)

        # 브랜치 전용 채널의 journal 엔트리 정리
        branch_channel = f"branch:{branch_id}"
        try:
            await self._journal.clear_channel(branch_channel)
        except (AttributeError, Exception):
            pass  # journal이 clear_channel을 미지원하면 무시

        # 현재 보고 있던 브랜치가 삭제되면 메인으로 복귀
        meta = self.context_store._cache.get(conv_id)
        if meta and getattr(meta, "active_branch_id", None) == branch_id:
            await self.context_store.set_active_branch(conv_id, None)

        # 모든 윈도우에 알림 (메인 + 브랜치 창)
        await self._broadcast("branch.deleted", {
            "conversation_id": conv_id,
            "branch_id": branch_id,
        })

    # --- Message action handlers ---

    async def _handle_message_retry(self, params: dict[str, Any], runtime: TransportRuntime, transport: TunadishTransport, ws_tg: anyio.abc.TaskGroup):
        """message.retry → 새 브랜치 생성 후 마지막 prompt를 재실행."""
        conv_id = params.get("conversation_id")
        message_id = params.get("message_id")
        if not conv_id or not message_id:
            return

        # 마지막 prompt 찾기
        entries = await self._journal.recent_entries(conv_id, limit=200)
        last_prompt_text = None
        for e in reversed(entries):
            if e.event == "prompt":
                last_prompt_text = e.data.get("text", "")
                break
        if not last_prompt_text:
            return

        # 브랜치 생성 (프로젝트가 있을 때만)
        ctx = await self.context_store.get_context(conv_id)
        project = ctx.project if ctx else None
        if project:
            meta = self.context_store._cache.get(conv_id)
            parent_id = meta.active_branch_id if meta else None
            branch = await self._facade.conv_branches.create(
                project,
                label=f"retry-{message_id[:6]}",
                parent_branch_id=parent_id,
                session_id=conv_id,
            )
            await self.context_store.set_active_branch(conv_id, branch.branch_id)
            await self._broadcast("branch.created", {
                "conversation_id": conv_id,
                "branch_id": branch.branch_id,
                "label": branch.label,
                "parent_branch_id": parent_id,
            })

        # 재실행
        ws_tg.start_soon(self.handle_chat_send, {"conversation_id": conv_id, "text": last_prompt_text}, runtime, transport)

    async def _handle_message_save(self, params: dict[str, Any], transport: TunadishTransport):
        """message.save → 메시지 내용을 프로젝트 메모리에 저장."""
        conv_id = params.get("conversation_id")
        message_id = params.get("message_id")
        content = params.get("content")  # 클라이언트에서 직접 전달
        if not conv_id or not message_id:
            return

        # content가 params에 없으면 저널에서 조회
        if not content:
            entries = await self._journal.recent_entries(conv_id, limit=200)
            for e in reversed(entries):
                if e.event == "completed" and e.data.get("ok"):
                    content = e.data.get("answer", "")
                    if content:
                        break
                elif e.event == "prompt":
                    content = e.data.get("text", "")
                    if content:
                        break

        if not content:
            await transport._send_notification("message.action.result", {
                "action": "save", "ok": False, "error": "message not found",
            })
            return

        ctx = await self.context_store.get_context(conv_id)
        project = ctx.project if ctx else None
        if project:
            await self._facade.memory.add_entry(
                project=project,
                type="context",
                title=f"Saved message {message_id[:8]}",
                content=content[:500],
                source="tunadish",
            )
        await transport._send_notification("message.action.result", {
            "action": "save", "ok": True, "message_id": message_id,
        })

    async def _handle_message_delete(self, params: dict[str, Any], transport: TunadishTransport):
        """message.delete → 클라이언트에 삭제 확인 알림 (저널은 append-only이므로 UI에서만 제거)."""
        conv_id = params.get("conversation_id")
        message_id = params.get("message_id")
        if not conv_id or not message_id:
            return
        # 저널은 append-only → 클라이언트에서 UI 제거만 수행
        await transport._send_notification("message.deleted", {
            "conversation_id": conv_id, "message_id": message_id,
        })

    async def _handle_message_adopt(self, params: dict[str, Any], transport: TunadishTransport):
        """message.adopt → 현재 브랜치를 채택하고 메인으로 복귀. sibling 브랜치는 건드리지 않음."""
        conv_id = params.get("conversation_id")
        message_id = params.get("message_id")
        if not conv_id or not message_id:
            return

        ctx = await self.context_store.get_context(conv_id)
        project = ctx.project if ctx else None
        meta = self.context_store._cache.get(conv_id)
        branch_id = meta.active_branch_id if meta else None

        if project and branch_id:
            lock = self._conv_locks.setdefault(conv_id, anyio.Lock())
            async with lock:
                await self._facade.conv_branches.adopt(project, branch_id)
                await self.context_store.set_active_branch(conv_id, None)
                await self._broadcast("branch.adopted", {
                    "conversation_id": conv_id, "branch_id": branch_id,
                })

        await transport._send_notification("message.action.result", {
            "action": "adopt", "ok": True, "message_id": message_id,
        })

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
                updated = await self.context_store.update_conv_settings(conv_id, **settings_update)
                payload["settings"] = updated.to_dict()
            payload_settings = self.context_store.get_conv_settings(conv_id).to_dict()
            if payload_settings:
                payload.setdefault("settings", payload_settings)
            await transport._send_notification("command.result", payload)

        return await dispatch_command(
            cmd, args,
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

    async def handle_chat_send(self, params: dict[str, Any], runtime: TransportRuntime, transport: TunadishTransport):
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
                    await transport._send_notification("command.result", {
                        "command": cmd,
                        "conversation_id": conv_id,
                        "text": msg.text or "",
                    })

                handled = await dispatch_command(
                    cmd, cmd_args,
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
                await self._execute_run(conv_id, text, runtime, transport, timeout=run_timeout)
        except Exception as e:
            logger.exception("Unhandled error in handle_chat_send")

    _RUN_TIMEOUT: int = 300  # 기본 실행 타임아웃 (초)

    async def _execute_run(self, conv_id: str, text: str, runtime: TransportRuntime, transport: TunadishTransport, *, timeout: int | None = None):
        # 실행 시작 알림
        await transport._send_notification("run.status", {
            "conversation_id": conv_id, "status": "running",
        })

        progress_ref = await transport.send(
            channel_id=conv_id,
            message=RenderedMessage(text="⏳ starting..."),
            options=SendOptions(notify=False),
        )

        running_task = RunningTask()
        if progress_ref is not None:
            self.running_tasks[progress_ref] = running_task
            self.run_map[conv_id] = progress_ref

        run_base_token = None
        try:
            # branch: 채널이면 원래 대화의 컨텍스트를 사용
            context_conv_id = await self._resolve_context_conv_id(conv_id)
            ambient_ctx = await self.context_store.get_context(context_conv_id)

            # ── rawq 컨텍스트 주입 ──
            enriched_text = text
            if ambient_ctx:
                project_name = getattr(ambient_ctx, "project", None)
                if project_name:
                    enriched_text = await self._rawq_enrich_message(
                        text, project_name, runtime
                    )

            # ── 크로스 세션 요약 주입 ──
            if ambient_ctx:
                project_name = getattr(ambient_ctx, "project", None)
                if project_name:
                    cross_summary = await self._build_cross_session_summary(conv_id, project_name)
                    if cross_summary:
                        enriched_text = f"{cross_summary}\n---\n{enriched_text}"

            # conv settings → ChatPrefs → project default 순으로 엔진/모델 결정
            conv_settings = self.context_store.get_conv_settings(conv_id)
            engine_override = conv_settings.engine
            if not engine_override and self._chat_prefs:
                prefs_engine = await self._chat_prefs.get_default_engine(context_conv_id)
                if prefs_engine:
                    engine_override = prefs_engine

            resolved = runtime.resolve_message(
                text=text,
                reply_text=None,
                ambient_context=ambient_ctx,
            )

            # conv별 독립 토큰 조회 (tunadish 전용) — 프로젝트 단위보다 우선
            conv_session = await self._conv_sessions.get(conv_id)
            if conv_session:
                from ..model import ResumeToken
                # conv_settings.engine이 명시적이고 conv_session.engine과 다르면 토큰 폐기
                if engine_override and conv_session.engine != engine_override:
                    effective_token = None
                else:
                    effective_token = ResumeToken(engine=conv_session.engine, value=conv_session.token)
            else:
                effective_token = resolved.resume_token

            # 새 세션(resume token 없음) 시작 시 code map 주입
            if effective_token is None and ambient_ctx:
                _proj = getattr(ambient_ctx, "project", None)
                if _proj:
                    from . import rawq_bridge as _rb
                    _proj_path = self._resolve_project_path(_proj, runtime)
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
                from ..engine_models import get_models as _get_engine_models, find_engine_for_model
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
            if not model_override and self._chat_prefs and final_engine_override:
                model_override = await self._chat_prefs.get_engine_model(context_conv_id, final_engine_override)

            cwd = runtime.resolve_run_cwd(resolved.context)
            run_base_token = set_run_base_dir(cwd)

            # Set engine/model meta on transport for message notifications
            run_engine = rr.runner.engine if hasattr(rr.runner, "engine") else None
            run_model = model_override or getattr(rr.runner, "model", None)
            transport.set_run_meta(run_engine, run_model)

            cfg = ExecBridgeConfig(
                transport=transport,
                presenter=self.presenter,
                final_notify=False,
            )

            incoming = IncomingMessage(
                channel_id=conv_id,
                message_id=progress_ref.message_id if progress_ref else "tmp_id",
                text=enriched_text,
            )

            run_timeout = timeout or self._RUN_TIMEOUT
            run_options = EngineRunOptions(model=model_override) if model_override else None
            def _on_started(evt: Any) -> None:
                """CLI started event에서 실제 모델을 캡처하여 transport meta 업데이트."""
                meta = evt.meta or {}
                model = meta.get("model") or run_model
                engine = evt.engine if hasattr(evt, "engine") else run_engine
                transport.set_run_meta(engine, model)

            with apply_run_options(run_options), anyio.fail_after(run_timeout):
                await handle_message(
                    cfg=cfg,
                    journal=self._journal,
                    runner=rr.runner,
                    incoming=incoming,
                    resume_token=effective_token,
                    context=resolved.context,
                    running_tasks=self.running_tasks,
                    progress_ref=progress_ref,
                    project_sessions=self._project_sessions,
                    on_thread_known=self._make_conv_token_saver(conv_id),
                    on_started=_on_started,
                )
        except TimeoutError:
            logger.error("Run timed out after %ds for %s", timeout or self._RUN_TIMEOUT, conv_id)
            if progress_ref:
                await transport.edit(ref=progress_ref, message=RenderedMessage(text=f"**⏱️ 타임아웃:** {timeout or self._RUN_TIMEOUT}초 초과로 실행이 중단되었습니다."))
        except Exception as e:
            logger.exception("Error during _execute_run")
            if progress_ref:
                await transport.edit(ref=progress_ref, message=RenderedMessage(text=f"**❌ 오류 발생:** {e}"))
        finally:
            transport.set_run_meta(None, None)
            if run_base_token is not None:
                reset_run_base_dir(run_base_token)
            self.run_map.pop(conv_id, None)
            # 실행 완료 알림
            await transport._send_notification("run.status", {
                "conversation_id": conv_id, "status": "idle",
            })

    # ── conv별 resume token 저장 ──

    def _make_conv_token_saver(self, conv_id: str):
        """handle_message()의 on_thread_known 콜백 생성.

        AI 에이전트가 세션을 시작하면 호출되어 conv별 토큰을 저장한다.
        project_sessions 래핑에 의해 프로젝트 단위 저장도 자동 실행된다.
        """
        async def _on_thread_known(token, done):
            await self._conv_sessions.set(
                conv_id,
                engine=token.engine,
                token=token.value,
            )
        return _on_thread_known

    # ── 크로스 세션 요약 ──

    async def _build_cross_session_summary(
        self, conv_id: str, project: str
    ) -> str | None:
        """같은 프로젝트의 다른 세션 최근 활동 요약 생성."""
        all_convs = self.context_store.list_conversations(project=project)
        sibling_ids = [c["id"] for c in all_convs if c["id"] != conv_id]

        if not sibling_ids:
            return None

        summaries = []
        for sib_id in sibling_ids[:3]:
            entries = await self._journal.recent_entries(sib_id, limit=5)
            if not entries:
                continue

            meta = self.context_store._cache.get(sib_id)
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

    # ── rawq integration ──

    async def _rawq_startup_check(self):
        """시작 시 rawq 버전 확인 + 원격 업데이트 체크 (백그라운드)."""
        from . import rawq_bridge

        if not rawq_bridge.is_available():
            logger.info("rawq: not installed (code search disabled)")
            return

        version = await rawq_bridge.get_version()
        logger.info("rawq %s available", version or "unknown")

        # 원격 업데이트 확인 (네트워크, 실패 무시)
        update_info = await rawq_bridge.check_for_update()
        if update_info and update_info.get("has_update"):
            commits = update_info.get("commits", [])
            msg = (
                f"rawq 업데이트 가능: {update_info['current']} → {update_info['latest']}"
                f" ({len(commits)}개 새 커밋)"
            )
            logger.info(msg)
            # 연결된 클라이언트에 알림
            await self._broadcast("command.result", {
                "command": "rawq",
                "conversation_id": "__system__",
                "text": f"🔄 {msg}\n`./scripts/update-rawq.sh --apply`로 업데이트하세요.",
            })

    def _resolve_project_path(self, project_name: str, runtime: TransportRuntime) -> Path | None:
        """프로젝트 이름으로 실제 파일시스템 경로를 해석한다."""
        # 1. tunapi 설정에서 프로젝트 경로 조회
        projects_map = getattr(getattr(runtime, "_projects", None), "projects", {})
        pc = projects_map.get(project_name.lower())
        if pc and getattr(pc, "path", None) and Path(pc.path).exists():
            return Path(pc.path)

        # 2. projects_root 하위에서 탐색
        projects_root = self._get_projects_root()
        if projects_root:
            candidate = Path(projects_root).expanduser() / project_name
            if candidate.exists():
                return candidate

        return None

    async def _rawq_ensure_index(self, project_name: str, runtime: TransportRuntime, transport: TunadishTransport):
        """프로젝트의 rawq 인덱스를 확보한다 (백그라운드)."""
        from . import rawq_bridge

        if not rawq_bridge.is_available():
            return

        project_path = self._resolve_project_path(project_name, runtime)
        if not project_path:
            return

        # 인덱스 상태 확인
        status = await rawq_bridge.check_index(project_path)
        if status is not None:
            logger.debug("rawq index exists for %s", project_name)
            return  # 증분 갱신은 search 시 자동 처리

        # 인덱스 생성
        logger.info("Building rawq index for %s at %s", project_name, project_path)
        await transport._send_notification("command.result", {
            "command": "rawq",
            "conversation_id": "__system__",
            "text": f"🔍 프로젝트 `{project_name}` 코드 인덱스를 생성합니다...",
        })

        ok = await rawq_bridge.build_index(project_path)

        msg = (
            f"✅ `{project_name}` 인덱스 생성 완료."
            if ok
            else f"⚠️ `{project_name}` 인덱스 생성 실패. rawq 없이 계속합니다."
        )
        await transport._send_notification("command.result", {
            "command": "rawq",
            "conversation_id": "__system__",
            "text": msg,
        })

    async def _rawq_enrich_message(
        self,
        text: str,
        project_name: str,
        runtime: TransportRuntime,
    ) -> str:
        """메시지에 rawq 검색 결과를 컨텍스트로 첨부한다.

        실패 시 원본 텍스트를 그대로 반환한다.
        """
        from . import rawq_bridge

        if not rawq_bridge.is_available():
            return text

        project_path = self._resolve_project_path(project_name, runtime)
        if not project_path:
            return text

        # 짧은 질문에는 더 많은 코드 컨텍스트 제공
        text_len = len(text)
        if text_len < 100:
            token_budget = 4000
        elif text_len < 500:
            token_budget = 2000
        else:
            token_budget = 1000

        result = await rawq_bridge.search(
            query=text,
            project_path=project_path,
            top=5,
            token_budget=token_budget,
            threshold=0.5,
        )

        context_block = rawq_bridge.format_context_block(result) if result else ""

        if context_block:
            logger.info(
                "rawq.enrich",
                project=project_name,
                results=len(result.get("results", [])),
                token_budget=token_budget,
            )
            return f"{context_block}\n\n---\n\n{text}"

        # 검색 결과 0건 → code map 폴백으로 프로젝트 구조 제공
        map_result = await rawq_bridge.get_map(project_path=project_path, depth=2)
        map_block = rawq_bridge.format_map_block(map_result) if map_result else ""
        if map_block:
            logger.info("rawq.enrich.map_fallback", project=project_name)
            return f"{map_block}\n\n---\n\n{text}"

        logger.info("rawq.enrich.no_results", project=project_name)
        return text

    async def _handle_code_search(self, params: dict[str, Any], runtime: TransportRuntime, transport: TunadishTransport):
        """code.search RPC 처리 — ContextPanel 코드 검색용."""
        from . import rawq_bridge

        query = params.get("query", "")
        project = params.get("project", "")
        lang = params.get("lang")
        top = params.get("top", 10)

        if not query or not project:
            await transport._send_notification("code.search.result", {
                "error": "query and project are required",
            })
            return

        project_path = self._resolve_project_path(project, runtime)
        if not project_path:
            await transport._send_notification("code.search.result", {
                "error": f"Project path not found: {project}",
            })
            return

        result = await rawq_bridge.search(
            query=query,
            project_path=project_path,
            top=top,
            token_budget=8000,  # UI 검색은 더 많은 결과 허용
            threshold=0.3,      # UI에서는 낮은 threshold 허용
            lang_filter=lang,
        )

        await transport._send_notification("code.search.result", {
            "query": query,
            "project": project,
            "available": rawq_bridge.is_available(),
            "results": result.get("results", []) if result else [],
            "query_ms": result.get("query_ms", 0) if result else 0,
            "total_tokens": result.get("total_tokens", 0) if result else 0,
        })

    async def _handle_code_map(self, params: dict[str, Any], runtime: TransportRuntime, transport: TunadishTransport):
        """code.map RPC 처리 — 프로젝트 구조 뷰용."""
        from . import rawq_bridge

        project = params.get("project", "")
        depth = params.get("depth", 2)
        lang = params.get("lang")

        project_path = self._resolve_project_path(project, runtime)
        if not project_path:
            await transport._send_notification("code.map.result", {
                "error": f"Project path not found: {project}",
            })
            return

        result = await rawq_bridge.get_map(
            project_path=project_path,
            depth=depth,
            lang_filter=lang,
        )

        await transport._send_notification("code.map.result", {
            "project": project,
            "available": rawq_bridge.is_available(),
            "map": result if result else {},
        })

    # --- Phase 4: Write API + Handoff handlers ---

    async def _handle_discussion_save(self, params: dict[str, Any], transport: TunadishTransport):
        """discussion.save_roundtable → DiscussionRecord 저장."""
        project = params.get("project", "")
        if not project:
            await transport._send_notification("discussion.save_roundtable.result", {"error": "project required"})
            return

        # RoundtableSession 대신 params에서 직접 DiscussionRecord 생성
        discussion_id = params.get("discussion_id", "")
        topic = params.get("topic", "")
        participants = params.get("participants", [])
        rounds = params.get("rounds", 0)
        transcript = params.get("transcript", [])
        summary = params.get("summary")
        branch_name = params.get("branch_name")

        record = await self._facade.discussions.create_record(
            project,
            discussion_id=discussion_id,
            topic=topic,
            participants=participants,
            rounds=rounds,
            transcript=transcript,
            summary=summary,
            branch_name=branch_name,
        )

        if branch_name:
            await self._facade.link_discussion_to_branch(project, record.discussion_id, branch_name)

        if params.get("auto_synthesis", False):
            await self._facade.save_synthesis_from_discussion(project, record.discussion_id)

        await transport._send_notification("discussion.save_roundtable.result", {
            "discussion_id": record.discussion_id,
            "project": project,
            "topic": record.topic,
            "status": record.status,
        })

    async def _handle_discussion_link_branch(self, params: dict[str, Any], transport: TunadishTransport):
        """discussion.link_branch → discussion ↔ branch 양방향 링크."""
        project = params.get("project", "")
        discussion_id = params.get("discussion_id", "")
        branch_name = params.get("branch_name", "")

        if not project or not discussion_id or not branch_name:
            await transport._send_notification("discussion.link_branch.result", {"error": "project, discussion_id, branch_name required"})
            return

        ok = await self._facade.link_discussion_to_branch(project, discussion_id, branch_name)
        await transport._send_notification("discussion.link_branch.result", {
            "ok": ok,
            "project": project,
            "discussion_id": discussion_id,
            "branch_name": branch_name,
        })

    async def _handle_synthesis_create(self, params: dict[str, Any], transport: TunadishTransport):
        """synthesis.create_from_discussion → SynthesisArtifact 생성."""
        project = params.get("project", "")
        discussion_id = params.get("discussion_id", "")

        if not project or not discussion_id:
            await transport._send_notification("synthesis.create.result", {"error": "project, discussion_id required"})
            return

        artifact = await self._facade.save_synthesis_from_discussion(project, discussion_id)
        if artifact is None:
            await transport._send_notification("synthesis.create.result", {"error": "discussion not found"})
            return

        await transport._send_notification("synthesis.create.result", {
            "artifact_id": artifact.artifact_id,
            "project": project,
            "source_id": artifact.source_id,
            "thesis": artifact.thesis,
        })

    async def _handle_review_request(self, params: dict[str, Any], transport: TunadishTransport):
        """review.request → ReviewRequest 생성."""
        project = params.get("project", "")
        artifact_id = params.get("artifact_id", "")

        if not project or not artifact_id:
            await transport._send_notification("review.request.result", {"error": "project, artifact_id required"})
            return

        review = await self._facade.request_review_for_synthesis(project, artifact_id)
        if review is None:
            await transport._send_notification("review.request.result", {"error": "artifact not found"})
            return

        await transport._send_notification("review.request.result", {
            "review_id": review.review_id,
            "project": project,
            "artifact_id": review.artifact_id,
            "artifact_version": review.artifact_version,
            "status": review.status,
        })

    async def _handle_handoff_create(self, params: dict[str, Any], runtime: TransportRuntime, transport: TunadishTransport):
        """handoff.create → HandoffURI 생성."""
        project = params.get("project", "")
        if not project:
            await transport._send_notification("handoff.create.result", {"error": "project required"})
            return

        uri = await self._facade.get_handoff_uri(
            project,
            session_id=params.get("session_id"),
            branch_id=params.get("branch_id"),
            focus=params.get("focus"),
            pending_run_id=params.get("pending_run_id"),
        )

        await transport._send_notification("handoff.create.result", {
            "project": project,
            "uri": uri,
        })

    async def _handle_handoff_parse(self, params: dict[str, Any], transport: TunadishTransport):
        """handoff.parse → HandoffURI 파싱."""
        from ..core.handoff import parse_handoff_uri

        uri_str = params.get("uri", "")
        if not uri_str:
            await transport._send_notification("handoff.parse.result", {"error": "uri required"})
            return

        parsed = parse_handoff_uri(uri_str)
        if parsed is None:
            await transport._send_notification("handoff.parse.result", {"error": "invalid handoff URI"})
            return

        await transport._send_notification("handoff.parse.result", {
            "project": parsed.project,
            "session_id": parsed.session_id,
            "branch_id": parsed.branch_id,
            "focus": parsed.focus,
            "pending_run_id": parsed.pending_run_id,
            "engine": parsed.engine,
            "conversation_id": parsed.conversation_id,
        })

    async def _handle_engine_list(self, runtime: TransportRuntime, transport: TunadishTransport):
        """engine.list → 사용 가능한 엔진 + 모델 목록."""
        from ..engine_models import get_models as _get_models

        engines: dict[str, list[str]] = {}
        try:
            for eid in runtime.available_engine_ids():
                models, _src = _get_models(eid)
                engines[eid] = models
        except Exception:
            pass

        await transport._send_notification("engine.list.result", {
            "engines": engines,
        })

    async def handle_run_cancel(self, params: dict[str, Any], websocket):
        conv_id = params.get("conversation_id")
        progress_ref = self.run_map.get(conv_id)
        if progress_ref is None:
            logger.warning("Cancel requested but no active run for %s", conv_id)
            return

        task = self.running_tasks.get(progress_ref)
        if task is not None:
            task.cancel_requested.set()
            logger.info("Cancelled run for conversation %s", conv_id)

BACKEND = TunadishBackend()
