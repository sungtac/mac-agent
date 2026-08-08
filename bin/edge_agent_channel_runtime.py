#!/usr/bin/env python3
"""Provider-neutral prompt and context contract for every channel.

Telegram, terminal, and future channel adapters may differ in transport, but
they must not assemble different agent meaning. This module owns the shared
team contract, provider identity, capability observations, selected skills,
and bounded logical-session context. It never executes a provider or sends an
external message.
"""

from __future__ import annotations

import argparse
import os
import threading
import time
from pathlib import Path

from agent_profile import normalize_role, render_agent_profile
from edge_agent_capability_preflight import render_prompt as render_capability_preflight
from edge_agent_router_contract import RouterInput
from edge_agent_router_core import route as route_request
from edge_agent_skill_connector import build_skill_context
from edge_agent_team_contract import render_team_contract


RUNTIME_CONTRACT = Path(
    os.environ.get(
        "EDGE_AGENT_RUNTIME_CONTRACT",
        str(Path.home() / ".edge-agent" / "EDGE_AGENT.md"),
    )
).expanduser().resolve()

_HEADLESS_POLICY = (
    "[Antigravity 헤드리스 안전 실행 규칙]\n"
    "이 실행은 사용자에게 권한 확인창을 보여줄 수 없는 headless 세션이다. "
    "제공된 프롬프트·로그·현재 작업공간 안의 증거만 사용하라. "
    "unsandboxed 도구나 권한 승격을 요청하지 말고, launchctl·ps·네트워크·자격증명·외부 메시지 전송은 호출하지 마라. "
    "확인할 수 없는 서비스 상태는 추측하지 말고 '확인 불가'로 표시하라. "
    "파일 검토가 필요하면 현재 작업공간 안에서 읽기·git diff 등 허용된 작업만 사용하라."
)

_SUPPORTED_CHANNELS = frozenset({"terminal", "telegram", "discord", "internal"})
_ROUTER_INPUT_MAX_CHARS = 4000


def _bounded_float_env(name: str, default: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(0.0, min(value, maximum))


_CAPABILITY_PREFLIGHT_TTL_SECONDS = _bounded_float_env(
    "EDGE_AGENT_CAPABILITY_PREFLIGHT_TTL_SECONDS", 30.0, 300.0
)
_PREFLIGHT_CACHE: dict[str, tuple[float, str]] = {}
_PREFLIGHT_CACHE_LOCK = threading.Lock()
_RODA_CAPABILITY_BOUNDARY = (
    "[Roda capability boundary]\n"
    "Roda는 로컬 Ollama 대화 전용이다. 셸, 파일 쓰기, 웹 검색, 자격증명, 서비스 조작, "
    "외부 메시지 전송 권한이 없다. 다른 provider의 capability 상태를 Roda의 권한이나 실행 결과로 추론하지 마라."
)


def _validate_channel(channel: str) -> str:
    selected = str(channel or "").strip().casefold()
    if selected not in _SUPPORTED_CHANNELS:
        choices = ", ".join(sorted(_SUPPORTED_CHANNELS))
        raise ValueError(f"unsupported channel {channel!r}; expected one of: {choices}")
    return selected


def _workspace_cache_key(workspace: str | os.PathLike[str] | None) -> str:
    if not workspace:
        return "<none>"
    try:
        return str(Path(workspace).expanduser().resolve())
    except (OSError, RuntimeError, TypeError, ValueError):
        return str(workspace)


def _render_cached_capability_preflight(
    workspace: str | os.PathLike[str] | None,
) -> str:
    """Bound repeated read-only probes without turning observations into auth."""
    if _CAPABILITY_PREFLIGHT_TTL_SECONDS == 0:
        return render_capability_preflight(workspace)
    key = _workspace_cache_key(workspace)
    now = time.monotonic()
    with _PREFLIGHT_CACHE_LOCK:
        cached = _PREFLIGHT_CACHE.get(key)
        if cached and now - cached[0] < _CAPABILITY_PREFLIGHT_TTL_SECONDS:
            return cached[1]
    try:
        rendered = render_capability_preflight(workspace)
    except (OSError, RuntimeError, TypeError, ValueError, UnicodeError) as exc:
        rendered = (
            "[Capability-first preflight unavailable: unknown; "
            f"observation failed with {type(exc).__name__}. Verify before claiming absence.]"
        )
    with _PREFLIGHT_CACHE_LOCK:
        _PREFLIGHT_CACHE[key] = (now, rendered)
    return rendered


def render_identity(role: str) -> str:
    """Render the same provider identity used by every channel adapter."""
    return f"[영구 아이덴티티 및 톤앤매너 규칙]\n{render_agent_profile(normalize_role(role))}"


def render_routing_context(request: str, *, provider: str) -> str:
    """Render the deterministic routing decision shared by all channels."""
    if not str(request or "").strip():
        return "[공통 입력 라우팅 결정]\n사용자 요청이 아직 입력되지 않음. 다음 입력에서 공통 라우터를 적용한다."
    selected_role = normalize_role(provider)
    router_provider = "gemma" if selected_role == "roda" else selected_role
    # The router only needs bounded classification input. Preserve the full
    # request for the provider and skill selector below; truncating it here
    # prevents RouterInput's hard limit from turning a long message into an
    # avoidable runtime failure.
    route_text = str(request or "")[:_ROUTER_INPUT_MAX_CHARS]
    decision = route_request(RouterInput(route_text, explicit_provider=router_provider))
    payload = decision.to_dict()
    roles = ", ".join(
        f"{item['role']}={item['provider']}" for item in payload["roles"]
    )
    return (
        "[공통 입력 라우팅 결정]\n"
        f"작업 유형: {payload['task_type']}\n"
        f"위험도: {payload['risk_level']}\n"
        f"실행 모드: {payload['execution_mode']}\n"
        f"역할 배정: {roles}\n"
        f"작업공간 필요: {'예' if payload['requires_worktree'] else '아니오'}\n"
        "이 결정은 채널과 무관하게 동일하게 적용한다."
    )


def build_shared_context(
    request: str,
    *,
    provider: str,
    workspace: str | os.PathLike[str] | None = None,
    session_context: str = "",
    extra_context: str = "",
    headless: bool = False,
    channel: str = "internal",
    include_capability_preflight: bool | None = None,
) -> str:
    """Build the channel-independent context envelope."""
    _validate_channel(channel)
    selected_role = normalize_role(provider)
    blocks = [
        render_team_contract(),
        (
            f"공통 운영 계약을 먼저 읽어라: {RUNTIME_CONTRACT}. "
            "계약은 권한 부여가 아니며, 실제 실행 결과와 현재 작업공간을 확인하라."
        ),
        render_identity(selected_role),
    ]
    if headless and selected_role == "antigravity":
        blocks.append(_HEADLESS_POLICY)
    if include_capability_preflight is None:
        include_capability_preflight = selected_role != "roda"
    if include_capability_preflight:
        blocks.append(_render_cached_capability_preflight(workspace))
    elif selected_role == "roda":
        blocks.append(_RODA_CAPABILITY_BOUNDARY)
    blocks.append(render_routing_context(request, provider=selected_role))
    skills = build_skill_context(request, max_chars=6000, include_peer=False)
    if skills:
        blocks.append(skills)
    if session_context:
        blocks.append(session_context.strip())
    if extra_context:
        blocks.append(extra_context.strip())
    return "\n\n".join(block for block in blocks if block)


def build_prompt(
    request: str,
    *,
    provider: str,
    workspace: str | os.PathLike[str] | None = None,
    session_context: str = "",
    extra_context: str = "",
    headless: bool = False,
    channel: str = "internal",
    include_capability_preflight: bool | None = None,
) -> str:
    context = build_shared_context(
        request,
        provider=provider,
        workspace=workspace,
        session_context=session_context,
        extra_context=extra_context,
        headless=headless,
        channel=channel,
        include_capability_preflight=include_capability_preflight,
    )
    return f"{context}\n\n[사용자 요청]\n{request}" if request else context


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the shared Edge Agent channel context")
    parser.add_argument("command", choices=("render",))
    parser.add_argument("--provider", required=True)
    parser.add_argument("--channel", default="terminal")
    parser.add_argument("--workdir", default="")
    parser.add_argument("--request", default=None)
    parser.add_argument("--request-file", default="")
    parser.add_argument("--session-context", default="")
    parser.add_argument("--extra-context", default="")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    channel = _validate_channel(args.channel)
    request = Path(args.request_file).read_text(encoding="utf-8") if args.request_file else (args.request or "")
    print(
        build_prompt(
            request,
            provider=args.provider,
            workspace=args.workdir or None,
            session_context=args.session_context,
            extra_context=args.extra_context,
            headless=args.headless,
            channel=channel,
        ),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
