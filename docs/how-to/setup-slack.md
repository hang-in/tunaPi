# Slack 봇 설정 가이드

Slack 워크스페이스에 tunapi 봇을 연결하는 방법을 안내합니다.

## 1. Slack App 생성

1. [api.slack.com/apps](https://api.slack.com/apps)에 접속합니다.
2. **Create New App** → **From scratch**를 선택합니다.
3. 앱 이름(예: `tunapi`)과 워크스페이스를 지정한 뒤 **Create App**을 클릭합니다.

## 2. Socket Mode 활성화

tunapi는 Socket Mode로 동작합니다. 별도 서버나 공개 URL이 필요 없습니다.

1. 왼쪽 메뉴 **Settings → Socket Mode**로 이동합니다.
2. **Enable Socket Mode**를 켭니다.
3. App-Level Token 이름(예: `tunapi-socket`)을 입력하고, scope로 `connections:write`를 추가합니다.
4. **Generate**를 클릭해 `xapp-` 로 시작하는 토큰을 복사합니다. 이 값이 `app_token`입니다.

## 3. Bot Token Scopes 추가

왼쪽 메뉴 **Features → OAuth & Permissions**로 이동합니다.

**Bot Token Scopes** 섹션에서 다음 scope를 추가합니다:

| Scope | 용도 |
|---|---|
| `chat:write` | 메시지 전송 및 편집 |
| `channels:read` | 채널 목록 조회 |
| `channels:history` | 채널 메시지 읽기 |
| `files:read` | 파일 다운로드 |
| `files:write` | 파일 업로드 |
| `reactions:read` | 리액션 감지 |
| `users:read` | 사용자 정보 조회 |

## 4. Event Subscriptions 설정

1. 왼쪽 메뉴 **Features → Event Subscriptions**로 이동합니다.
2. **Enable Events**를 켭니다.
3. **Subscribe to bot events**에서 다음 이벤트를 추가합니다:
    - `message.channels` — 공개 채널 메시지
    - `message.groups` — 비공개 채널 메시지
    - `message.im` — DM 메시지
    - `app_mention` — 봇 멘션
4. **Save Changes**를 클릭합니다.

## 5. 워크스페이스에 앱 설치

1. 왼쪽 메뉴 **Settings → Install App**으로 이동합니다.
2. **Install to Workspace**를 클릭하고 권한을 승인합니다.
3. `xoxb-`로 시작하는 **Bot User OAuth Token**을 복사합니다. 이 값이 `bot_token`입니다.

## 6. 채널에 봇 초대

봇이 메시지를 받으려면 채널에 초대되어야 합니다.

```
/invite @tunapi
```

## 7. tunapi.toml 설정

`~/.tunapi/tunapi.toml`에 다음을 추가합니다:

```toml
transport = "slack"

[slack]
bot_token = "xoxb-..."
app_token = "xapp-..."

# 선택 옵션
# session_mode = "chat"           # "stateless"(기본값) 또는 "chat"(대화 유지)
# trigger_mode = "mentions"       # "mentions"(기본값, @멘션 시만 응답) 또는 "all"
# message_overflow = "trim"       # "trim"(기본값) 또는 "split"
# show_resume_line = true         # 세션 재개 줄 표시 (기본값: true)
# channel_id = "C0123456789"      # 특정 채널만 사용
# allowed_channel_ids = ["C0123456789", "C9876543210"]  # 허용 채널 제한
# allowed_user_ids = ["U0123456789"]                     # 허용 사용자 제한
```

토큰을 환경변수로 관리할 수도 있습니다:

```bash
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_APP_TOKEN="xapp-..."
```

환경변수가 설정되면 toml 파일에서 토큰을 생략할 수 있습니다.

### 파일 전송 (선택)

```toml
[slack.files]
enabled = true
uploads_dir = "incoming"
# max_upload_bytes = 20971520      # 20MB
# max_download_bytes = 52428800    # 50MB
# deny_globs = [".git/**", ".env", ".envrc", "*.pem", ".ssh/**"]
```

### 음성 메시지 (선택)

```toml
[slack.voice]
enabled = true
# model = "gpt-4o-mini-transcribe"
# base_url = "https://api.openai.com/v1"
# api_key = "sk-..."
```

## 8. 설정 검증

```bash
tunapi doctor
```

토큰, scope, 연결 상태를 확인합니다. 문제가 있으면 원인과 해결 방법을 안내합니다.

## 9. 실행

```bash
tunapi
```

정상 연결되면 로그에 `slack.connected`와 봇 사용자 정보가 표시됩니다. 채널에서 봇을 멘션하면 응답합니다.

디버그 모드로 실행하려면:

```bash
tunapi --debug
```
