<div align="center">

# tunaPi

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/github/actions/workflow/status/hang-in/tunaPi/release.yml?label=tests)](https://github.com/hang-in/tunaPi/actions)
[![GitHub release](https://img.shields.io/github/v/release/hang-in/tunaPi?include_prereleases)](https://github.com/hang-in/tunaPi/releases)

채팅앱에서 AI 코딩 도구를 바로 쓰게 해주는 브릿지

[**한국어**](#한국어) | [English](docs/README_EN.md) | [日本語](docs/README_JP.md)

<!-- TODO: 데모 GIF 추가 -->

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
- **코드 인덱싱** — rawq 기반 시맨틱 검색, 메시지에 관련 코드 자동 첨부
- **채널별 프로젝트/엔진 매핑** — 채널마다 다른 프로젝트, 다른 AI를 쓸 수 있음
- **실시간 진행 표시** — `working · claude/opus4.6 · 12s · step 4` 형태로 진행 상황 표시
- **세션 이어가기** — 대화를 끊었다 다시 이어도 컨텍스트 유지
- **세부 모델 설정** — `!model claude claude-opus-4-6` 으로 엔진뿐 아니라 모델까지 지정
- **크로스 세션 컨텍스트** — 같은 프로젝트의 다른 세션 활동을 자동 요약하여 전달

### 테스트 현황

- 테스트: 1,075개
- 커버리지: 61%

### 지원하는 채팅 앱

Mattermost · Slack · Telegram · [tunaDish](https://github.com/hang-in/tunaDish) (웹 클라이언트)

### 지원하는 AI 도구

Claude Code · Codex · Gemini CLI · OpenCode · Pi

### 설치

```sh
uv tool install -U tunapi
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

```toml
transport = "slack"          # mattermost, telegram도 가능
default_engine = "claude"

[transports.slack]
bot_token = "xoxb-..."
app_token = "xapp-..."
channel_id = "C0123456789"
```

```toml
# Mattermost
transport = "mattermost"
default_engine = "claude"

[transports.mattermost]
url = "https://mm.example.com"
token = "YOUR_TOKEN"
channel_id = "YOUR_CHANNEL_ID"
```

```toml
# Telegram
transport = "telegram"
default_engine = "claude"

[transports.telegram]
bot_token = "YOUR_BOT_TOKEN"
chat_id = 123456789
```

### 실행

```sh
tunapi
```

설정 확인:

```sh
tunapi doctor
```

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
