# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Response Style (when running via tunapi/Mattermost)

- 핵심만 짧게 답변
- Mattermost Markdown 형식으로 작성

## Project Overview

tunapi is a Mattermost bridge for agent CLIs (Claude Code, Codex, Gemini CLI, OpenCode, Pi), forked from [takopi](https://github.com/banteg/takopi). It replaces the Telegram transport with Mattermost while keeping takopi's core intact.

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
Mattermost WebSocket → Transport (parse) → TransportRuntime (resolve engine/project)
    → Runner (spawn agent CLI, stream JSONL) → RunnerBridge (track progress, send updates)
    → Presenter (render Markdown) → Transport (send/edit messages back)
```

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

### Engines (`src/tunapi/runners/`)

Each subclasses `JsonlSubprocessRunner`: `claude.py`, `codex.py`, `gemini.py`, `opencode.py`, `pi.py`

### Plugin System (`plugins.py`)

Entry-point groups in `pyproject.toml`:
- `tunapi.engine_backends` — claude, codex, gemini, opencode, pi
- `tunapi.transport_backends` — telegram, mattermost

### Configuration (`settings.py`, `config.py`)

Pydantic settings from `~/.tunapi/tunapi.toml`. Env prefix: `TUNAPI__`. `MATTERMOST_TOKEN` env var supported for token. Per-project `chat_id` maps channels to engines.

## Test Patterns

- Fakes: `tests/telegram_fakes.py`, event factories: `tests/factories.py`
- Coverage threshold: 81% (pytest-cov)
- Python 3.14+ required
