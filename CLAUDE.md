# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Response Style (when running via tunapi/Mattermost or Telegram)

- 핵심만 짧게 답변
- Mattermost Markdown 형식으로 작성

## Project Overview

tunapi is a Mattermost and Telegram bridge for agent CLIs (Claude Code, Codex, Gemini CLI, OpenCode, Pi), forked from [takopi](https://github.com/banteg/takopi). It extends takopi's original Telegram transport with a new Mattermost transport while keeping takopi's core intact. Set `transport = "mattermost"` or `transport = "telegram"` in `tunapi.toml`.

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
[Mattermost WebSocket | Telegram Long-Polling] → Transport (parse)
    → TransportRuntime (resolve engine/project)
    → Runner (spawn agent CLI, stream JSONL) → RunnerBridge (track progress, send updates)
    → Presenter (render Markdown/HTML) → Transport (send/edit messages back)
```

Two transports share the same runtime, runner, and presenter protocols. The transport layer abstracts the messaging platform.

### Core Protocols (`src/tunapi/`)

- **Transport** (`transport.py`) — send/edit/delete messages
- **Runner** (`runner.py`) — execute agent CLI, yield `TunapiEvent` stream. `JsonlSubprocessRunner` is the base class
- **Presenter** (`presenter.py`) — render `ProgressState` to `RenderedMessage`

### Mattermost Transport (`src/tunapi/mattermost/`)

- `api_models.py` — msgspec models for MM API (Post, User, Channel, WebSocketEvent)
- `client_api.py` — low-level HTTP + WebSocket client (Bearer auth in handshake headers)
- `client.py` — outbox queue with rate limiting and deduplication
- `bridge.py` — `MattermostTransport` + `MattermostPresenter`
- `loop.py` — WebSocket event loop with `ChatSessionStore` for resume
- `parsing.py` — WebSocket events → typed messages
- `backend.py` — `TransportBackend` entry point

### Telegram Transport (`src/tunapi/telegram/`)

The original transport from takopi. Uses long-polling, inline keyboard for cancel, supports topics, voice notes, and file transfer.

### Engines (`src/tunapi/runners/`)

Each subclasses `JsonlSubprocessRunner`: `claude.py`, `codex.py`, `gemini.py`, `opencode.py`, `pi.py`

### Plugin System (`plugins.py`)

Entry-point groups in `pyproject.toml`:
- `tunapi.engine_backends` — claude, codex, gemini, opencode, pi
- `tunapi.transport_backends` — telegram, mattermost

### Configuration (`settings.py`, `config.py`)

Pydantic settings from `~/.tunapi/tunapi.toml`. Env prefix: `TUNAPI__`. `MATTERMOST_TOKEN` env var supported for Mattermost token; `TELEGRAM_TOKEN` for Telegram. Per-project `chat_id` maps channels (Mattermost) or chats/topics (Telegram) to engines.

## Test Patterns

- Fakes: `tests/telegram_fakes.py`, event factories: `tests/factories.py`
- Coverage threshold: 81% (pytest-cov)
- Python 3.14+ required
