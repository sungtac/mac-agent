#!/usr/bin/env python3
"""Shared helpers between discord-bot.py (맥, Claude-side: 주간보고서/상태/
자유채팅) and codex-bot.py (코덱스 전용, 2026-07-29) — split into its own
module rather than duplicated across both files so a fix to the usage gate,
the graceful-kill escalation, or SUBPROCESS_ENV's PATH handling lands once
for both bots instead of risking the two copies silently drifting apart.

Both bot scripts run as independent launchd-managed processes with their own
Discord token/config — this module has no discord.py dependency of its own
and no side effects at import time, so either bot can import from it freely.
"""
import asyncio
import contextlib
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
import re
import signal
import tempfile
from pathlib import Path
from typing import Awaitable, Callable

from edge_agent_locks import canonical_repository_root
from edge_agent_capability_registry import prepare_provider_argv
from edge_agent_workspace_lock import RepoLockBusy as CommonRepoLockBusy, try_acquire_repo_lock as common_try_acquire_repo_lock
from agent_profile import render_agent_profile

MAC_AGENT = Path.home() / "mac-agent"

# weekly-report.sh(2026-07-30 확장)와 동일한 패턴 — 계정 사용/속도 한도 초과를
# 나타내는 흔한 문구들의 근사치(정확한 오류 카탈로그가 없어 grep 휴리스틱일
# 수밖에 없음). discord-bot.py/codex-bot.py 둘 다 "이건 코드 결함이 아니라
# 계정 한도"를 구분하는 데 쓰고(사용자에게 친절한 안내), 2026-07-30 사용자
# 요청 이후로는 "실패했다고 그냥 기다리라고 하지 말고 다른 provider로 자동
# 폴백하라"는 판단 트리거로도 쓴다 — 이 저장소 다른 곳(route-dispatch.sh의
# Rule B)에 이미 있는 "한쪽이 낮으면 다른 쪽으로" 멀티에이전트 원칙을 두
# 봇의 실패 처리에도 반영한 것. 파이썬 re 모듈 문법으로 옮김(원본은 bash
# grep -E 문법).
QUOTA_LIMIT_PATTERN = re.compile(
    r'hit your (session|usage) limit|rate.?limit(_error| exceeded)?|usage cap|quota exceeded|\boverloaded\b|\b429\b',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HeadroomAdvice:
    """Parsed Claude/Codex remaining-usage advice.

    ``None`` means the advisor could not produce a trustworthy numeric
    comparison.  Callers must fail open in that case rather than turning a
    monitoring failure into a provider outage.
    """

    preferred_provider: str | None
    claude_pct: int | None
    codex_pct: int | None
    raw: str = ""


_HEADROOM_ADVICE_PATTERN = re.compile(
    r"PREFER:\s*(claude|codex)\s*\(claude:(\d+)%\s+codex:(\d+)%\)",
    re.IGNORECASE,
)


def parse_headroom_advice(output: str) -> HeadroomAdvice:
    """Parse the strict one-line contract emitted by usage-advisor.sh."""
    raw = (output or "").strip()
    # Do not route on a recommendation embedded in unrelated stderr/log text.
    # The shell helper promises exactly one line; anything else is unknown.
    match = _HEADROOM_ADVICE_PATTERN.fullmatch(raw)
    if not match:
        return HeadroomAdvice(None, None, None, raw)
    try:
        claude_pct = int(match.group(2))
        codex_pct = int(match.group(3))
    except (TypeError, ValueError):
        return HeadroomAdvice(None, None, None, raw)
    if not 0 <= claude_pct <= 100 or not 0 <= codex_pct <= 100:
        return HeadroomAdvice(None, None, None, raw)
    return HeadroomAdvice(
        match.group(1).lower(), claude_pct, codex_pct, raw,
    )


def atomic_write_json(path: Path, payload: dict) -> None:
    """Write one JSON object via same-directory temp file + atomic replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def should_prefer_codex(advice: HeadroomAdvice, minimum_margin_pct: int = 20) -> bool:
    """Return whether a new, non-resumable turn should spare Claude.

    The margin prevents noisy coach readings or a one-point tie-break from
    constantly moving new conversations between providers.  Unknown data,
    an invalid margin, and an advantage in Claude's direction all fail open.
    Existing Claude sessions are handled by the caller and are never routed
    here because continuity is more valuable than a small usage difference.
    """
    if advice.preferred_provider != "codex":
        return False
    if advice.claude_pct is None or advice.codex_pct is None:
        return False
    if minimum_margin_pct < 1:
        return False
    return advice.codex_pct - advice.claude_pct >= minimum_margin_pct


@dataclass(frozen=True)
class ProviderResult:
    """Normalized result contract for a single external provider attempt.

    The caller decides whether a generic error is retryable; this class only
    answers the mechanical question "what did the process return?".  Keeping
    this distinction in one place prevents the Discord fallback and the
    route-dispatch fallback from growing subtly different quota heuristics.
    """

    provider: str
    returncode: int | None
    output: str

    @property
    def status(self) -> str:
        text = (self.output or "").strip()
        if self.returncode == 0 and text:
            return "ok"
        if not text:
            return "empty"
        # A short quota/error line is a depletion signal.  A long successful
        # answer can legitimately discuss rate limits as its subject.
        if (self.returncode != 0 or len(text) < 200) and QUOTA_LIMIT_PATTERN.search(text):
            return "quota"
        if self.returncode != 0:
            return "error"
        return "empty"

    @property
    def usable(self) -> bool:
        return self.status == "ok"

    def diagnostic(self, max_chars: int = 300) -> str:
        text = (self.output or "").strip()
        if len(text) > max_chars:
            text = text[-max_chars:]
        return text or f"exit={self.returncode}; 응답 없음"


@dataclass(frozen=True)
class ProviderFallbackResult:
    """Outcome of the Antigravity -> Codex fallback chain."""

    antigravity: ProviderResult
    codex: ProviderResult | None
    codex_skip_reason: str | None
    stop_reason: str | None = None


def format_provider_fallback_failure(result: ProviderFallbackResult, max_chars: int = 1900) -> str:
    """Build a Discord-safe failure envelope without hiding provider labels."""
    antigravity_detail = result.antigravity.diagnostic(450)
    if result.codex_skip_reason:
        codex_detail = f"사전 게이트 차단: {result.codex_skip_reason[:450]}"
    elif result.codex is None:
        codex_detail = "실행되지 않음"
    else:
        codex_detail = result.codex.diagnostic(450)
    text = (
        "❌ 대체 provider 체인도 완료하지 못했습니다.\n"
        f"- Antigravity: {antigravity_detail}\n"
        f"- Codex: {codex_detail}\n"
        "사용량 회복 후 다시 말씀해주세요."
    )
    return text[:max_chars]


def load_provider_context(path: Path) -> dict | None:
    """Load one bounded fallback response for the next native session turn."""
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not data.get("response"):
            return None
        return data
    except Exception:
        return None


def save_provider_context(path: Path, provider: str, user_text: str, response: str) -> None:
    """Persist bounded cross-provider context without making the turn fail."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        atomic_write_json(path, {
            "provider": provider,
            "user_message": user_text[-4000:],
            "response": response[-6000:],
        })
    except Exception:
        pass


def clear_provider_context(path: Path) -> None:
    path.unlink(missing_ok=True)


def format_provider_context(data: dict) -> str:
    """Render persisted fallback context as an explicitly untrusted prompt block."""
    return (
        "[참고 — 직전 요청에 대한 대체 provider 응답. Claude 세션 기록에는 아직 "
        "들어오지 않은 참고자료이므로, 사실 여부를 확인하며 이어서 답해:]\n"
        f"provider: {data.get('provider', 'unknown')}\n"
        f"사용자 요청: {data.get('user_message', '')}\n"
        f"대체 응답:\n{data.get('response', '')}"
    )


async def run_provider_fallback_chain(
    antigravity_attempt: Callable[[], Awaitable[ProviderResult]],
    codex_gate: Callable[[], Awaitable[str | None]],
    codex_attempt: Callable[[], Awaitable[ProviderResult]],
    should_continue: Callable[[], bool] | None = None,
) -> ProviderFallbackResult:
    """Advance to Codex only when Antigravity did not produce an answer."""
    antigravity = await antigravity_attempt()
    if antigravity.usable:
        return ProviderFallbackResult(antigravity, None, None)
    if should_continue is not None and not should_continue():
        return ProviderFallbackResult(antigravity, None, None, "사용자가 중단함")
    skip_reason = await codex_gate()
    if skip_reason:
        return ProviderFallbackResult(antigravity, None, skip_reason)
    if should_continue is not None and not should_continue():
        return ProviderFallbackResult(antigravity, None, None, "사용자가 중단함")
    return ProviderFallbackResult(antigravity, await codex_attempt(), None)

# Natural-chat wake words for codex-bot.py's handle_codex_chat_wake — a
# message starting with one of these (e.g. "코덱스야 ...", "콕스 ...") is
# addressed to Codex, not Claude (2026-07-29, user's explicit request to
# address bots by name like ChatGPT rather than typing a command every
# turn). Lives here, not in codex-bot.py, because discord-bot.py's own
# free-chat catch-all must exclude the SAME set of words — both bots sit in
# the same channel and see every message, so if this list ever drifted
# between the two files, a wake-worded message could get answered by BOTH
# bots (or by neither).
CODEX_CHAT_WAKE_WORDS = ("코덱스", "콕스")

# 2026-07-30, 실측 버그 리포트로 발견: "안녕 콕스"(wake word가 문장 맨 앞이
# 아님)는 startswith(CODEX_CHAT_WAKE_WORDS)에 걸리지 않아 codex-bot.py의
# wake 핸들러로 아예 안 들어갔고, 동시에 discord-bot.py의 배제 조건에도
# 안 걸려서 맥의 자유채팅 쪽으로 잘못 흘러들어갔다(마침 그 시점에 다른
# free-chat 요청 처리 중이라 락 충돌 메시지까지 떴다). 두 증상이 사실
# 하나의 원인 — "맨 앞에서만" 감지하는 게 너무 좁다.
_WAKE_PARTICLE_SUFFIXES = ("야", "아", "씨", "님")
_WAKE_TRAILING_PUNCT = "!?~.,-… "


def _strip_wake_particle(token: str) -> str:
    for particle in _WAKE_PARTICLE_SUFFIXES:
        if token.endswith(particle) and token != particle:
            return token[: -len(particle)]
    return token


def is_codex_wake_word(content: str) -> bool:
    """True if `content` addresses Codex by name — at the start ("콕스야
    ...", "코덱스 ...") or as the trailing word ("안녕 콕스", "이거 어때
    코덱스야?"). Only the first and last whitespace-delimited tokens are
    checked (with trailing punctuation and common vocative particles
    야/아/씨/님 stripped before comparing) — deliberately NOT a bare
    substring check, which would also fire on a message merely mentioning
    Codex mid-sentence ("어제 콕스가 이상했어") and misroute a statement
    about the bot as an address to it.

    Lives here, not in either bot file, because discord-bot.py's free-chat
    catch-all must exclude exactly the same messages codex-bot.py's wake
    handler accepts — both bots sit in the same channel and see every
    message, so if this logic ever drifted between the two files, a
    wake-worded message could get answered by BOTH bots (or by neither).
    """
    tokens = content.strip().split()
    if not tokens:
        return False
    first = _strip_wake_particle(tokens[0].rstrip(_WAKE_TRAILING_PUNCT))
    last = _strip_wake_particle(tokens[-1].rstrip(_WAKE_TRAILING_PUNCT))
    return first in CODEX_CHAT_WAKE_WORDS or last in CODEX_CHAT_WAKE_WORDS


# 2026-07-30, 사용자 실제 테스트로 발견: "각자 자기소개 해줘"를 보냈더니 맥만
# 응답하고, 맥이 스스로 "콕스는 별도 봇이라 콕스야로 따로 불러야 한다"고
# 안내했다. 사용자 피드백: "각자"라는 말을 그냥 일반 단체방처럼 콕스도
# 이해하고 같이 말하면 되지 않냐 — 매번 이름을 따로 불러야 하는 게 아니라,
# 여러 참가자를 동시에 지칭하는 표현이면 콕스도 (맥과 별개로) 알아서 같이
# 응답해야 한다는 것. is_codex_wake_word()와 별개 함수인 이유: 이건 "이름으로
# 콕스 하나를 지목"이 아니라 "여러 명을 한꺼번에 지칭"이므로 첫/끝 토큰
# 제한이 아니라 문장 어디에 있어도 신호가 된다 — 사용자가 명시적으로 더 넓은
# 단어 목록(모두/전부 포함)을 선택, 오탐 가능성("모두 감사합니다"에도 콕스가
# 응답)은 감수하기로 확정.
GROUP_ADDRESS_WORDS = (
    "각자", "둘 다", "둘다", "다같이", "같이", "모두 다", "모두", "전부", "얘들아",
)


def is_group_address(content: str) -> bool:
    """True if `content` refers to multiple/all participants at once
    ("각자 자기소개 해줘", "둘 다 어떻게 생각해?") rather than naming Codex
    specifically. Used by codex-bot.py to also answer such messages even
    without an explicit "콕스야" — see GROUP_ADDRESS_WORDS comment above for
    why this is a bare substring check (unlike is_codex_wake_word) and the
    false-positive tradeoff that implies.

    Whitespace is collapsed before matching (2026-07-30, 실측 버그: 사용자가
    실제로 보낸 "둘  다 소개 좀 해줘"는 "둘"과 "다" 사이에 스페이스가 두
    칸이라 리터럴 "둘 다"(한 칸) 매칭에 실패해 콕스가 응답하지 않았다 —
    다중 단어 항목("둘 다", "모두 다")은 모두 이 문제에 취약하므로 개별
    스페이스 개수를 맞추는 대신 입력 쪽 공백을 정규화한다)."""
    normalized = " ".join(content.split())
    return any(word in normalized for word in GROUP_ADDRESS_WORDS)


# 2026-07-30, 사용자 명시적 요청: 두 봇 다 지금까지 자기 자신이 누구인지,
# 옆에 누가 있는지에 대한 시스템 레벨 인지가 전혀 없었다 — 실측으로 확인된
# 실제 사고: 사용자가 "콕스야"라고 불렀는데, 맥(discord-bot.py, Discord
# 계정명 "edgeAI_맥")이 "콕스"라는 이름에 대한 기억이 자기 어디에도 없다고
# 답한 반면, 실제로는 콕스(codex-bot.py, Discord 계정명 "콕스")가 바로 옆
# 채널에서 계속 응답하고 있었다(실제 채널 히스토리로 확인, 2026-07-30).
# 이름 자체는 사용자가 이미 Discord 봇 계정명으로 붙여둔 것을 그대로 채택 —
# 새로 짓지 않음. fetch_cross_bot_context의 라벨링과 아래 두 페르소나 텍스트
# 둘 다 여기 상수를 공유해서 이름이 어긋날 여지를 없앤다.
MAC_BOT_NAME = "맥"
CODEX_BOT_NAME = "콕스"

# 2026-07-30, 사용자 명시적 요청(후속): "지금의 멀티에이전트[오케스트레이터가
# 지시받으면 판단해서 코덱스에게 능동적으로 넘기는 것]를 터미널이 아니라
# 디스코드에서 그대로 쓰고 싶다." 원래 버전은 코덱스 관련 요청을 무조건
# CODEX_BOT_NAME한테 떠넘기라고 했는데, 그건 정반대 방향이었다 —
# handle_free_chat의 claude -p는 이미 인터랙티브 세션과 동일한 풀 툴
# 권한(Bash 포함, 저장소 제한 없음, docs/discord-bot.md "Phase 3" 절 참고)을
# 갖고 있어서, 터미널의 오케스트레이터처럼 직접 Bash로 코덱스를 부를 수 있는
# 능력이 이미 있었다 — 페르소나가 그 능력을 쓰지 말고 사람한테 떠넘기라고
# 지시하고 있었을 뿐. codex-execute-dispatch.sh(verify-task-v2.js가 실제
# 코덱스 실행 단계에서 쓰는 것과 동일한, 쓰기 가능한 디스패처)를 직접 부를 수
# 있다고 알려주고, 자기보고 불신 원칙(이 저장소 전체에 이미 깔린 규율 —
# score-dispatch.sh/codex-bot.py의 before/after diff 검증과 동일)까지
# 명시해서 판단·검증 방식도 맞춘다.
# 2026-07-30, 사용자 후속 요청: "터미널의 너와 디스코드의 맥은 100% 동일해야해.
# 콕스의 역할도 마찬가지고." — 위 버전은 코덱스 위임을 codex-execute-dispatch.sh
# 직접호출로만 안내했는데, 이건 verify-task-v2.js Full track(스펙+블라인드
# 비평+다단계 검증, 이 저장소에서 실제 코딩 위임의 "진짜" 엔진)을 우회하는
# 얕은 버전이었다 — 완전한 동일성이 아니었음. 확인해보니 필요한 인프라는
# 이미 다 있었다: ~/.claude/settings.json의 Stop 훅(verify-task-stop-check.sh
# 등)은 유저 스코프라 claude -p 헤드리스 호출에도 그대로 적용되고(별도
# --settings override 없음), handle_free_chat의 env는 이미
# CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0을 설정해서 자기 턴 안에서 Workflow
# 호출이 끝날 때까지 제대로 블록하며(handle_verify_task_v2_retry가 이미
# 실측 검증한 것과 동일 메커니즘), FREE_CHAT_TIMEOUT_SECONDS도 처음부터
# "!코덱스/verify-task-v2와 동일 예산"으로 30분 잡혀 있었다 — 즉 이 페르소나
# 텍스트가 그 능력을 안 알려줬을 뿐, 새 인프라를 만들 필요는 없었다.
#
# 사용자 확정(2026-07-30): 작은 작업은 가볍게, 진짜 코딩 위임만
# verify-task-v2로 — 이 저장소에 이미 있는 3단 구분(트리비얼=직접 처리 /
# 소규모=경량 디스패치 / 진짜 코딩=verify-task-v2, 파일수·민감경로 기준 자동
# 티어링은 verify-task-v2 스스로 함)을 그대로 옮겨왔다.
MAC_BOT_PERSONA = (
    render_agent_profile("claude", "coordinator")
    + "\n\n"
    f"너는 이 Discord 채널에서 '{MAC_BOT_NAME}'이라는 이름으로 활동하는 Claude 기반 에이전트야. "
    "인터랙티브 터미널 세션과 동일한 풀 툴 권한(Edit/Write/Bash 등, 저장소 범위 제한 없음)을 갖고 있고, "
    "이 계정의 ~/.claude/settings.json에 설정된 Stop 훅(verify-task-stop-check.sh 등)도 인터랙티브 "
    "세션과 완전히 동일하게 너한테도 적용돼 — 즉 너는 터미널의 오케스트레이터와 같은 엔진, 같은 규율 "
    "아래서 움직여.\n\n"
    "**'맥아' 같은 단순 호출/인사에는 짧게만 답해**(2026-07-30, 실측으로 발견한 문제 — "
    "\"맥아\"라는 인사 하나에 로그를 뒤져서 긴 기술 분석문을 만들어내느라 응답이 58초까지 "
    "걸린 적이 있었어). 채팅 맥락에 곁들여지는 '[참고 — 같은 채널에서 최근 다른 봇과 나눈 "
    "대화]' 블록에 예전에 있었던 오류나 논쟁이 담겨 있어도, 사용자가 그걸 다시 묻지 않는 "
    "이상 먼저 나서서 분석·설명하지 마 — 그냥 참고자료일 뿐이니 필요할 때만 자연스럽게 "
    "언급해. 트리비얼한 호출엔 트리비얼하게 응답해.\n\n"
    "코딩/저장소 작업 요청을 받으면 규모로 판단해:\n"
    "1. **트리비얼**(오타, 한 줄 확인, 짧은 조회): 네가 Read/Edit/Bash로 직접 처리해.\n"
    "2. **소규모 위임**(파일 하나, 간단한 변경, 빠른 확인이 필요한 정도): Bash로 지시문을 임시 파일에 "
    "쓰고 `bash /Users/edge_ai/mac-agent/workflows/lib/codex-execute-dispatch.sh <저장소 절대경로> "
    '<지시문파일경로>`를 실행해 — `{"ok": true/false, "message": "..."}` JSON이 돌아와. 코덱스의 '
    "자기 보고를 그대로 믿지 말고, 실행 전/후 `git status`/`git diff`로 직접 대조 확인.\n"
    "3. **진짜 코딩 위임**(새 기능, 여러 파일에 걸친 변경, 로직이 있는 작업 등 — 인터랙티브 세션이었다면 "
    "네가 verify-task-v2를 돌렸어야 할 만한 일): Workflow 툴을 직접 호출해 — "
    '`Workflow({scriptPath: "/Users/edge_ai/mac-agent/workflows/verify-task-v2.js", args: {task: '
    "\"<사용자 요청 그대로 + 필요한 맥락>\", cwd: \"<저장소 절대경로>\", persona: \"discord 사용자\"}})`. "
    "이게 코덱스가 스스로 계획을 세우고, 클로드/안티그래비티가 그 계획을 블라인드 비평하고, 코덱스가 "
    "반영해서 실행하고, 다시 듀얼 코드리뷰까지 거치는 정식 파이프라인이야(파일 개수·민감경로에 따라 "
    "내부적으로 경량/전체 트랙을 스스로 판단하니 너는 그냥 불러주기만 하면 돼). 실행이 끝나면 반환된 "
    "finalVerdict를 요약해서 사용자에게 한국어로 보고해.\n\n"
    f"같은 채널에 '{CODEX_BOT_NAME}'이라는 이름의 동료 봇도 별도로 있어(사용자가 직접 '콕스야'라고 "
    "부르면 그쪽이 응답함) — 하지만 너도 위 방법들로 직접 코덱스를 부를 수 있으니, 코딩 관련 요청이라고 "
    f"무조건 '{CODEX_BOT_NAME}한테 물어보세요'로 떠넘기지 말고, 위 세 단계 중 어디에 해당하는지 판단해서 "
    "직접 처리해. 채팅 맥락에 '[참고 — 같은 채널에서 최근 다른 봇과 나눈 대화]' 같은 블록이 곁들여질 수 "
    "있는데, 그건 실제 네 세션 기록이 아니라 참고자료일 뿐이야.\n\n"
    f"**'둘 다'/'각자'/'모두'처럼 여럿을 한꺼번에 지칭하는 요청을 받아도, {CODEX_BOT_NAME}을 대신해서 "
    f"소개하거나 의견을 답하지 마 — 네 얘기만 해**(2026-07-30, 실측으로 발견한 문제 — 사용자가 '둘 다 "
    f"소개해줘'라고 했을 때 한쪽 봇이 상대까지 요약해서 답해버려서 '왜 한 명이 둘 다 소개하냐'는 혼란을 "
    f"줬어). {CODEX_BOT_NAME}은 같은 요청에 별도로, 독립적으로 응답해."
)

# 2026-07-30, 사용자 요청: "콕스야도 똑같이 위임 판단하게 해줘" — 맥이 코덱스에게
# 능동적으로 위임하는 것과 대칭으로, 콕스도 자기가 못 하거나 안 맞는 요청이면
# 맥에게 위임할 수 있어야 한다는 것. 다만 맥→코덱스와 똑같은 메커니즘(Bash로
# 상대를 직접 호출)은 안 됨 — 실측 확인(2026-07-30): `codex exec -s
# workspace-write` 샌드박스 안에서 `claude -p`를 직접 실행시켜보니 빈 응답
# 또는 90초 타임아웃이 났다(네트워크 아웃바운드가 막혀있는 것으로 추정).
# `-s danger-full-access`로 풀면 되겠지만, 그건 파일쓰기 범위 제한이라는
# 원래 안전장치를 없애는 거라 채택 안 함. 대신 콕스는 위임하고 싶으면 이
# 마커로 시작하는 응답을 내고, 실제 claude -p 호출은 codex-bot.py 자신의
# 파이썬 코드(코덱스의 샌드박스 밖, 호스트 레벨)가 대신 수행한다 —
# _delegate_to_claude() 참고. 콕스 쪽 페르소나와 그 파싱 로직 둘 다 이
# 마커 문자열을 공유해서 어긋날 여지를 없앤다.
CODEX_DELEGATE_TO_MAC_MARKER = "[위임:맥]"

CODEX_BOT_PERSONA = (
    render_agent_profile("codex", "implementer")
    + "\n\n"
    f"너는 이 Discord 채널에서 '{CODEX_BOT_NAME}'이라는 이름으로 활동하는 Codex 기반 동료야. "
    f"같은 채널에 '{MAC_BOT_NAME}'이라는 이름의 동료 봇이 있는데, Claude 기반이고 범용 대화/일반 업무를 "
    f"맡고 있어. 사용자가 '맥'을 부르거나 그 이름으로 뭔가 물어보면 그건 {MAC_BOT_NAME}을 가리키는 거야. "
    "너는 코딩/저장소 작업(파일 읽기·쓰기, git, 코드 분석)에 특화돼 있어 — 그 범위를 벗어나는 요청이면"
    f"(예: 일반 잡담, 저장소와 무관한 지식 질문, 여러 도구를 넘나드는 폭넓은 작업 등) 억지로 답하려 하지 "
    f"말고 {MAC_BOT_NAME}에게 위임해. 위임하려면 응답을 정확히 `{CODEX_DELEGATE_TO_MAC_MARKER}` 로 시작하고 "
    f"그 뒤에 {MAC_BOT_NAME}에게 물어볼 내용을 그대로 이어써(그 줄이 응답 전체가 되게) — 이 정확한 형식일 "
    "때만 실제로 위임이 처리돼. 코딩/저장소 작업이면 평소처럼 네가 직접 처리해. "
    "채팅 맥락에 '[참고 — 같은 채널에서 최근 다른 봇과 나눈 대화]' 같은 블록이 곁들여질 수 있는데, "
    "그건 실제 네 스레드 기록이 아니라 참고자료일 뿐이야.\n\n"
    f"**'둘 다'/'각자'/'모두'처럼 여럿을 한꺼번에 지칭하는 요청을 받아도, {MAC_BOT_NAME}을 대신해서 "
    f"소개하거나 의견을 답하지 마 — 네 얘기만 해**(2026-07-30, 실측으로 발견한 문제 — 사용자가 '둘 다 "
    f"소개해줘'라고 했을 때 한쪽 봇이 상대까지 요약해서 답해버려서 '왜 한 명이 둘 다 소개하냐'는 혼란을 "
    f"줬어). {MAC_BOT_NAME}은 같은 요청에 별도로, 독립적으로 응답해."
)

# Subprocesses we spawn shell out to codex/agy/claude by absolute path
# already, but git/date/etc still resolve via PATH — launchd's own PATH for
# this process can be the stripped /usr/bin:/bin:/usr/sbin:/sbin default, so
# give spawned children a real one explicitly rather than rediscovering this
# gotcha yet again (same recurring issue as codex/agy/ffmpeg/whisper-cli/tmux/
# coach elsewhere in this repo, see docs/discord-bot.md).
SUBPROCESS_ENV = {
    **os.environ,
    "PATH": f"/opt/homebrew/bin:{Path.home()}/.local/bin:" + os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
}

USAGE_PREFLIGHT_GATE_SH = MAC_AGENT / "workflows" / "lib" / "usage-preflight-gate.sh"
USAGE_GATE_TIMEOUT_SECONDS = 15  # should return in well under a second normally; bounds a `coach` hang instead of wedging the caller's lock forever
USAGE_ADVISOR_SH = MAC_AGENT / "workflows" / "lib" / "usage-advisor.sh"
USAGE_ADVISOR_TIMEOUT_SECONDS = 15


async def usage_gate_check(actor: str) -> str | None:
    """Runs usage-preflight-gate.sh <actor> and returns the human-readable
    skip reason (SKIP:'s text with that prefix stripped) if usage is too low
    to safely start, or None if it's fine to proceed.

    Callers are already live replies to a real message.channel, so on SKIP
    they just tell the user directly and return — no pending-job/auto-retry
    needed here, the user can just re-send the command once usage recovers.

    Fails open (returns None, i.e. "proceed") on any subprocess error OR
    timeout — a broken gate must not become a new way for these commands to
    stop working, same posture as every bash call site's
    `|| echo "PROCEED..."`.

    15s timeout: callers hold a lock (FREE_CHAT_LOCK / CODEX_DISPATCH_LOCKS)
    around this call, so an unbounded hang here doesn't just delay one
    message — it permanently wedges that lock. `coach` (the underlying data
    source, via usage-preflight-gate.sh) has been observed mid-session to
    report a query-timeout condition for one of its own provider checks,
    confirming it can genuinely stall — this guards against that propagating
    upward into an unrecoverable lock.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", str(USAGE_PREFLIGHT_GATE_SH), actor,
            env=SUBPROCESS_ENV,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=USAGE_GATE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            return None
        text = out.decode(errors="replace").strip()
    except Exception:
        return None
    if text.startswith("SKIP:"):
        return text[len("SKIP:"):].strip()
    return None


async def usage_headroom_advice() -> HeadroomAdvice:
    """Read the bounded Claude/Codex headroom recommendation.

    This is deliberately separate from ``usage_gate_check``: the gate answers
    "is this provider safe to start?", while this answers "which provider has
    materially more room?".  It is used only at a new free-chat session
    boundary, so a failed/slow advisor cannot interrupt an established
    Claude conversation.  Like the gate, it fails open with unknown data.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", str(USAGE_ADVISOR_SH),
            env=SUBPROCESS_ENV,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=USAGE_ADVISOR_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            # coach is a grandchild of the shell helper; killing only the
            # shell would leave the usage query alive after the caller has
            # already failed open.  Keep the same process-group invariant as
            # the provider runners and wait for the leader to be reaped.
            _kill_process_group(proc)
            await proc.wait()
            return HeadroomAdvice(None, None, None, "advisor timeout")
        return parse_headroom_advice((out or b"").decode(errors="replace"))
    except Exception as exc:
        return HeadroomAdvice(None, None, None, f"advisor error: {exc}")


def _kill_process_group(proc) -> None:
    """`proc.kill()` only SIGKILLs that one direct child — if it spawned its
    own children (e.g. `claude -p`/`codex exec` running a shell tool call),
    those become orphans that keep running after "kill" (confirmed: a
    `sleep 60 & wait` child under a plain `create_subprocess_exec`d bash
    survived `proc.kill()` with the sleep still alive; the same repro under
    `start_new_session=True` + `os.killpg` left nothing behind). Requires the
    process to have been spawned with `start_new_session=True` so it's its
    own process-group leader — falls back to a plain `proc.kill()` if the
    group lookup fails (process already gone, or wasn't a group leader).
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def _force_kill_pgid(pgid: int) -> None:
    """SIGKILL a process group by an already-captured pgid (not re-derived
    from a proc that may have already exited — `os.getpgid(proc.pid)` raises
    ProcessLookupError once that specific pid is gone, even if other members
    of its former group are still alive under the same pgid number)."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


async def _kill_process_group_graceful(proc, grace_seconds: float = 2.0) -> None:
    """SIGTERM first, escalate to SIGKILL only if the process group hasn't
    exited within `grace_seconds`. A bare SIGKILL mid-write (e.g. `codex exec
    -s workspace-write` or `claude -p` with full tool access, both
    mid-timeout-kill) risks leaving a partially-written file behind right
    when the caller's own before/after diff most needs a coherent post-kill
    state to compare against — same reasoning weekly-report.sh already
    applies to its own claude -p timeouts.

    Bug fixed 2026-07-30 (found in an independent Codex code review): the
    original version only ever watched `proc.wait()` — i.e. whether the ONE
    direct child (the group leader) exited — and treated that as "the group
    is done." SIGTERM was sent to the whole group via `os.killpg`, but if the
    leader (e.g. a `claude -p` wrapper) happened to exit quickly while a
    grandchild it spawned (e.g. a `codex`/`agy` subprocess mid-write) was
    still shutting down, `proc.wait()` returned with no TimeoutError, so the
    SIGKILL escalation never ran — exactly the orphaned-mid-write scenario
    this function's own docstring claims to prevent. Now: after the leader
    exits (or times out), separately probe whether the ORIGINAL pgid (kept
    from before the leader exited — `os.getpgid(proc.pid)` would raise once
    that pid is gone) still has any live member via a signal-0 existence
    check, and only declare success once the whole group is confirmed empty.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        _kill_process_group(proc)
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
    except asyncio.TimeoutError:
        _force_kill_pgid(pgid)
        return
    try:
        os.killpg(pgid, 0)  # existence probe only — signal 0 never actually kills
    except (ProcessLookupError, PermissionError, OSError):
        return  # group is confirmed empty (or we can't check further)
    _force_kill_pgid(pgid)


async def run_provider_attempt(
    provider: str,
    args: list[str],
    timeout_seconds: float,
    env: dict[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    on_process_started: Callable[[object], None] | None = None,
    on_process_finished: Callable[[object], None] | None = None,
) -> ProviderResult:
    """Run one text provider attempt under the shared process contract.

    All fallback callers use a new process group, stdin is closed, stdout and
    stderr are combined, and timeout cleanup is graceful before escalation.
    Spawn/timeout failures are returned as a normal `ProviderResult` so a
    later provider can still turn the same request's next gear.
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    args = prepare_provider_argv(provider, args, workdir=cwd)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            env=env or SUBPROCESS_ENV,
            cwd=str(cwd) if cwd is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        return ProviderResult(provider, None, f"spawn failed: {exc}")

    try:
        if on_process_started is not None:
            on_process_started(proc)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        await _kill_process_group_graceful(proc)
        try:
            await asyncio.wait_for(proc.communicate(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        return ProviderResult(provider, None, f"timeout after {timeout_seconds:g}s")
    except OSError as exc:
        return ProviderResult(provider, proc.returncode, f"communication failed: {exc}")
    finally:
        if on_process_finished is not None:
            on_process_finished(proc)
    return ProviderResult(provider, proc.returncode, (stdout or b"").decode(errors="replace").strip())


REPO_LOCK_DIR = Path.home() / ".claude" / "discord-bot" / "repo-locks"


class RepoLockBusy(Exception):
    """Raised by try_acquire_repo_lock() when another PROCESS (not just
    another coroutine in this same process) already holds the lock for this
    resolved repo path."""


def _repo_lock_path(resolved_path: str) -> Path:
    # A hash, not the raw path, as the filename — resolved repo paths can be
    # long/contain characters awkward for a filename, and a hash keeps the
    # lock directory flat and collision-free without needing to sanitize.
    canonical_path = str(canonical_repository_root(resolved_path))
    digest = hashlib.sha256(canonical_path.encode()).hexdigest()[:32]
    return REPO_LOCK_DIR / f"{digest}.lock"


@contextlib.contextmanager
def try_acquire_repo_lock(resolved_path: str):
    """Cross-process, non-blocking file lock keyed by a repo's canonical
    common root (2026-07-31, worktree-safe contract implementation; added
    in the same integration audit that
    fixed the !코덱스 double-fire bug).

    `CODEX_DISPATCH_LOCKS` (codex-bot.py's own asyncio.Lock dict) only
    protects against races WITHIN that one process — it has no visibility
    into discord-bot.py, a separate OS process with its own Python heap.
    Since discord-bot.py's verify-task-v2 retry handlers and codex-bot.py's
    `!코덱스`/`!코덱스대화` dispatch can both end up writing to the same
    resolved repo path (independently of the wake-word bug already fixed —
    e.g. two genuinely different user commands issued close together), a
    second, cross-process layer is needed for the same "reject immediately,
    never queue/wait" semantics the in-process locks already use.

    Uses `flock(2)` via a plain lock file under `REPO_LOCK_DIR`, one per
    canonical repository root (hashed filename). Non-blocking (`LOCK_NB`): raises
    `RepoLockBusy` immediately if any other process — this one's sibling
    coroutine included, though callers should keep using the cheaper
    in-process `asyncio.Lock` as the first check for that case — already
    holds it, rather than waiting. Usage:

        try:
            with try_acquire_repo_lock(str(cwd.resolve())):
                ... do the write-capable work ...
        except RepoLockBusy:
            await message.channel.send("다른 실행이 이미 이 저장소를 건드리고 있습니다 — 끝나면 다시 시도해주세요.")
    """
    try:
        with common_try_acquire_repo_lock(resolved_path):
            yield
    except CommonRepoLockBusy as exc:
        raise RepoLockBusy(resolved_path) from exc


CROSS_BOT_CONTEXT_LIMIT = 20


async def fetch_cross_bot_context(channel, own_bot_id: int, limit: int = CROSS_BOT_CONTEXT_LIMIT) -> str:
    """Cross-bot channel-context bridge (2026-07-30, user-requested,
    explicit design intent — NOT a session merge).

    discord-bot.py's Claude free-chat and codex-bot.py's Codex chat each
    keep their own separate, native conversation continuity
    (`claude -p --resume <session_id>` / `codex exec resume <thread_id>`)
    — that stays untouched. What was missing: since both bots sit in the
    SAME Discord channel and each only ever feeds ITS OWN resumed
    session/thread as context, neither agent had any visibility into what
    the user discussed with the *other* one, even though a human reading
    the channel would see both halves of the conversation naturally.

    This reads the channel's own recent message history (discord.py
    already has this for free — no separate file bridge or config needed)
    and returns everything NOT authored by the calling bot itself, oldest
    first. That includes the sibling bot's own replies AND the user's
    messages to it — deliberately not trying to classify "was this message
    actually directed at the other bot," since that's a fuzzy judgment call
    (wake words, replies, plain commands) that a bounded raw-history dump
    sidesteps entirely. The caller's own bot messages are excluded because
    they're already redundant with what `--resume`/`resume <thread_id>`
    already carries.

    Bounded by `limit` (Discord API messages, not tokens) so this stays
    cheap and can't unboundedly grow a prompt regardless of how long the
    channel's history gets. Returns "" if there's nothing to show (e.g. a
    fresh channel, or a channel where only this bot has ever posted) — the
    caller should skip injecting the block entirely in that case rather
    than send an empty section header.
    """
    lines = []
    async for msg in channel.history(limit=limit):
        if msg.author.id == own_bot_id:
            continue
        content = (msg.content or "").strip()
        if not content:
            continue
        # 2026-07-30 개선: 그냥 "봇"이라고 뭉뚱그리면 정체성 페르소나
        # (MAC_BOT_PERSONA/CODEX_BOT_PERSONA)가 상대를 이름으로 설명해줘도
        # 이 참고자료 블록만 보면 "봇"이 누군지 다시 헷갈릴 수 있다 —
        # 실제 Discord 표시 이름(display_name — 서버 닉네임 우선, 없으면
        # username)을 그대로 라벨로 써서 "[콕스] ..." 식으로 명확히 한다.
        label = msg.author.display_name if msg.author.bot else "사용자"
        lines.append(f"[{label}] {content}")
    lines.reverse()  # channel.history() yields newest-first; want chronological
    return "\n".join(lines)
