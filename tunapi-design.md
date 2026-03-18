# tunapi 프로젝트 설계서

> takopi 포크 — Telegram transport를 Mattermost transport로 교체

## 0. 핵심 전략

takopi를 포크한 뒤 `src/takopi/telegram/` 디렉토리를 `src/takopi/mattermost/`로 **교체**하고, 설정 모델만 수정한다. takopi의 코어(`runner.py`, `runner_bridge.py`, `transport.py`, `transport_runtime.py`, `engines/`)는 **그대로 유지**.

```
takopi 코어 (건드리지 않음)          tunapi에서 교체하는 부분
─────────────────────────          ──────────────────────────
runner.py                          telegram/ → mattermost/
runner_bridge.py                   settings.py (transport 설정부분)
transport.py (Protocol)            pyproject.toml (entrypoint)
transport_runtime.py               config.py (chat_map 확장)
presenter.py (Protocol)
model.py, events.py
runners/ (claude, codex, pi...)
```

---

## 1. Mattermost Transport 파일 구조

```
src/takopi/mattermost/
├── __init__.py
├── backend.py          # MattermostBackend(TransportBackend) — 진입점
├── bridge.py           # MattermostTransport(Transport) + MattermostPresenter(Presenter)
├── client.py           # MattermostClient — REST API + WebSocket 래퍼
├── client_api.py       # HttpMattermostClient — 저수준 HTTP/WS 호출
├── api_models.py       # Post, Channel, User, WebSocketEvent 등 데이터 모델
├── outbox.py           # MattermostOutbox — 메시지 큐 + 레이트 리밋
├── loop.py             # 메인 이벤트 루프 (WebSocket 수신 → 명령 분배)
├── render.py           # ProgressState → Mattermost Markdown 변환
├── parsing.py          # WebSocket 이벤트 → IncomingMessage 파싱
├── onboarding.py       # check_setup / interactive_setup
├── types.py            # MattermostIncomingMessage, MattermostCallbackAction 등
└── commands/           # 내장 명령어 핸들러
    ├── __init__.py
    ├── handlers.py     # /model, /agent, /new, /cancel 등 분배
    ├── cancel.py       # 취소 처리
    └── file_transfer.py # 파일 업로드/다운로드
```

---

## 2. Telegram ↔ Mattermost API 매핑

### 2-1. 통신 모델

| 항목 | Telegram (takopi 현재) | Mattermost (tunapi) |
|------|----------------------|---------------------|
| **이벤트 수신** | Long-polling (`getUpdates`) | **WebSocket** (`/api/v4/websocket`) |
| **메시지 전송** | `sendMessage` | `POST /api/v4/posts` |
| **메시지 편집** | `editMessageText` | `PUT /api/v4/posts/{id}` |
| **메시지 삭제** | `deleteMessage` | `DELETE /api/v4/posts/{id}` |
| **인증** | Bot Token | Personal Access Token 또는 Bot Account Token |
| **포맷팅** | Entities 배열 (커스텀) | **Markdown** (네이티브 지원) |
| **버튼 (취소 등)** | Inline Keyboard + Callback Query | Interactive Message Attachments (`actions`) |
| **스레드** | `message_thread_id` (Forum Topics) | `root_id` (Reply Thread) |
| **채널 ID** | `chat_id: int` | `channel_id: str` (26자 ID) |
| **파일** | `sendDocument` / `getFile` | `POST /api/v4/files` + file_ids in post |
| **레이트 리밋** | 1 RPS (private), 20/min (group) | 서버 설정 의존, 일반적으로 10 RPS |

### 2-2. Transport Protocol 매핑

```python
class MattermostTransport(Transport):

    async def send(self, *, channel_id, message, options):
        # channel_id: str (MM 채널 ID)
        # message.text: 이미 Markdown — 그대로 사용
        # message.extra["props"]: MM attachment/actions (취소 버튼 등)
        # options.reply_to → root_id (스레드 답글)
        # options.replace → 기존 post 삭제 후 새로 생성
        # → POST /api/v4/posts
        # → return MessageRef(channel_id, post_id)

    async def edit(self, *, ref, message, wait):
        # ref.message_id: post_id
        # → PUT /api/v4/posts/{post_id}
        # → return MessageRef

    async def delete(self, *, ref):
        # → DELETE /api/v4/posts/{post_id}
```

### 2-3. Presenter 매핑

```python
class MattermostPresenter(Presenter):

    def render_progress(self, state, *, elapsed_s, label):
        # ProgressState → Markdown 텍스트 (코드블록, 볼드 등)
        # extra["props"] = {"attachments": [{"actions": [cancel_button]}]}
        # → RenderedMessage(text=markdown, extra={"props": ...})

    def render_final(self, state, *, elapsed_s, status, answer):
        # 최종 답변 → Markdown
        # 긴 답변: message_overflow="split" 시 followups로 분할
        # extra["props"] = {} (버튼 제거)
        # → RenderedMessage(text=markdown, extra={"props": ...})
```

**핵심 차이**: Telegram은 entities 배열로 서식을 표현하지만, Mattermost는 Markdown 네이티브이므로 `render.py`가 **훨씬 단순**해진다. takopi의 `MarkdownFormatter`가 이미 Markdown parts를 생성하므로, `prepare_telegram()` 같은 변환 없이 거의 직접 사용 가능.

---

## 3. 채널 → 프로젝트 매핑 설정

### 3-1. 설정 파일 구조 (`~/.takopi/takopi.toml`)

```toml
# tunapi 설정 예시
transport = "mattermost"
default_engine = "claude"

[transports.mattermost]
url = "https://mm.company.com"           # Mattermost 서버 URL
token = "xxxxxxxxxxxxxxxxxxxxxxxxxxxx"    # Bot 또는 Personal Access Token
team_id = "abcdef1234567890abcdef1234"   # 기본 팀 ID
bot_user_id = ""                         # 자동 감지 (get_me)

# 봇이 응답할 채널 목록 (화이트리스트)
# 비어있으면 모든 DM + 멘션에 응답
allowed_channel_ids = [
    "channel_id_1",
    "channel_id_2",
]

# 봇 사용 허가 유저 (비어있으면 모든 유저)
allowed_user_ids = []

session_mode = "stateless"       # "stateless" | "chat"
message_overflow = "split"       # "trim" | "split"

# 프로젝트별 채널 매핑
[projects.backend]
path = "/home/user/projects/backend-api"
default_engine = "claude"
channel_id = "channel_id_for_backend"    # ← 이 채널에서 오는 메시지는 이 프로젝트에서 실행

[projects.frontend]
path = "/home/user/projects/frontend-app"
default_engine = "codex"
channel_id = "channel_id_for_frontend"

[projects.infra]
path = "/home/user/projects/infra"
default_engine = "claude"
channel_id = "channel_id_for_infra"
```

### 3-2. Settings 모델 변경

```python
# settings.py에 추가

class MattermostTransportSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: NonEmptyStr                              # MM 서버 URL
    token: NonEmptyStr                            # API 토큰
    team_id: NonEmptyStr                          # 기본 팀
    bot_user_id: str = ""                         # 자동 감지
    allowed_channel_ids: list[str] = []           # 허용 채널
    allowed_user_ids: list[str] = []              # 허용 유저
    session_mode: Literal["stateless", "chat"] = "stateless"
    message_overflow: Literal["trim", "split"] = "split"

class TransportsSettings(BaseModel):
    telegram: TelegramTransportSettings | None = None
    mattermost: MattermostTransportSettings | None = None   # ← 추가
    model_config = ConfigDict(extra="allow")
```

### 3-3. 채널 매핑 흐름

```
사용자가 #backend 채널에서 메시지 전송
    ↓
WebSocket 이벤트 수신 (channel_id = "abc123...")
    ↓
config.chat_map[channel_id] → "backend" 프로젝트
    ↓
ProjectConfig.path = "/home/user/projects/backend-api"
ProjectConfig.default_engine = "claude"
    ↓
runner가 해당 디렉토리에서 claude CLI 실행
```

takopi의 `ProjectsConfig.chat_map`은 현재 `dict[int, str]`인데, Mattermost의 channel_id는 문자열이므로 `dict[int | str, str]`로 확장하거나, `config.py`의 `project_for_chat()`을 오버라이드.

---

## 4. 핵심 컴포넌트 상세 설계

### 4-1. `client_api.py` — 저수준 HTTP/WS

```python
class HttpMattermostClient:
    """Mattermost REST API + WebSocket 클라이언트"""

    def __init__(self, url: str, token: str, *, http_client: httpx.AsyncClient | None = None):
        self._base_url = url.rstrip("/")
        self._token = token  # Authorization: Bearer {token}

    # REST API
    async def create_post(self, channel_id, message, root_id=None, props=None) -> Post
    async def update_post(self, post_id, message, props=None) -> Post
    async def delete_post(self, post_id) -> bool
    async def get_me(self) -> User
    async def upload_file(self, channel_id, filename, content) -> FileInfo

    # WebSocket
    async def websocket_connect(self) -> AsyncIterator[WebSocketEvent]:
        # wss://{url}/api/v4/websocket
        # 인증: {"seq": 1, "action": "authentication", "data": {"token": ...}}
        # 수신: {"event": "posted", "data": {"post": "..."}} 등
```

### 4-2. `client.py` — Outbox 래퍼

takopi의 `TelegramClient`와 동일한 패턴:

```python
class MattermostClient:
    def __init__(self, url, token):
        self._client = HttpMattermostClient(url, token)
        self._outbox = MattermostOutbox(...)  # 레이트 리밋 큐

    async def send_message(self, channel_id, text, root_id=None, props=None) -> Post
    async def edit_message(self, post_id, text, props=None) -> Post
    async def delete_message(self, post_id) -> bool
    async def websocket_events(self) -> AsyncIterator[WebSocketEvent]
```

### 4-3. `loop.py` — 메인 이벤트 루프

Telegram의 `poll_updates()`를 WebSocket 기반으로 교체:

```python
async def run_main_loop(cfg: MattermostBridgeConfig, ...):
    # 1. WebSocket 연결
    # 2. 시작 메시지 전송
    # 3. 이벤트 루프:
    async for event in cfg.bot.websocket_events():
        match event.event:
            case "posted":
                msg = parse_posted_event(event)
                if should_handle(msg, cfg):  # 봇 멘션 or DM or 허용 채널
                    await dispatch(msg, cfg, running_tasks)
            case "post_edited":
                pass  # 무시 또는 처리
            case "custom_tunapi_cancel":
                await handle_cancel(...)
```

**봇 트리거 방식** (Telegram과의 차이):

| Telegram | Mattermost |
|----------|------------|
| DM으로 보내면 무조건 처리 | DM으로 보내면 무조건 처리 |
| 그룹에서 봇 멘션 불필요 | **채널에서 `@bot` 멘션** 또는 허용 채널이면 모든 메시지 |
| `/command` 형식 | `/command` 형식 동일 (MM 슬래시 커맨드와 별도) |

### 4-4. 취소 구현

Telegram의 Inline Keyboard → Mattermost 대안:

```python
# Telegram (현재)
CANCEL_MARKUP = {
    "inline_keyboard": [[{"text": "cancel", "callback_data": "takopi:cancel"}]]
}

# Mattermost (tunapi) — 이모지 반응 방식 (권장)
# WebSocket에서 "reaction_added" 이벤트 감지
case "reaction_added":
    if event.data.emoji_name == "octagonal_sign":  # 🛑
        await handle_cancel_by_reaction(event)
```

초기 구현에서는 이모지 반응(`🛑`)으로 취소 감지하는 것이 실용적. Mattermost Interactive Message는 외부 Integration URL이 필요해서 서버 설정이 복잡해질 수 있음.

---

## 5. takopi 코어 수정 최소화 전략

### 수정이 필요한 코어 파일

| 파일 | 변경 | 이유 |
|------|------|------|
| `settings.py` | `MattermostTransportSettings` 추가, `TransportsSettings`에 필드 추가 | transport 설정 로드 |
| `config.py` | `ProjectsConfig.chat_map` 타입을 `dict[int \| str, str]`로 확장 | MM channel_id가 str |
| `config.py` | `project_for_chat()` 시그니처를 `chat_id: int \| str \| None`로 변경 | 동일 |
| `pyproject.toml` | entrypoint에 `mattermost = "takopi.mattermost.backend:BACKEND"` 추가 | transport 등록 |

### 수정하지 않는 파일

- `transport.py` — `ChannelId = int | str` 이미 str 지원
- `runner.py`, `runner_bridge.py` — 엔진 실행 로직 무관
- `transport_runtime.py` — `resolve_message(chat_id=...)` 이미 `int | None`이나, `int | str | None`으로 변경 필요
- `presenter.py` — Protocol이므로 수정 불필요
- `runners/` — 전혀 무관

---

## 6. 전체 아키텍처 다이어그램

```
                    ┌─────────────────────────────────────────┐
                    │           Mattermost Server             │
                    │                                         │
                    │  #backend   #frontend   #infra   DM     │
                    └────┬──────────┬──────────┬───────┬──────┘
                         │ WebSocket│          │       │
                    ┌────▼──────────▼──────────▼───────▼──────┐
                    │         tunapi (takopi fork)             │
                    │                                         │
                    │  ┌──────────────────────────────────┐   │
                    │  │  mattermost/loop.py               │   │
                    │  │  WebSocket 수신 → 메시지 분류     │   │
                    │  └──────────┬───────────────────────┘   │
                    │             │                            │
                    │  ┌──────────▼───────────────────────┐   │
                    │  │  transport_runtime.py (코어)      │   │
                    │  │  channel_id → project 매핑        │   │
                    │  │  resolve_message() → 엔진 결정    │   │
                    │  │  resolve_runner() → runner 획득    │   │
                    │  └──────────┬───────────────────────┘   │
                    │             │                            │
                    │  ┌──────────▼───────────────────────┐   │
                    │  │  runner_bridge.py (코어)           │   │
                    │  │  handle_message()                  │   │
                    │  │  → subprocess spawn                │   │
                    │  │  → JSONL 스트리밍                  │   │
                    │  │  → ProgressEdits (실시간 편집)     │   │
                    │  └──────────┬───────────────────────┘   │
                    │             │                            │
                    │  ┌──────────▼───────────────────────┐   │
                    │  │  mattermost/bridge.py              │   │
                    │  │  MattermostTransport.send/edit     │   │
                    │  │  MattermostPresenter.render        │   │
                    │  └──────────┬───────────────────────┘   │
                    │             │ REST API                   │
                    └─────────────┼───────────────────────────┘
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
         claude CLI          codex CLI           gemini CLI
         (subprocess)        (subprocess)        (subprocess)
```

---

## 7. 구현 우선순위

| 단계 | 파일 | 설명 |
|------|------|------|
| **1단계** | `api_models.py`, `client_api.py` | MM REST API + WebSocket 기본 통신 |
| **2단계** | `client.py`, `outbox.py` | Outbox 큐 + 레이트 리밋 |
| **3단계** | `bridge.py`, `render.py` | Transport + Presenter 구현 |
| **4단계** | `loop.py`, `parsing.py`, `types.py` | WebSocket 이벤트 루프 |
| **5단계** | `backend.py`, `onboarding.py` | TransportBackend + 설정 검증 |
| **6단계** | 코어 수정 (`settings.py`, `config.py`, `pyproject.toml`) | 통합 |
| **7단계** | `commands/` | 내장 명령어 (/cancel, /model, /agent 등) |

예상 코드량: **1,200~1,500줄** (Telegram transport ~2,000줄에서 entities 변환 로직이 빠지므로 더 작음)
