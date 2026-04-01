# Discord 봇 설정 가이드

tunapi를 Discord 봇으로 운영하기 위한 설정 절차.

---

## 1. Discord Application 생성

1. [Discord Developer Portal](https://discord.com/developers/applications) 접속
2. **New Application** 클릭, 이름 입력 후 생성

## 2. Bot 생성 및 토큰 복사

1. 좌측 메뉴 **Bot** 탭 선택
2. **Reset Token** 클릭하여 토큰 생성
3. 토큰을 복사하여 안전한 곳에 보관 — 이후 `tunapi.toml`에 사용

## 3. Intent 설정

같은 **Bot** 탭 하단의 **Privileged Gateway Intents** 섹션:

- **Message Content Intent** — **필수 활성화**. 이 권한 없이는 메시지 내용을 읽을 수 없음

## 4. 봇 초대

1. 좌측 메뉴 **OAuth2** → **URL Generator** 선택
2. **Scopes**: `bot`, `applications.commands` 체크
3. **Bot Permissions** 에서 다음 권한 체크:
    - Send Messages
    - Read Messages/View Channels
    - Manage Messages
    - Use Slash Commands
    - Connect (Voice)
    - Embed Links
    - Attach Files
    - Read Message History
4. 생성된 URL을 브라우저에 붙여넣고 서버 선택 후 초대

## 5. `tunapi.toml` 설정

`~/.tunapi/tunapi.toml`:

```toml
transport = "discord"

[discord]
bot_token = "YOUR_BOT_TOKEN"
```

### 전체 옵션

```toml
[discord]
# (필수) 봇 토큰
bot_token = "YOUR_BOT_TOKEN"

# 특정 길드(서버)로 제한. 생략 시 봇이 참여한 모든 서버에서 동작
guild_id = 123456789012345678

# 세션 모드: "stateless"(매 요청 독립) 또는 "chat"(대화 이력 유지)
session_mode = "stateless"

# 이전 대화 재개 시 구분선 표시 여부
show_resume_line = true

# 긴 응답 처리: "split"(여러 메시지로 분할) 또는 "trim"(잘라내기)
message_overflow = "split"

# 기본 트리거 모드: "all"(모든 메시지 반응) 또는 "mentions"(@멘션만 반응)
trigger_mode_default = "all"

# 허용된 사용자 ID 목록. 생략 시 모든 사용자 허용
allowed_user_ids = [123456789012345678, 987654321098765432]

# 미디어 그룹 디바운스 시간(초). 여러 첨부파일을 하나로 묶는 대기 시간
media_group_debounce_s = 0.75

# 파일 전송 설정
[discord.files]
enabled = false                # 파일 전송 기능 활성화
auto_put = true                # 첨부파일 자동 저장
auto_put_mode = "upload"       # "upload"(즉시 저장) 또는 "prompt"(확인 후 저장)
uploads_dir = "incoming"       # 업로드 저장 디렉토리
max_upload_bytes = 20971520    # 최대 업로드 크기(바이트, 기본 20MB)
deny_globs = [".git/**", ".env", ".envrc", "**/*.pem", "**/.ssh/**"]
allowed_user_ids = []          # 파일 전송 허용 사용자 (생략 시 전체 허용)

# 음성 메시지 전사 설정
[discord.voice_messages]
enabled = false                # 음성 메시지 전사 활성화
max_bytes = 10485760           # 최대 음성 파일 크기(바이트, 기본 10MB)
whisper_model = "base"         # Whisper 모델 (tiny, base, small, medium, large)
```

## 6. 설정 검증

```sh
tunapi doctor
```

토큰, 엔진, 권한 등의 설정 상태를 확인한다. 문제가 있으면 원인과 해결 방법이 출력된다.

## 7. 실행

```sh
tunapi
```

정상 연결 시 봇이 온라인 상태로 전환되며, 설정된 채널에서 메시지에 반응한다.

---

## 참고

- `trigger_mode_default = "mentions"` 설정 시 그룹 채널에서 `@봇이름`으로 호출해야 반응
- `allowed_user_ids`를 설정하면 지정된 사용자만 봇을 사용할 수 있음
- `tunapi setup` 명령으로 대화형 초기 설정도 가능
