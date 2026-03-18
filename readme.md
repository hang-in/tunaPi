# tunapi

Mattermost bridge for Codex, Claude Code, and other agent CLIs. forked from [takopi](https://github.com/banteg/takopi) — replacing Telegram transport with Mattermost.

## features

- projects and worktrees: work on multiple repos/branches simultaneously, branches are git worktrees
- stateless resume: continue in chat or copy the resume line to pick up in terminal
- progress streaming: commands, tools, file changes, elapsed time
- parallel runs across agent sessions, per-agent-session queue
- Mattermost native: WebSocket events, Markdown rendering, channel-to-project mapping
- file transfer: send files to the repo or fetch files/dirs back
- works with existing anthropic and openai subscriptions

## requirements

`uv` for installation (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

python 3.14+ (`uv python install 3.14`)

at least one engine on PATH: `codex`, `claude`, `opencode`, or `pi`

## install

```sh
uv tool install -U tunapi
```

## setup

run `tunapi` and follow the setup wizard. it will help you:

1. configure your Mattermost server URL and API token
2. pick a default engine
3. map channels to projects

## usage

```sh
cd ~/dev/happy-gadgets
tunapi
```

send a message to your bot. prefix with `/codex`, `/claude`, `/opencode`, or `/pi` to pick an engine. reply to continue a thread.

register a project with `tunapi init happy-gadgets`, then target it from anywhere with `/happy-gadgets hard reset the timeline`.

mention a branch to run an agent in a dedicated worktree `/happy-gadgets @feat/memory-box freeze artifacts forever`.

inspect or update settings with `tunapi config list`, `tunapi config get`, and `tunapi config set`.

see [tunapi.dev](https://tunapi.dev/) for configuration, worktrees, topics, file transfer, and more.

## plugins

tunapi supports entrypoint-based plugins for engines, transports, and commands.

see [`docs/how-to/write-a-plugin.md`](docs/how-to/write-a-plugin.md) and [`docs/reference/plugin-api.md`](docs/reference/plugin-api.md).

## development

see [`docs/reference/specification.md`](docs/reference/specification.md) and [`docs/developing.md`](docs/developing.md).

