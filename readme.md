# tunapi

Mattermost bridge for coding agent CLIs — run **Claude Code**, **Codex**, **Gemini CLI**, and more from any Mattermost channel.

Forked from [takopi](https://github.com/banteg/takopi), replacing the Telegram transport with Mattermost.

## Features

- **Multi-engine** — Claude, Codex, Gemini, OpenCode, Pi. Map each channel to a different engine
- **Live progress** — stream tool calls, file changes, and elapsed time as the agent works
- **Session resume** — conversations persist across messages via resume tokens (`session_mode = "chat"`)
- **Projects & worktrees** — bind channels to repos; mention a branch to run in a dedicated git worktree
- **Cancel by reaction** — add 🛑 to a progress message to abort a running task
- **Native Markdown** — Mattermost renders responses directly, no entity conversion needed
- **Plugin system** — add engines, transports, or commands via Python entry points

## Requirements

- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Python 3.14+ (`uv python install 3.14`)
- At least one agent CLI on PATH: `claude`, `codex`, `gemini`, `opencode`, or `pi`

## Install

```sh
uv tool install -U tunapi
```

Or run from source:

```sh
git clone https://github.com/hang-in/tunapi.git
cd tunapi
uv tool install -e .
```

## Setup

### 1. Create a Mattermost bot

- **System Console** → **Integrations** → **Bot Accounts** → **Add Bot Account**
- Copy the **Access Token**

### 2. Configure tunapi

Create `~/.tunapi/tunapi.toml`:

```toml
transport = "mattermost"
default_engine = "claude"

[transports.mattermost]
url = "https://mm.example.com"
token = "your-bot-access-token"
channel_id = "default-channel-id"       # bot's DM or a channel ID
show_resume_line = false
session_mode = "chat"                   # "stateless" or "chat"
```

Or use a `.env` file for the token:

```sh
# .env or ~/.tunapi/.env
MATTERMOST_TOKEN=your-bot-access-token
```

### 3. Map channels to engines (optional)

```toml
[projects.backend]
path = "/home/user/projects/backend"
default_engine = "claude"
chat_id = "claude-channel-id"

[projects.infra]
path = "/home/user/projects/infra"
default_engine = "codex"
chat_id = "codex-channel-id"

[projects.research]
path = "/home/user/projects/research"
default_engine = "gemini"
chat_id = "gemini-channel-id"
```

## Usage

```sh
# Run in foreground
tunapi

# Run in background
nohup tunapi > /tmp/tunapi.log 2>&1 &

# Debug mode
tunapi --debug
```

Send a message in any mapped channel. The bot will run the configured engine and reply.

| Action | How |
|--------|-----|
| Pick an engine | `/claude`, `/codex`, `/gemini` prefix |
| Register a project | `tunapi init my-project` |
| Target a project | `/my-project fix the bug` |
| Use a worktree | `/my-project @feat/branch do something` |
| Start a new session | `/new` |
| Cancel a running task | React with 🛑 |
| View config | `tunapi config list` |

## Supported Engines

| Engine | CLI | Status |
|--------|-----|--------|
| Claude Code | `claude` | Built-in |
| Codex | `codex` | Built-in |
| Gemini CLI | `gemini` | Built-in |
| OpenCode | `opencode` | Built-in |
| Pi | `pi` | Built-in |

## Plugins

tunapi supports entry-point plugins for engines, transports, and commands.

See [`docs/how-to/write-a-plugin.md`](docs/how-to/write-a-plugin.md) and [`docs/reference/plugin-api.md`](docs/reference/plugin-api.md).

## Development

```sh
uv sync --dev
just check          # format + lint + typecheck + tests
uv run pytest --no-cov -k "test_name"   # single test
```

See [`docs/reference/specification.md`](docs/reference/specification.md).

## License

MIT — see [LICENSE](LICENSE).
