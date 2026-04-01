<div align="center">

# tunaPi

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](../LICENSE)
[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/github/actions/workflow/status/hang-in/tunaPi/release.yml?label=tests)](https://github.com/hang-in/tunaPi/actions)
[![GitHub release](https://img.shields.io/github/v/release/hang-in/tunaPi?include_prereleases)](https://github.com/hang-in/tunaPi/releases)

A bridge that lets you run AI coding agents from your chat app

Discord · Mattermost · Slack · Telegram · Web

**Claude Code · Codex · Gemini CLI · OpenCode · Pi**

Paste into your agent:

> Clone and install tunaPi (https://github.com/hang-in/tunaPi). Ask which chat app I use and create ~/.tunapi/tunapi.toml. Don't ask for bot tokens directly — just tell me where to fill them in the config file. Verify with tunapi doctor, fix any issues, and run it.

[한국어](../readme.md) | [**English**](#english) | [日本語](README_JP.md)

</div>

---

## English

### Background

Forked from [takopi](https://github.com/banteg/takopi) to use it with Mattermost and Slack instead of Telegram. Features accumulated from there.

### How it works

```
chat message → tunaPi → runs AI on your machine → sends result back to chat
```

What it looks like in chat:

```
you:     fix the login bug

tunaPi:  working · claude/opus4.6 · 0s · step 1
         ↳ Reading src/auth/login.py...

tunaPi:  working · claude/opus4.6 · 12s · step 4
         ↳ Writing fix...

tunaPi:  ✓ done · 23s · 3 files changed
         Fixed token expiry handling in login.py.
```

### When is this useful?

- You want to trigger AI work from chat without switching to a terminal
- You want separate projects in separate chat rooms
- You want to control your work machine from your phone
- You want multiple AIs to debate a topic

### Features

- **Multi-agent roundtable** — `!rt "topic"` runs Claude, Gemini, and Codex in turn
- **Conversation branching** — fork from any message into an independent thread, adopt back to main
- **Code indexing** — rawq-based semantic search, auto-attach relevant code to messages (tunaDish only)
- **Per-channel project and engine mapping** — each channel can use a different project and AI
- **Live progress display** — `working · claude/opus4.6 · 12s · step 4` in chat
- **Session resumption** — context is preserved between conversations
- **Model-level selection** — `!model claude claude-opus-4-6` sets the exact model, not just the engine
- **Cross-session context** — auto-summarize activity from other sessions in the same project

### Test status

- Tests: 3,538
- Coverage: 81%

### Works with

Discord · Mattermost · Slack · Telegram · [tunaDish](https://github.com/hang-in/tunaDish) (web client)

### Feature comparison by transport

| Feature | Discord | Mattermost | Slack | Telegram | tunaDish |
|---------|:-------:|:----------:|:-----:|:--------:|:-------:|
| Multi-agent roundtable (`!rt`) | O | O | O | O | O |
| Conversation branching | O | O | O | — | O |
| Session resumption | O | O | O | O | O |
| Live progress display | O | O | O | O | O |
| Engine/model switching | O | O | O | O | O |
| File transfer | O | O | O | O | — |
| Voice transcription | O | O | O | O | — |
| Personas | — | O | O | O | O |
| Code indexing (rawq) | — | — | — | — | O |
| Cross-session context | — | — | — | — | O |
| Slash commands | O | — | — | O | — |

### Runs these AI tools

Claude Code · Codex · Gemini CLI · OpenCode · Pi

### Install

```sh
uv tool install -U tunapi
```

For Discord:

```sh
uv tool install -U "tunapi[discord]"         # Discord basic
uv tool install -U "tunapi[discord-voice]"   # + Whisper voice transcription
```

From source:

```sh
git clone https://github.com/hang-in/tunaPi.git
cd tunaPi
uv tool install -e .
```

### Requirements

- Python 3.14+
- `uv`
- at least one of: `claude` / `codex` / `gemini` / `opencode` / `pi`

### Setup

`~/.tunapi/tunapi.toml`

<details>
<summary><b>Slack</b> — <a href="how-to/setup-slack.md">bot setup guide</a></summary>

```toml
transport = "slack"
default_engine = "claude"

[transports.slack]
bot_token = "xoxb-..."
app_token = "xapp-..."
channel_id = "C0123456789"
```
</details>

<details>
<summary><b>Mattermost</b> — <a href="how-to/setup-mattermost.md">bot setup guide</a></summary>

```toml
transport = "mattermost"
default_engine = "claude"

[transports.mattermost]
url = "https://mm.example.com"
token = "YOUR_TOKEN"
channel_id = "YOUR_CHANNEL_ID"
```
</details>

<details>
<summary><b>Telegram</b> — <a href="how-to/setup-telegram.md">bot setup guide</a></summary>

```toml
transport = "telegram"
default_engine = "claude"

[transports.telegram]
bot_token = "YOUR_BOT_TOKEN"
chat_id = 123456789
```
</details>

<details>
<summary><b>Discord</b> — <a href="how-to/setup-discord.md">bot setup guide</a></summary>

```toml
transport = "discord"
default_engine = "claude"

[transports.discord]
bot_token = "YOUR_BOT_TOKEN"
guild_id = 123456789            # optional — restrict to a specific server
session_mode = "chat"           # "stateless" | "chat"
trigger_mode_default = "all"    # "all" | "mentions"
```
</details>

### Run

```sh
tunapi
```

Check your setup:

```sh
tunapi doctor
```

### Install via agent

Paste the prompt below into Claude Code, Codex, Gemini CLI, etc. and the agent will install and configure everything for you.

> Install and set up tunaPi.
>
> 1. Run `uv tool install -U tunapi` (for Discord: `"tunapi[discord]"`)
> 2. Create `~/.tunapi/tunapi.toml` if it doesn't exist
> 3. Ask which chat app I use (`slack` / `mattermost` / `telegram` / `discord`) and write the transport config block
> 4. Don't ask for bot tokens directly — tell me where to fill them in the file
> 5. Ask which default AI engine (`claude` / `codex` / `gemini` / `opencode` / `pi`) and set `default_engine`
> 6. Validate with `tunapi doctor`
> 7. Fix any issues, then run `tunapi`

### Commands

| What you want | Example |
|---|---|
| Ask for work | `fix this bug` |
| Switch engine | `!model codex` |
| Set model | `!model claude claude-opus-4-6` |
| List available models | `!models` |
| Bind a project | `!project set my-project` |
| Multi-agent roundtable | `!rt "architecture review" --rounds 2` |
| Start fresh | `!new` |
| Cancel a run | `!cancel` or 🛑 reaction |
| Check status | `!status` |
| See all commands | `!help` |

### Note

- Image files can be transferred, but image content is not analyzed.
- Full docs: [docs/index.md](index.md)

### Credit

[takopi](https://github.com/banteg/takopi) — this project started as a fork.

### License

MIT — [LICENSE](../LICENSE)
