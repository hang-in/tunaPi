# Repo map

Quick pointers for navigating the Tunapi codebase.

## Where things start

- CLI entry point: `src/tunapi/cli.py`
- Telegram backend entry point: `src/tunapi/telegram/backend.py`
- Telegram bridge loop: `src/tunapi/telegram/bridge.py`
- Transport-agnostic handler: `src/tunapi/runner_bridge.py`

## Core concepts

- Domain types (resume tokens, events, actions): `src/tunapi/model.py`
- Runner protocol: `src/tunapi/runner.py`
- Router selection and resume polling: `src/tunapi/router.py`
- Per-thread scheduling: `src/tunapi/scheduler.py`
- Progress reduction and rendering: `src/tunapi/progress.py`, `src/tunapi/markdown.py`

## Engines and streaming

- Runner implementations: `src/tunapi/runners/*`
- JSONL decoding schemas: `src/tunapi/schemas/*`

## Plugins

- Public API boundary (`tunapi.api`): `src/tunapi/api.py`
- Entrypoint discovery + lazy loading: `src/tunapi/plugins.py`
- Engine/transport/command backend loading: `src/tunapi/engines.py`, `src/tunapi/transports.py`, `src/tunapi/commands.py`

## Configuration

- Settings model + TOML/env loading: `src/tunapi/settings.py`
- Config migrations: `src/tunapi/config_migrations.py`

## Docs and contracts

- Normative behavior: [Specification](../specification.md)
- Runner invariants: `tests/test_runner_contract.py`

