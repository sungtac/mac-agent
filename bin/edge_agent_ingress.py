#!/usr/bin/env python3
"""One deterministic ingress policy shared by every Telegram agent.

The Telegram bridges are separate processes, so routing must be decided from
the message itself with exactly the same rules in every process.  This module
has no Telegram or provider dependency and is intentionally side-effect free.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


AGENT_ROLES = ("claude", "codex", "antigravity", "roda")
DEFAULT_ROLE = "claude"
USERNAMES = {
    "claude": "edgeai_stk_bot",
    "codex": "edgeai_macmini_bot",
    "antigravity": "edgeai_anti_bot",
    "roda": "sukja_hwpx_helper_bot",
}
ALIASES = {
    "claude": "claude",
    "클로드": "claude",
    "codex": "codex",
    "코덱스": "codex",
    "콕스": "codex",
    "antigravity": "antigravity",
    "agy": "antigravity",
    "안티": "antigravity",
    "안티그래비티": "antigravity",
    "roda": "roda",
    "로다": "roda",
}
GROUP_ADDRESS_WORDS = ("각자", "둘 다", "둘다", "다같이", "같이", "모두 다", "모두", "전부", "얘들아")
DELIBERATION_MARKERS = (
    "논의해", "논의하자", "토론해", "토론하자", "의견을 내", "각자 의견",
    "방법들을", "전략을 세워", "함께 검토", "실현 가능한 대화", "회의해",
)
_PARTICLES = ("에게", "한테", "야", "아", "씨", "님", "랑", "과", "와", "도", "만", "가", "는", "를", "을", "은", "이")
_VOCATIVE_PARTICLES = ("야", "아", "씨", "님", "에게", "한테")
UNMATCHED = "__unmatched__"


@dataclass(frozen=True)
class IngressDecision:
    route: str
    targets: frozenset[str]
    cleaned_text: str

    def accepts(self, role: str, *, default_role: str = DEFAULT_ROLE) -> bool:
        if role not in AGENT_ROLES:
            return False
        if self.route == "targeted":
            return role in self.targets
        if self.route == "broadcast":
            return True
        if self.route == "default":
            # A human group utterance is addressed to the room.  The caller
            # may still pass an explicit role for compatibility, but a plain
            # message must not silently become a Claude-only default.
            return role in AGENT_ROLES
        return False


def _strip_punctuation(token: str) -> str:
    while token and unicodedata.category(token[-1])[0] in ("P", "S"):
        token = token[:-1]
    return token


def _strip_particle(token: str) -> str:
    for particle in _PARTICLES:
        if token.endswith(particle) and token != particle:
            return token[: -len(particle)]
    return token


def _alias_role(token: str) -> str | None:
    return ALIASES.get(_strip_particle(_strip_punctuation(token)).casefold())


def _wake_targets(text: str) -> set[str]:
    tokens = text.strip().split()
    if not tokens:
        return set()
    targets: set[str] = set()
    edge_tokens = (tokens[0], tokens[-1])
    for token in edge_tokens:
        role = _alias_role(token)
        if role:
            targets.add(role)
    # A vocative can occur after a conversational lead-in: "근데 클로드야".
    # Bare names in the middle are deliberately ignored, so historical
    # mentions such as "어제 클로드가 이상했어" do not wake Claude.
    for raw_token in tokens:
        token = _strip_punctuation(raw_token)
        base = _strip_particle(token)
        suffix = token[len(base):] if base != token else ""
        if suffix in ("랑", "과", "와"):
            role = ALIASES.get(base.casefold())
            if role:
                targets.add(role)
        for particle in _VOCATIVE_PARTICLES:
            if token.endswith(particle) and token != particle:
                role = ALIASES.get(token[: -len(particle)].casefold())
                if role:
                    targets.add(role)
    # In a coordinated address such as "안티랑 로다는", the second role can
    # carry an ordinary topic/subject particle rather than a vocative one.
    # Recognize those role tokens only when the phrase is demonstrably a
    # coordination (two role-bearing tokens or a coordinating particle), so
    # narrative text such as "어제 클로드가 이상했어" remains unaddressed.
    role_particles: list[tuple[str, str]] = []
    for raw_token in tokens:
        token = _strip_punctuation(raw_token)
        for particle in _PARTICLES:
            if token.endswith(particle) and token != particle:
                role = ALIASES.get(token[: -len(particle)].casefold())
                if role:
                    role_particles.append((role, particle))
                    break
    if len({role for role, _ in role_particles}) >= 2 or any(
        particle in {"랑", "과", "와"} for _, particle in role_particles
    ):
        targets.update(role for role, _ in role_particles)
    return targets


def _mention_targets(text: str) -> set[str]:
    targets: set[str] = set()
    username_to_role = {username.casefold(): role for role, username in USERNAMES.items()}
    for match in re.finditer(r"@[A-Za-z0-9_]+", text):
        role = username_to_role.get(match.group(0)[1:].casefold())
        if role:
            targets.add(role)

    command = re.match(r"^/(\w+)(?:@([^\s]+))?(?:\s|$)", text, re.IGNORECASE)
    if command:
        command_role = ALIASES.get(command.group(1).casefold())
        suffix = command.group(2)
        if suffix:
            targets.add(username_to_role.get(suffix.casefold(), UNMATCHED))
        elif command_role:
            targets.add(command_role)
        else:
            targets.add(UNMATCHED)
    return targets


def _strip_addresses(text: str) -> str:
    cleaned = text
    for username in USERNAMES.values():
        cleaned = re.sub(rf"@{re.escape(username)}(?![A-Za-z0-9_])", "", cleaned, flags=re.IGNORECASE)
    command = re.match(r"^/(\w+)(?:@([^\s]+))?(?:\s|$)", cleaned, re.IGNORECASE)
    if command:
        command_name, suffix = command.group(1), command.group(2)
        if command_name.casefold() in ALIASES:
            cleaned = cleaned[command.end():]
        elif suffix:
            # Preserve a real command name while removing only its addressed
            # bot suffix (e.g. /status@sukja_hwpx_helper_bot -> /status).
            cleaned = "/" + command_name + cleaned[command.end():]
    return " ".join(cleaned.split()).strip()


def classify(text: str, *, default_role: str = DEFAULT_ROLE) -> IngressDecision:
    normalized = " ".join(str(text or "").split()).strip()
    if not normalized:
        return IngressDecision("blocked", frozenset(), "")
    targets = _mention_targets(normalized) | _wake_targets(normalized)
    if UNMATCHED in targets:
        return IngressDecision("blocked", frozenset(targets), _strip_addresses(normalized))
    if targets:
        return IngressDecision("targeted", frozenset(targets), _strip_addresses(normalized))
    if any(word in normalized for word in GROUP_ADDRESS_WORDS):
        return IngressDecision("broadcast", frozenset(AGENT_ROLES), _strip_addresses(normalized))
    return IngressDecision("default", frozenset(), _strip_addresses(normalized))


def is_deliberation_request(text: str) -> bool:
    """Return whether a room message asks the agents to compare and integrate."""
    normalized = " ".join(str(text or "").split()).casefold()
    return any(marker.casefold() in normalized for marker in DELIBERATION_MARKERS)


def should_respond(text: str, role: str, *, default_role: str = DEFAULT_ROLE) -> bool:
    return classify(text, default_role=default_role).accepts(role, default_role=default_role)
