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
# Codex is the canonical Telegram intake and deputy/coordinator. Claude stays
# the lead reviewer and integrator, but does not receive ordinary room traffic
# unless explicitly addressed or assigned a bounded review.
DEFAULT_ROLE = "codex"
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
_TRANSCRIPT_TIMESTAMP = re.compile(
    r"(?:^|\s)\[\d{4}-\d{2}-\d{2}\s+[^\]\n]{1,80}\]"
)
_FENCED_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_GROUP_ADDRESS = re.compile(
    r"(?<![0-9A-Za-z가-힣_])"
    r"(?:각자|둘\s*다|다같이|같이|모두(?:\s*다)?|전부|얘들아)"
    r"(?![0-9A-Za-z가-힣_])"
)
_DELIBERATION_ACTION = re.compile(
    r"(?:회의|토론|논의)(?:를|을)?\s*"
    r"(?:해|하자|하고|한\s*뒤|해서|후|시작|진행)"
)
_INTEGRATION_REQUEST = re.compile(
    r"(?:마지막(?:에는|에)?\s*)?"
    r"(?:하나의\s+결론|최종\s+결론|의견|결과)"
    r"(?:으로|을|를)?\s*(?:통합|종합|정리|합의)"
    r"|(?:통합|종합)(?:해|하자|하고|한\s*뒤|해서)"
)


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
            # A plain room utterance belongs to the operational intake role.
            # Fan-out is explicit through GROUP_ADDRESS_WORDS; otherwise every
            # provider would repeat the same answer or capability failure.
            return role == default_role
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


def routing_projection(text: str) -> str:
    """Return only the current user's routing directive.

    Telegram exports and pasted chat histories contain old vocatives such as
    ``로다야``.  Those words are evidence supplied to the current request,
    not fresh addresses.  Keep the full text for the provider prompt, but
    exclude fenced/blockquote evidence and cut at the first exported
    timestamp when deciding which process may answer.
    """
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = _FENCED_BLOCK.sub(" ", normalized)
    lines = [line for line in normalized.splitlines() if not line.lstrip().startswith(">")]
    candidate = "\n".join(lines)
    timestamp = _TRANSCRIPT_TIMESTAMP.search(candidate)
    if timestamp:
        candidate = candidate[: timestamp.start()]
    return " ".join(candidate.split()).strip()


def is_group_address(text: str) -> bool:
    """Return whether the current directive explicitly addresses the group.

    Korean nouns often contain a group word as a prefix. For example,
    "각자의 인격" describes each agent; it does not call every bot. Match
    only a standalone address token so narrative questions stay with the
    single default intake.
    """
    return bool(_GROUP_ADDRESS.search(routing_projection(text)))


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


def _has_codex_delegation_object(text: str, targets: set[str]) -> bool:
    """Detect a non-Codex role used as the object of a Codex handoff.

    ``코덱스야 클로드에게 맡겨줘`` addresses Codex and names Claude as the
    delegated recipient.  Treating ``클로드에게`` as a second wake word makes
    Claude answer the raw request as well, before Codex can create a durable
    delegation.  Coordinated addresses such as ``클로드와 안티야`` do not use
    this object form and remain multi-target requests.
    """
    if "codex" not in targets:
        return False
    for raw_token in text.strip().split():
        token = _strip_punctuation(raw_token)
        for particle in ("에게", "한테"):
            if not token.endswith(particle) or token == particle:
                continue
            if _alias_role(token[: -len(particle)]) not in {None, "codex"}:
                return True
    return False


def classify(text: str, *, default_role: str = DEFAULT_ROLE) -> IngressDecision:
    normalized = " ".join(str(text or "").split()).strip()
    if not normalized:
        return IngressDecision("blocked", frozenset(), "")
    directive = routing_projection(text)
    targets = _mention_targets(directive) | _wake_targets(directive)
    if UNMATCHED in targets:
        return IngressDecision("blocked", frozenset(targets), _strip_addresses(normalized))
    if _has_codex_delegation_object(normalized, targets):
        targets = {"codex"}
    if targets:
        return IngressDecision("targeted", frozenset(targets), _strip_addresses(normalized))
    if is_group_address(directive):
        return IngressDecision("broadcast", frozenset(AGENT_ROLES), _strip_addresses(normalized))
    return IngressDecision("default", frozenset(), _strip_addresses(normalized))


def is_deliberation_request(text: str) -> bool:
    """Return whether a room message asks the agents to compare and integrate."""
    normalized = " ".join(str(text or "").split()).casefold()
    return any(marker.casefold() in normalized for marker in DELIBERATION_MARKERS) or bool(
        _DELIBERATION_ACTION.search(normalized)
    ) or bool(_INTEGRATION_REQUEST.search(normalized))


def should_respond(text: str, role: str, *, default_role: str = DEFAULT_ROLE) -> bool:
    return classify(text, default_role=default_role).accepts(role, default_role=default_role)
