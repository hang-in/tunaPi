# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Response Style (when running via tunapi/Mattermost or Telegram)

- 핵심만 짧게 답변
- Mattermost Markdown 형식으로 작성

## Project Overview

tunapi is a Mattermost, Slack, and Telegram bridge for agent CLIs (Claude Code, Codex, Gemini CLI, OpenCode, Pi). Set `transport = "mattermost"`, `transport = "slack"`, or `transport = "telegram"` in `tunapi.toml`.

Config: `~/.tunapi/tunapi.toml`

## Commands

```sh
uv sync --dev                  # install dependencies
just check                     # format + lint + typecheck + tests
uv run pytest --no-cov         # tests without coverage
uv run pytest tests/test_foo.py -k "test_name"  # single test
just docs-serve                # local docs
```

## Architecture

```
[Mattermost WebSocket | Slack Socket Mode | Telegram Long-Polling | Tunadish WebSocket] → Transport (parse)
    → TransportRuntime (resolve engine/project)
    → Runner (spawn agent CLI, stream JSONL) → RunnerBridge (track progress, send updates)
    → Presenter (render Markdown/HTML) → Transport (send/edit messages back)
```

Four transports (Mattermost, Slack, Telegram, Tunadish) share the same runtime, runner, presenter protocols, and core modules.

### Core Protocols (`src/tunapi/`)

- **Transport** (`transport.py`) — send/edit/delete messages
- **Runner** (`runner.py`) — execute agent CLI, yield `TunapiEvent` stream. `JsonlSubprocessRunner` is the base class
- **RunnerBridge** (`runner_bridge.py`) — track progress with 5s tick refresh
- **Presenter** (`presenter.py`) — render `ProgressState` to `RenderedMessage`
- **Journal** (`journal.py`) — JSONL journal for conversation handoff, PendingRunLedger

### Shared Core (`src/tunapi/core/`) — Mattermost/Slack 공통; Telegram은 별도 구현 유지

- `lifecycle.py` — heartbeat, shutdown state, restart notification, pending-run recovery, graceful drain, SIGTERM handler
- `chat_sessions.py` — per-channel/engine resume token store (v2 schema with v1 migration)
- `chat_prefs.py` — per-channel preferences (engine, trigger mode, project binding, personas, per-engine model override)
- `outbox.py` — priority queue with rate limiting, deduplication, retry-after handling
- `trigger.py` — resolve_trigger_mode (Slack/MM 공통, default 파라미터)
- `startup.py` — build_startup_message (bold/line_break 주입)
- `presenter.py` — ChatPresenter (Slack/MM 공통 render logic)
- `commands.py` — parse_command (Slack/MM 공통 /! command parsing)
- `files.py` — file validation, path resolution, atomic write (transport-agnostic)
- `voice.py` — audio transcription via OpenAI (transport-agnostic)
- `roundtable.py` — multi-agent sequential opinion collection, follow-up, persistence

### Project Collaboration Memory (`src/tunapi/core/`) — P0~P2 완료

- `project_memory.py` — per-project decisions, reviews, ideas, context entries
- `branch_sessions.py` — git branch lifecycle (active/merged/abandoned)
- `discussion_records.py` — structured roundtable results (summary, resolution, action_items)
- `conversation_branch.py` — dialogue-level branching (independent of git branches)
- `synthesis.py` — distilled discussion artifacts (thesis, agreements, disagreements, open_questions)
- `review.py` — review request/approve/reject workflow
- `rt_participant.py` — engine + role separation for roundtable participants
- `rt_utterance.py` — structured per-turn records (stage, reply_to chain)
- `rt_structured.py` — StructuredRoundtableSession with participants + utterances
- `memory_facade.py` — unified API: ProjectMemoryFacade, ProjectContextDTO, get_handoff_uri
- `handoff.py` — async re-entry deep links (tunapi://open?project=...)

### Mattermost Transport (`src/tunapi/mattermost/`)

- `api_models.py` — msgspec models for MM API (Post, User, Channel, WebSocketEvent)
- `client_api.py` — low-level HTTP + WebSocket client (Bearer auth in handshake headers)
- `client.py` — outbox queue with rate limiting, deduplication, and graceful drain on shutdown
- `bridge.py` — `MattermostTransport` + `MattermostPresenter`
- `loop.py` — WebSocket event loop with `ChatSessionStore` for resume, SIGTERM graceful shutdown
- `parsing.py` — WebSocket events → typed messages
- `backend.py` — `TransportBackend` entry point
- `chat_prefs.py` — per-channel preferences storage (engine, trigger mode)
- `trigger_mode.py` — @mention detection for bot invocation in group channels
- `voice.py` — voice message transcription
- `files.py` — file attachment download and auto-recognition
- `commands.py` — slash command handling (`/help`, `/model`, `/trigger`, `/status`, `/cancel`, `/file`, `/new`, `/project`, `/persona`, `/rt`)
- `roundtable.py` — multi-agent roundtable: sequential opinion collection with transcript context

### Slack Transport (`src/tunapi/slack/`)

- `api_models.py` — msgspec models for Slack API (SlackMessage, SocketModeEnvelope)
- `client_api.py` — HTTP + Socket Mode WebSocket client (reconnection with fresh URL per attempt, disconnect envelope handling, exponential backoff)
- `client.py` — outbox queue with rate limiting (re-exports core Outbox)
- `bridge.py` — `SlackTransport` + `SlackPresenter`
- `loop.py` — Socket Mode event loop with access control, lifecycle management (re-uses core lifecycle)
- `parsing.py` — Socket Mode events → typed messages with bot/channel/user filtering
- `backend.py` — `TransportBackend` entry point
- `commands.py` — slash command handling (`/help`, `/model`, `/trigger`, `/status`, `/cancel`, `/new`, `/project`, `/persona`)
- `trigger_mode.py` — @mention detection (default: mentions only); thread replies always trigger without mention

### Telegram Transport (`src/tunapi/telegram/`)

Uses long-polling, inline keyboard for cancel, and supports topics, voice notes, and file transfer.

### Tunadish Transport (`src/tunapi/tunadish/`)

WebSocket-based transport for the tunadish web client. JSON-RPC 2.0 protocol.

- `backend.py` — `TunadishBackend` entry point, WebSocket handler, RPC dispatch, `_execute_run`
- `transport.py` — `TunadishTransport` (send/edit/delete via WebSocket, per-run engine/model metadata in `message.new`/`message.update` notifications)
- `commands.py` — shared command handlers (help, model, project, memory, branch, review, context, roundtable)
- `session_store.py` — per-conversation resume token store (`~/.tunapi/tunadish_conv_sessions.json`)
- `context_store.py` — per-conversation project/branch binding
- `presenter.py` — progress rendering for WebSocket client
- `rawq_bridge.py` — code search/map integration

### Engines (`src/tunapi/runners/`)

Each subclasses `JsonlSubprocessRunner`: `claude.py`, `codex.py`, `gemini.py`, `opencode.py`, `pi.py`

Gemini CLI engine supports auto model selection — the model is resolved automatically unless overridden in config.

### Plugin System (`plugins.py`)

Entry-point groups in `pyproject.toml`:
- `tunapi.engine_backends` — claude, codex, gemini (auto model), opencode, pi
- `tunapi.transport_backends` — telegram, mattermost, slack, tunadish

### Configuration (`settings.py`, `config.py`)

Pydantic settings from `~/.tunapi/tunapi.toml`. Env prefix: `TUNAPI__`. `MATTERMOST_TOKEN` env var supported for Mattermost token; `TELEGRAM_TOKEN` for Telegram; `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN` for Slack. Per-project `chat_id` maps channels to engines. `[roundtable]` section configures multi-agent roundtable (engines, rounds, max_rounds).

### Engine Models (`src/tunapi/engine_models.py`)

Dynamic model discovery per engine with fallback registry. `!models` command for listing, `!model <engine> <model>` for setting. Per-channel model override stored in `chat_prefs.engine_models`.

- **Codex**: reads `~/.codex/models_cache.json` (auto-cached by CLI, `visibility != "hide"` filter)
- **Gemini**: reads constants from installed `@google/gemini-cli-core` npm package (filters `lite`, `customtools`)
- **Claude**: static fallback list (OAuth-only, no local model cache)
- `find_engine_for_model(model)` — reverse lookup: given a model ID, returns the engine it belongs to
- `shorten_model(model)` — display shortener (`claude-opus-4-6[1m]` → `opus4.6`)
- Results cached in-process with 1-hour TTL; `invalidate_cache()` to refresh

## Test Patterns

- Fakes: `tests/telegram_fakes.py`, event factories: `tests/factories.py`
- Coverage threshold: 71% (pytest-cov) — target: 85%
- Python 3.14+ required
