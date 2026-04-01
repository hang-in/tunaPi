# Mattermost 봇 설정 가이드

Mattermost 서버에 tunapi 봇을 연결하는 단계별 안내.

## 1. 봇 계정 생성

Mattermost 관리자 권한이 필요하다.

1. **System Console** > **Integrations** > **Bot Accounts** 에서 봇 계정을 활성화한다.
2. 채널 좌측 상단 메뉴 > **Integrations** > **Bot Accounts** > **Add Bot Account** 클릭.
3. 항목을 채운다:
   - **Username**: `tunapi` (또는 원하는 이름)
   - **Display Name**: 표시 이름
   - **Role**: `System Admin` 권한이 필요하지 않다면 `Member`로 충분하다.
4. **Create Bot Account** 클릭 후 생성된 **Token**을 복사해 둔다.

> 토큰은 한 번만 표시된다. 분실 시 새로 생성해야 한다.

## 2. Personal Access Token (대안)

봇 계정 대신 개인 토큰을 사용할 수도 있다.

1. **Profile** > **Security** > **Personal Access Tokens** > **Create Token**.
2. 토큰을 복사한다.

봇 계정 토큰이 권장된다. 개인 토큰은 해당 사용자의 모든 권한을 갖기 때문이다.

## 3. 채널 ID 확인

봇이 메시지를 보낼 기본 채널의 ID가 필요하다.

**방법 A — URL에서 확인:**

채널에 접속하면 URL이 다음 형태이다:

```
https://mattermost.example.com/team-name/channels/channel-name
```

이 URL의 채널 이름은 `channel_id`가 아니다. 정확한 ID를 얻으려면 방법 B를 사용한다.

**방법 B — 채널 헤더에서 확인:**

채널 이름 클릭 > **View Info** > 하단의 **ID** 복사.

**방법 C — API 호출:**

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  https://mattermost.example.com/api/v4/teams/TEAM_ID/channels/name/CHANNEL_NAME \
  | jq .id
```

## 4. `tunapi.toml` 설정

`~/.tunapi/tunapi.toml` 파일을 편집한다.

### 최소 설정

```toml
transport = "mattermost"
default_engine = "claude"

[transports.mattermost]
url = "https://mattermost.example.com"
token = "your-bot-token-here"
channel_id = "abc123def456"
```

### 전체 옵션

```toml
transport = "mattermost"
default_engine = "claude"

[transports.mattermost]
url = "https://mattermost.example.com"      # Mattermost 서버 URL (필수)
token = "your-bot-token-here"               # 봇/개인 토큰 (필수, 환경변수 대안 있음)
channel_id = "abc123def456"                 # 기본 채널 ID (선택 — 시작 알림 대상)
session_mode = "stateless"                  # "stateless" | "chat" — chat이면 대화 이력 유지
show_resume_line = true                     # 세션 재개 시 구분선 표시 여부
message_overflow = "trim"                   # "trim" | "split" — 긴 응답 처리 방식
trigger_mode = "all"                        # "all" | "mentions" — all이면 모든 메시지에 반응, mentions이면 @멘션만
allowed_channel_ids = ["id1", "id2"]        # 허용 채널 목록 (비어 있으면 모든 채널 허용)
allowed_user_ids = ["uid1", "uid2"]         # 허용 사용자 목록 (비어 있으면 모든 사용자 허용)

[transports.mattermost.files]
enabled = false                             # 파일 전송 활성화
uploads_dir = "incoming"                    # 업로드 저장 디렉터리 (프로젝트 상대 경로)
max_upload_bytes = 20971520                 # 업로드 최대 크기 (기본 20MB)
max_download_bytes = 52428800               # 다운로드 최대 크기 (기본 50MB)
deny_globs = [".git/**", ".env", ".envrc", "*.pem", ".ssh/**"]  # 차단 패턴

[transports.mattermost.voice]
enabled = false                             # 음성 메시지 텍스트 변환 활성화
max_bytes = 10485760                        # 음성 파일 최대 크기 (기본 10MB)
model = "gpt-4o-mini-transcribe"            # 음성 인식 모델
base_url = "https://api.openai.com/v1"      # OpenAI API base URL (선택)
api_key = "sk-..."                          # OpenAI API 키 (선택, OPENAI_API_KEY 환경변수 대안)
```

## 5. 환경변수 대안

토큰을 설정 파일에 직접 넣고 싶지 않다면 환경변수를 사용한다.

```bash
export MATTERMOST_TOKEN="your-bot-token-here"
```

설정 파일의 `token` 필드가 비어 있거나 없을 때 `MATTERMOST_TOKEN` 환경변수가 자동으로 사용된다. 설정 파일에 `token` 값이 있으면 환경변수보다 우선한다.

## 6. 설정 검증

```bash
tunapi doctor
```

정상이면 다음과 같이 출력된다:

```
tunapi doctor
- mattermost token: ok (@tunapi)
- channel_id: ok (General)
- file transfer: ok (disabled)
- voice transcription: ok (disabled)
```

토큰이 유효하지 않거나 채널에 접근할 수 없으면 `error`로 표시된다.

## 7. 실행

```bash
tunapi run
```

봇이 시작되면 설정된 채널에 시작 알림을 보낸다. 이후 해당 채널(또는 `allowed_channel_ids`에 포함된 채널)에서 메시지를 보내면 봇이 응답한다.

`trigger_mode = "mentions"`로 설정한 경우 그룹 채널에서는 `@botname`으로 멘션해야 반응한다. DM은 항상 반응한다.
