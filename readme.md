<div align="center">

# tunaPi

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/github/actions/workflow/status/hang-in/tunaPi/release.yml?label=tests)](https://github.com/hang-in/tunaPi/actions)
[![GitHub release](https://img.shields.io/github/v/release/hang-in/tunaPi?include_prereleases)](https://github.com/hang-in/tunaPi/releases)

채팅앱에서 AI 코딩 에이전트를 바로 쓰게 해주는 브릿지

Discord · Mattermost · Slack · Telegram · Web

**Claude Code · Codex · Gemini CLI · OpenCode · Pi**

에이전트에 붙여넣기:

> tunaPi(https://github.com/hang-in/tunaPi)를 클론하고 설치해 줘. 내가 쓸 채팅앱을 물어보고 ~/.tunapi/tunapi.toml을 만들어. 봇 토큰 등 시크릿은 직접 입력하지 말고 파일에서 어디를 채우면 되는지 안내해 줘. tunapi doctor로 검증하고 문제 있으면 고쳐서 실행까지 해 줘.

[**한국어**](#한국어) | [English](docs/README_EN.md) | [日本語](docs/README_JP.md)

</div>

---

## 한국어

### 배경

[takopi](https://github.com/banteg/takopi)를 Telegram 대신 Mattermost/Slack에서 쓰고 싶어서 포크했습니다. 쓰다 보니 기능이 붙었습니다.

### 어떻게 동작하나요?

```
채팅 메시지 → tunaPi → 내 컴퓨터에서 AI 실행 → 결과를 채팅으로 반환
```

실제로 채팅창에서 보이는 모습:

```
나:      로그인 버그 고쳐줘

tunaPi:  working · claude/opus4.6 · 0s · step 1
         ↳ Reading src/auth/login.py...

tunaPi:  working · claude/opus4.6 · 12s · step 4
         ↳ Writing fix...

tunaPi:  ✓ done · 23s · 3 files changed
         login.py의 토큰 만료 처리 로직을 수정했습니다.
```

### 이런 때 좋아요

- 터미널 대신 채팅으로 AI에게 일을 시키고 싶을 때
- 여러 프로젝트를 채팅방별로 나눠서 관리하고 싶을 때
- 밖에서 휴대폰으로 내 작업 PC를 다루고 싶을 때
- 여러 AI를 같은 주제로 토론시키고 싶을 때

### 주요 기능

- **멀티 에이전트 토론** — `!rt "주제"` 로 Claude, Gemini, Codex가 순서대로 의견을 냄
- **대화 브랜칭** — 특정 메시지에서 분기하여 독립 대화 → 채택(adopt)으로 메인에 병합
- **코드 인덱싱** — rawq 기반 시맨틱 검색, 메시지에 관련 코드 자동 첨부 (tunaDish 전용)
- **채널별 프로젝트/엔진 매핑** — 채널마다 다른 프로젝트, 다른 AI를 쓸 수 있음
- **실시간 진행 표시** — `working · claude/opus4.6 · 12s · step 4` 형태로 진행 상황 표시
- **세션 이어가기** — 대화를 끊었다 다시 이어도 컨텍스트 유지
- **세부 모델 설정** — `!model claude claude-opus-4-6` 으로 엔진뿐 아니라 모델까지 지정
- **크로스 세션 컨텍스트** — 같은 프로젝트의 다른 세션 활동을 자동 요약하여 전달

### 테스트 현황

- 테스트: 1,237개
- 커버리지: 61%

### 지원하는 채팅 앱

Discord · Mattermost · Slack · Telegram · [tunaDish](https://github.com/hang-in/tunaDish) (웹 클라이언트)

### 지원하는 AI 도구

Claude Code · Codex · Gemini CLI · OpenCode · Pi

### 설치

```sh
uv tool install -U tunapi
```

Discord를 쓸 경우:

```sh
uv tool install -U "tunapi[discord]"         # Discord 기본
uv tool install -U "tunapi[discord-voice]"   # + Whisper 음성 전사
```

소스에서:

```sh
git clone https://github.com/hang-in/tunaPi.git
cd tunaPi
uv tool install -e .
```

### 준비물

- Python 3.14+
- `uv`
- `claude` / `codex` / `gemini` / `opencode` / `pi` 중 하나 이상

### 설정

`~/.tunapi/tunapi.toml`

<details>
<summary><b>Slack</b></summary>

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
<summary><b>Mattermost</b></summary>

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
<summary><b>Telegram</b></summary>

```toml
transport = "telegram"
default_engine = "claude"

[transports.telegram]
bot_token = "YOUR_BOT_TOKEN"
chat_id = 123456789
```
</details>

<details>
<summary><b>Discord</b></summary>

```toml
transport = "discord"
default_engine = "claude"

[transports.discord]
bot_token = "YOUR_BOT_TOKEN"
guild_id = 123456789            # 선택 — 특정 서버 제한
session_mode = "chat"           # "stateless" | "chat"
trigger_mode_default = "all"    # "all" | "mentions"
```
</details>

### 실행

```sh
tunapi
```

설정 확인:

```sh
tunapi doctor
```

### 에이전트로 설치하기

아래 프롬프트를 Claude Code, Codex, Gemini CLI 등에 붙여넣으면 에이전트가 직접 설치·설정해 줍니다.

> tunaPi를 설치하고 설정해 줘.
>
> 1. `uv tool install -U tunapi` 실행 (Discord면 `"tunapi[discord]"`)
> 2. `~/.tunapi/tunapi.toml` 파일이 없으면 생성
> 3. 내가 쓸 채팅앱(`slack` / `mattermost` / `telegram` / `discord`)을 물어보고 해당 transport 설정 블록을 작성
> 4. 봇 토큰, 채널 ID 등 필요한 값을 물어보고 채워 넣기
> 5. 기본 AI 엔진(`claude` / `codex` / `gemini` / `opencode` / `pi`)을 물어보고 `default_engine` 설정
> 6. `tunapi doctor`로 설정 검증
> 7. 문제가 있으면 수정, 없으면 `tunapi`로 실행

### 자주 쓰는 커맨드

| 하고 싶은 일 | 예시 |
|---|---|
| AI에게 작업 요청 | `버그 고쳐줘` |
| 엔진 바꾸기 | `!model codex` |
| 세부 모델 지정 | `!model claude claude-opus-4-6` |
| 사용 가능한 모델 목록 | `!models` |
| 프로젝트 지정 | `!project set my-project` |
| 멀티 에이전트 토론 | `!rt "아키텍처 검토" --rounds 2` |
| 새 대화 시작 | `!new` |
| 실행 취소 | `!cancel` 또는 🛑 반응 |
| 현재 상태 확인 | `!status` |
| 전체 커맨드 보기 | `!help` |

### 참고

- 이미지 파일 전달은 가능하지만, 이미지 내용을 분석하지는 않습니다.
- 더 자세한 사용법: [docs/index.md](docs/index.md)

### 감사

[takopi](https://github.com/banteg/takopi) — 이 프로젝트의 출발점입니다.

### 라이선스

MIT — [LICENSE](LICENSE)
