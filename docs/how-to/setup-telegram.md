# Telegram 봇 설정 가이드

Telegram 봇을 만들고 tunapi에 연결하는 단계별 가이드.

## 1. @BotFather로 봇 생성

1. Telegram에서 [@BotFather](https://t.me/BotFather)에게 메시지를 보낸다.
2. `/newbot` 명령을 입력한다.
3. 봇의 표시 이름을 입력한다 (예: `tunapi`).
4. `bot`으로 끝나는 사용자명을 입력한다 (예: `my_tunapi_bot`).

BotFather가 토큰을 포함한 응답을 보낸다:

```
Use this token to access the HTTP API:
123456789:ABCdefGHIjklMNOpqrsTUVwxyz
```

!!! warning "토큰 보안"
    봇 토큰을 git에 커밋하거나 공개적으로 공유하지 않는다. 토큰이 있으면 누구나 봇을 제어할 수 있다.

## 2. 봇 토큰 복사

BotFather 응답에서 `123456789:ABC...` 형식의 토큰 전체를 복사한다. 콜론 앞의 숫자 부분까지 포함해야 한다.

## 3. chat_id 확인

봇에게 메시지를 보낸 후 `getUpdates` API를 호출하여 `chat_id`를 확인한다.

**1단계:** 봇에게 아무 메시지나 보낸다 (예: `/start`).

**2단계:** 브라우저나 터미널에서 다음 URL을 호출한다:

```sh
curl "https://api.telegram.org/bot<토큰>/getUpdates"
```

`<토큰>`을 실제 토큰으로 교체한다. 예:

```sh
curl "https://api.telegram.org/bot123456789:ABCdefGHIjklMNOpqrsTUVwxyz/getUpdates"
```

**3단계:** 응답에서 `chat.id` 값을 찾는다:

```json
{
  "result": [{
    "message": {
      "chat": {
        "id": 987654321,
        "type": "private"
      }
    }
  }]
}
```

이 `id` 값(위 예시에서 `987654321`)이 `chat_id`이다.

!!! tip "그룹 채팅의 chat_id"
    그룹이나 포럼 그룹의 `chat_id`는 음수이다 (예: `-1001234567890`). 봇을 그룹에 추가한 뒤 그룹에서 메시지를 보내고 동일하게 `getUpdates`를 호출한다.

## 4. tunapi.toml 설정

`~/.tunapi/tunapi.toml` 파일을 편집한다. 아래는 모든 Telegram 옵션을 포함한 예시이다:

```toml title="~/.tunapi/tunapi.toml"
default_engine = "codex"
transport = "telegram"

[transports.telegram]
bot_token = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
chat_id = 987654321

# 접근 제어: 허용할 Telegram 사용자 ID 목록 (비어 있으면 모든 사용자 허용)
allowed_user_ids = []

# 메시지가 Telegram 글자 수 제한을 초과할 때 처리 방식
# "trim" = 잘라냄 (기본값), "split" = 여러 메시지로 분할
message_overflow = "trim"

# 대화 모드
# "stateless" = 매 메시지가 독립적, reply-to로 이어감 (기본값)
# "chat" = 자동 이어가기, /new로 초기화
session_mode = "stateless"

# resume line 표시 여부
show_resume_line = true

# 음성 메시지 텍스트 변환
voice_transcription = false
voice_max_bytes = 10485760          # 10MB
voice_transcription_model = "gpt-4o-mini-transcribe"
# voice_transcription_base_url = "https://api.openai.com/v1"  # 커스텀 엔드포인트
# voice_transcription_api_key = "sk-..."                       # 별도 API 키

# 포워드 메시지 병합 대기 시간 (초)
forward_coalesce_s = 1.0

# 미디어 그룹 디바운스 시간 (초)
media_group_debounce_s = 1.0

# 토픽(포럼) 설정
[transports.telegram.topics]
enabled = false
scope = "auto"   # "auto" | "main" | "projects" | "all"

# 파일 전송 설정
[transports.telegram.files]
enabled = false
auto_put = true
auto_put_mode = "upload"   # "upload" | "prompt"
uploads_dir = "incoming"   # 프로젝트 내 상대 경로
allowed_user_ids = []
deny_globs = [".git/**", ".env", ".envrc", "*.pem", ".ssh/**"]
```

최소 설정은 `bot_token`과 `chat_id`만 있으면 된다:

```toml title="최소 설정"
default_engine = "codex"
transport = "telegram"

[transports.telegram]
bot_token = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
chat_id = 987654321
```

## 5. 환경변수 대안

토큰을 설정 파일에 직접 넣지 않고 환경변수로 전달할 수 있다:

```sh
export TELEGRAM_TOKEN="123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
```

이 경우 `tunapi.toml`에서 `bot_token`을 빈 문자열로 두거나 생략할 수 없다. 환경변수는 설정 파일의 `bot_token` 값이 비어 있을 때 대체값으로 사용된다.

!!! tip "dotenv 활용"
    `~/.tunapi/.env` 파일에 `TELEGRAM_TOKEN`을 넣어두면 셸 프로파일을 수정하지 않아도 된다.

## 6. tunapi doctor로 검증

설정이 올바른지 확인한다:

```sh
tunapi doctor
```

봇 토큰 유효성, 엔진 설치 상태, 설정 파일 문법 등을 검사한다. 문제가 있으면 원인과 해결 방법을 안내한다.

## 7. 실행

```sh
tunapi
```

정상적으로 시작되면 봇이 메시지를 수신 대기한다. Telegram에서 봇에게 메시지를 보내면 응답이 온다.

백그라운드 실행:

```sh
nohup tunapi &
```

!!! note "인스턴스 제한"
    동일한 봇 토큰으로 tunapi를 동시에 두 개 이상 실행할 수 없다. `~/.tunapi/tunapi.lock` 파일로 중복 실행을 방지한다.
