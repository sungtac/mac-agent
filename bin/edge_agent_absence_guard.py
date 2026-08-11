#!/usr/bin/env python3
"""Evidence gate for claims that a capability or resource is absent.

The dangerous mistake this module prevents is turning a narrow observation
(``not in the current shell``) into a global conclusion (``does not exist``).
It records where discovery happened, never reads secret contents, and rejects
provider results that make a capability/configuration absence claim without a
discovery evidence block.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA = "edge_agent.absence_claim_guard.v1"
MAX_CANDIDATES = 5000
SOURCE_NAME_RE = re.compile(
    r"(?:token|secret|credential|auth|config|setting|key|password|chat|telegram|plist|service)",
    re.IGNORECASE,
)
# The Korean "없다" family (없습니다/없음/없다) idiomatically negates
# whatever noun+particle immediately precedes it ("X가/이/은/는 없다" = "there
# is no X"). Letting that ending pair with a subject noun found up to 80
# characters earlier — the same wide gap the English "missing"/"not found"
# endings need — misreads sentences where an unrelated noun sits between the
# two, e.g. "기존 파일에는 변경이 없다" ("no *changes* to the existing file")
# reads "변경이 없다" (no changes), not a claim that the file itself is
# missing. Keeping the gap tight (particle-only) for this ending specifically
# requires the negated noun to be the word directly in front of "없다".
_NO_EXIST_GAP = r"\s*(?:이|가|은|는|도|만|조차|마저)?\s{0,2}"
ABSENCE_CLAIM_RE = re.compile(
    r"(?:"
    r"(?:token|api\s*key|secret|credential|인증|자격|토큰|(?<![가-힣])키|설정|구성|config(?:uration)?|service|서비스|executable|실행파일|capability|기능|file|파일)"
    r"(?:"
    r"[^.!?\n\\]{0,80}(?:missing|not\s+(?:found|present|configured|available|set)|does\s+not\s+exist|unavailable|찾을\s+수\s+없|미설정|구성되지\s+않)"
    r"|"
    r"" + _NO_EXIST_GAP + r"없(?:습니다|음|다)"
    r")"
    r"|"
    r"(?:missing|not\s+(?:found|present|configured|available|set)|does\s+not\s+exist|unavailable|없(?:습니다|음|다)|찾을\s+수\s+없|미설정|구성되지\s+않)"
    r"[^.!?\n\\]{0,80}(?:token|api\s*key|secret|credential|인증|자격|토큰|(?<![가-힣])키|설정|구성|config(?:uration)?|service|서비스|executable|실행파일|capability|기능|file|파일)"
    r")",
    re.IGNORECASE,
)


# Only strip text that actually has unified-diff shape (header, then
# --- /+++ , then at least one @@ hunk of +/-/space/no-newline lines). A
# bare "diff --git" line with no such structure behind it is left in place
# and still scanned — requiring the real shape closes the earlier bypass
# where a message could hide everything after that line from the guard by
# imitating just the header, without providing an actual diff.
_DIFF_BLOCK_RE = re.compile(
    r"^diff --git \S+ \S+\n"
    r"(?:(?:index [0-9a-fA-F]+\.\.[0-9a-fA-F]+.*|new file mode \d+|deleted file mode \d+|"
    r"similarity index \d+%|rename (?:from|to) .*|copy (?:from|to) .*|Binary files .* differ)\n)*"
    r"--- .*\n\+\+\+ .*\n"
    r"(?:@@.*\n(?:[-+ ].*\n|\\ No newline at end of file\n?)*)+",
    re.MULTILINE,
)


def _strip_diff_blocks(text: str) -> str:
    """Drop unified-diff hunks so quoted code/test literals inside a diff are
    never scanned as the provider's own prose claim."""
    return _DIFF_BLOCK_RE.sub("", text)


def _claim_scan_text(value: Any) -> str:
    """Flatten a provider payload into scannable prose, stripping diff hunks
    per string leaf before any JSON escaping collapses real newlines."""
    if isinstance(value, str):
        return _strip_diff_blocks(value)
    if isinstance(value, Mapping):
        return "\n".join(_claim_scan_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(_claim_scan_text(item) for item in value)
    return str(value)


class UnsupportedAbsenceClaim(ValueError):
    """Raised when an absence claim has no machine-readable search evidence."""


@dataclass(frozen=True)
class CandidateSource:
    source_type: str
    location: str


@dataclass(frozen=True)
class DiscoveryEvidence:
    subject: str
    searched_scopes: tuple[str, ...]
    methods: tuple[str, ...]
    candidate_sources: tuple[CandidateSource, ...]
    complete: bool
    generated_at: str

    def as_dict(self, *, candidate_limit: int = 40) -> dict[str, Any]:
        candidates = self.candidate_sources[:max(0, candidate_limit)]
        return {
            "schema": SCHEMA,
            "subject": self.subject,
            "searched_scopes": list(self.searched_scopes),
            "methods": list(self.methods),
            "candidate_source_count": len(self.candidate_sources),
            "candidate_sources_truncated": len(self.candidate_sources) > len(candidates),
            "candidate_sources": [asdict(item) for item in candidates],
            "complete": self.complete,
            "generated_at": self.generated_at,
        }


def _default_roots(home: Path) -> tuple[Path, ...]:
    return (
        home / ".edge-agent" / "secrets",
        home / "Library" / "LaunchAgents",
        home / ".config",
        home / "Library" / "Application Support",
    )


def _walk_candidate_sources(root: Path, candidates: list[CandidateSource]) -> None:
    if not root.is_dir() or len(candidates) >= MAX_CANDIDATES:
        return
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv"}
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories[:] = [item for item in directories if item not in skip]
        for name in files:
            if not SOURCE_NAME_RE.search(name):
                continue
            candidates.append(CandidateSource("filesystem_candidate", str(Path(current) / name)))
            if len(candidates) >= MAX_CANDIDATES:
                return


def discover_local_sources(
    subject: str = "capability/configuration",
    *,
    home: str | os.PathLike[str] | None = None,
    extra_roots: Iterable[str | os.PathLike[str]] = (),
) -> DiscoveryEvidence:
    """Search common local configuration/service locations without secrets.

    This is intentionally a source inventory, not a credential reader. It
    records environment variable names and candidate file paths only. A
    missing item in one source therefore remains an unknown until all declared
    sources have been searched.
    """
    resolved_home = Path(home or Path.home()).expanduser().resolve()
    roots = list(dict.fromkeys((*_default_roots(resolved_home), *(Path(item).expanduser().resolve() for item in extra_roots))))
    candidates: list[CandidateSource] = []
    env_scope = "environment variable names (values withheld)"
    for name in sorted(os.environ):
        if SOURCE_NAME_RE.search(name):
            candidates.append(CandidateSource("environment_name", name))
    searched_scopes = [env_scope]
    for root in roots:
        searched_scopes.append(str(root))
        _walk_candidate_sources(root, candidates)
    return DiscoveryEvidence(
        subject=subject,
        searched_scopes=tuple(searched_scopes),
        methods=("environment_name_inventory", "bounded_filesystem_candidate_scan"),
        candidate_sources=tuple(candidates[:MAX_CANDIDATES]),
        complete=True,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


def _is_meaningful_evidence_value(value: Any) -> bool:
    """A discovery-evidence key merely being present isn't evidence: an empty
    list/dict/string or None means nothing was actually searched or recorded,
    the same way an empty ``searched_scopes: []`` would fail
    ``scoped_absence_claim``'s own completeness check below."""
    if isinstance(value, (str, list, tuple, dict)):
        return len(value) > 0
    return False


def _has_discovery_evidence(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key in ("discovery_evidence", "searched_scopes", "search_scope"):
            if key in value and _is_meaningful_evidence_value(value[key]):
                return True
        return any(_has_discovery_evidence(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_discovery_evidence(item) for item in value)
    return False


def validate_provider_payload(value: Any) -> dict[str, Any]:
    """Reject capability/configuration absence claims without search evidence."""
    encoded = _claim_scan_text(value)
    if ABSENCE_CLAIM_RE.search(encoded) and not _has_discovery_evidence(value):
        raise UnsupportedAbsenceClaim(
            "capability/configuration absence claim requires discovery_evidence, searched_scopes, or search_scope"
        )
    return {"schema": SCHEMA, "validated": True}


def scoped_absence_claim(subject: str, evidence: DiscoveryEvidence) -> dict[str, Any]:
    """Create the only allowed absence statement: not found in searched scope."""
    if not evidence.complete or not evidence.searched_scopes or not evidence.methods:
        raise UnsupportedAbsenceClaim("incomplete discovery evidence cannot support an absence claim")
    return {
        "schema": SCHEMA,
        "status": "not_found_in_searched_scope",
        "subject": subject,
        "searched_scopes": list(evidence.searched_scopes),
        "methods": list(evidence.methods),
        "candidate_source_count": len(evidence.candidate_sources),
        "generated_at": evidence.generated_at,
    }
