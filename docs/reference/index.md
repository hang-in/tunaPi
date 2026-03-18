# Reference

Reference docs are **authoritative and exact**. Use these when you need stable facts, schemas, and contracts.

If you’re trying to achieve a goal (“enable topics”, “fetch a file”), use **[How-to](../how-to/index.md)**.  
If you’re trying to understand the *why*, use **[Explanation](../explanation/index.md)**.

## Most-used reference pages

- [Commands & directives](commands-and-directives.md)
  - Message prefixes like `/<engine-id>`, `/<project-alias>`, and `@branch`
  - In-chat commands like `/cancel`, `/new`, `/ctx`, `/file …`, `/topic …`
- [Configuration](config.md)
  - `tunapi.toml` options and defaults
  - Telegram transport options (sessions, topics, files, voice transcription)

## Normative behavior

- [Specification](specification.md)  
  The normative (“MUST/SHOULD/MAY”) contract for:
  - resume tokens + resume lines
  - event model
  - progress/final message semantics
  - per-thread serialization rules

## Plugins and extension contracts

- [Plugin API](plugin-api.md)  
  The **only** supported import surface for plugins: `tunapi.api`
- [Context resolution](context-resolution.md)  
  How Tunapi resolves project + worktree context from directives, replies, and chat ids.

## Transport reference

- [Telegram transport](transports/telegram.md)  
  Rate limits, outbox behavior, retries, message editing rules.

## Runner reference

These are “engine adapter” implementation details: JSONL formats, mapping rules, and emitted events.

- [Runners overview](runners/index.md)
- Claude:
  - [runner.md](runners/claude/runner.md)
  - [stream-json-cheatsheet.md](runners/claude/stream-json-cheatsheet.md)
  - [tunapi-events.md](runners/claude/tunapi-events.md)
- Codex:
  - [exec-json-cheatsheet.md](runners/codex/exec-json-cheatsheet.md)
  - [tunapi-events.md](runners/codex/tunapi-events.md)
- OpenCode:
  - [runner.md](runners/opencode/runner.md)
  - [stream-json-cheatsheet.md](runners/opencode/stream-json-cheatsheet.md)
  - [tunapi-events.md](runners/opencode/tunapi-events.md)
- Pi:
  - [runner.md](runners/pi/runner.md)
  - [stream-json-cheatsheet.md](runners/pi/stream-json-cheatsheet.md)
  - [tunapi-events.md](runners/pi/tunapi-events.md)

## For LLM agents

If you’re an LLM agent contributing to Tunapi, start here:

- [Agent entrypoint](agents/index.md)
- [Repo map](agents/repo-map.md)
- [Invariants](agents/invariants.md) (runner contract, resume handling, “don’t break this” rules)
