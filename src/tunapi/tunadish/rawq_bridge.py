"""rawq CLI 브릿지 — rawq 바이너리 호출을 캡슐화."""

import json
import os
import platform
import shutil
from pathlib import Path
from typing import Any

import anyio

from ..logging import get_logger

logger = get_logger(__name__)

# rawq search 기본 옵션
_DEFAULT_TOP = 5
_DEFAULT_TOKEN_BUDGET = 2000
_DEFAULT_THRESHOLD = 0.5
_DEFAULT_CONTEXT_LINES = 3
_SEARCH_TIMEOUT = 30   # search/map 타임아웃 (첫 실행 시 모델 로드 포함)
_INDEX_TIMEOUT = 300    # index build 타임아웃 (대형 프로젝트 대응)

# 기본 제외 패턴 — 빌드 산출물, 의존성 디렉토리
_DEFAULT_EXCLUDE = [
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "target/debug",
    "target/release",
    "dist",
    ".git",
    "*.egg-info",
    ".mypy_cache",
    ".pytest_cache",
    ".next",
]

# 캐싱: 바이너리 경로를 한 번만 탐색
_rawq_path: str | None = None
_rawq_checked = False


def _find_rawq() -> str | None:
    """rawq 바이너리 경로를 탐색한다.

    탐색 순서:
      1. RAWQ_BIN 환경변수 (명시적 경로)
      2. PATH에서 탐색 (일반 설치)
      3. vendor/rawq 빌드 산출물 (개발 환경)
    """
    # 1. 환경변수
    env_bin = os.environ.get("RAWQ_BIN")
    if env_bin and Path(env_bin).is_file():
        return env_bin

    # 2. PATH
    which = shutil.which("rawq")
    if which:
        return which

    # 3. vendor 빌드 경로 (개발 환경)
    # transport/src/tunadish_transport/rawq_bridge.py → 3단계 상위 = repo root
    repo_root = Path(__file__).resolve().parents[3]
    suffix = ".exe" if platform.system() == "Windows" else ""
    for profile in ("release", "debug"):
        candidate = repo_root / "vendor" / "rawq" / "target" / profile / f"rawq{suffix}"
        if candidate.is_file():
            return str(candidate)

    return None


def _get_rawq() -> str | None:
    """rawq 바이너리 경로를 반환 (캐시됨)."""
    global _rawq_path, _rawq_checked
    if not _rawq_checked:
        _rawq_path = _find_rawq()
        _rawq_checked = True
        if _rawq_path:
            logger.info("rawq binary found: %s", _rawq_path)
        else:
            logger.debug("rawq binary not found")
    return _rawq_path


def is_available() -> bool:
    """rawq 바이너리가 사용 가능한지 확인."""
    return _get_rawq() is not None


async def check_index(project_path: str | Path) -> dict[str, Any] | None:
    """프로젝트의 rawq 인덱스 상태를 확인.

    Returns:
        인덱스 정보 dict 또는 None(rawq 미설치/인덱스 없음)
    """
    if not is_available():
        return None
    try:
        result = await anyio.run_process(
            [_get_rawq(), "index", "status", str(project_path), "--json"],
            check=False,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception as e:
        logger.debug("rawq index status failed: %s", e)
    return None


async def build_index(
    project_path: str | Path,
    exclude: list[str] | None = None,
) -> bool:
    """프로젝트 인덱스를 생성/갱신한다.

    증분 인덱싱이므로 변경된 파일만 재처리된다.

    Returns:
        성공 여부
    """
    if not is_available():
        return False

    cmd = [_get_rawq(), "index", "build", str(project_path)]
    for pattern in (exclude or _DEFAULT_EXCLUDE):
        cmd.extend(["-x", pattern])

    try:
        with anyio.fail_after(_INDEX_TIMEOUT):
            result = await anyio.run_process(cmd, check=False)
        return result.returncode == 0
    except TimeoutError:
        logger.warning("rawq index build timed out after %ds", _INDEX_TIMEOUT)
        return False
    except Exception as e:
        logger.warning("rawq index build failed: %s", e)
        return False


async def search(
    query: str,
    project_path: str | Path,
    *,
    top: int = _DEFAULT_TOP,
    token_budget: int = _DEFAULT_TOKEN_BUDGET,
    threshold: float = _DEFAULT_THRESHOLD,
    context_lines: int = _DEFAULT_CONTEXT_LINES,
    lang_filter: str | None = None,
    exclude: list[str] | None = None,
) -> dict[str, Any] | None:
    """하이브리드 검색을 실행하고 JSON 결과를 반환.

    Returns:
        rawq JSON 출력 dict 또는 None(실패 시)
    """
    if not is_available():
        return None

    cmd = [
        _get_rawq(), "search", query, str(project_path),
        "--top", str(top),
        "--token-budget", str(token_budget),
        "--threshold", str(threshold),
        "--context", str(context_lines),
        "--json",
    ]
    if lang_filter:
        cmd.extend(["--lang", lang_filter])
    for pattern in (exclude or []):
        cmd.extend(["--exclude", pattern])

    try:
        with anyio.fail_after(_SEARCH_TIMEOUT):
            result = await anyio.run_process(cmd, check=False)
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except TimeoutError:
        logger.warning("rawq search timed out after %ds", _SEARCH_TIMEOUT)
    except Exception as e:
        logger.debug("rawq search failed: %s", e)
    return None


async def get_map(
    project_path: str | Path,
    *,
    depth: int = 2,
    lang_filter: str | None = None,
) -> dict[str, Any] | None:
    """AST 심볼 맵을 반환.

    Returns:
        rawq map JSON 출력 dict 또는 None
    """
    if not is_available():
        return None

    cmd = [_get_rawq(), "map", str(project_path), "--depth", str(depth), "--json"]
    if lang_filter:
        cmd.extend(["--lang", lang_filter])

    try:
        with anyio.fail_after(_SEARCH_TIMEOUT):
            result = await anyio.run_process(cmd, check=False)
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except TimeoutError:
        logger.warning("rawq map timed out after %ds", _SEARCH_TIMEOUT)
    except Exception as e:
        logger.debug("rawq map failed: %s", e)
    return None


def format_context_block(search_result: dict[str, Any]) -> str:
    """rawq 검색 결과를 에이전트 주입용 마크다운 블록으로 변환.

    Args:
        search_result: rawq search --json 의 출력

    Returns:
        마크다운 문자열. 결과가 없으면 빈 문자열.
    """
    results = search_result.get("results", [])
    if not results:
        return ""

    lines = ["<relevant_code>"]
    for r in results:
        file_path = r.get("file", "unknown")
        line_range = r.get("lines", [])
        lang = r.get("language", "")
        scope = r.get("scope", "")
        confidence = r.get("confidence", 0)
        content = r.get("content", "")

        header = f"## {file_path}"
        if line_range:
            header += f":{line_range[0]}-{line_range[1]}"
        if scope:
            header += f"  ({scope})"
        header += f"  [confidence: {confidence:.2f}]"

        lines.append(header)
        lines.append(f"```{lang}")
        lines.append(content.rstrip())
        lines.append("```")
        lines.append("")

    lines.append("</relevant_code>")
    return "\n".join(lines)


# ── 버전 확인 & 업데이트 체크 ──

async def get_version() -> str | None:
    """로컬 rawq 바이너리의 버전을 반환."""
    if not is_available():
        return None
    try:
        result = await anyio.run_process(
            [_get_rawq(), "--version"],
            check=False,
        )
        if result.returncode == 0:
            # "rawq 0.1.1" → "0.1.1"
            return result.stdout.decode().strip().split()[-1]
    except Exception as e:
        logger.debug("rawq version check failed: %s", e)
    return None


async def check_for_update() -> dict[str, Any] | None:
    """git submodule을 통해 원격에 새 버전이 있는지 확인.

    Returns:
        {"current": "abc123", "latest": "def456", "has_update": True, "commits": [...]}
        또는 None (확인 실패/submodule 없음)
    """
    repo_root = Path(__file__).resolve().parents[3]
    rawq_dir = repo_root / "vendor" / "rawq"

    if not (rawq_dir / ".git").exists():
        return None

    try:
        # fetch (5초 타임아웃, 네트워크 실패 허용)
        with anyio.fail_after(5):
            await anyio.run_process(
                ["git", "-C", str(rawq_dir), "fetch", "origin", "--quiet"],
                check=False,
            )

        # 현재 커밋
        current_result = await anyio.run_process(
            ["git", "-C", str(rawq_dir), "rev-parse", "HEAD"],
            check=False,
        )
        current = current_result.stdout.decode().strip()

        # 원격 최신 커밋
        latest_result = await anyio.run_process(
            ["git", "-C", str(rawq_dir), "rev-parse", "origin/main"],
            check=False,
        )
        latest = latest_result.stdout.decode().strip()

        has_update = current != latest

        commits: list[str] = []
        if has_update:
            log_result = await anyio.run_process(
                ["git", "-C", str(rawq_dir), "log", "--oneline", f"{current}..{latest}"],
                check=False,
            )
            if log_result.returncode == 0:
                commits = log_result.stdout.decode().strip().splitlines()[:10]

        return {
            "current": current[:12],
            "latest": latest[:12],
            "has_update": has_update,
            "commits": commits,
        }
    except TimeoutError:
        logger.debug("rawq update check: git fetch timed out")
    except Exception as e:
        logger.debug("rawq update check failed: %s", e)
    return None
