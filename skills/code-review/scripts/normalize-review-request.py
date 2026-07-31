#!/usr/bin/env python3
"""Normalize code-review aliases and scope into a stable JSON request."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any


ALIASES = (
    "코드리뷰",
    "코드 리뷰",
    "코드 점검",
    "코드점검",
    "코드 품질 검사",
    "코드품질검사",
    "코드 품질검사",
    "코드품질 검사",
)
SCOPES = {"diff", "files", "module", "repo", "snippet"}


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text.casefold())


def has_code_review_intent(text: str) -> bool:
    value = compact(text)
    return any(compact(alias) in value for alias in ALIASES)


def normalize_request(
    text: str,
    *,
    scope: str | None = None,
    paths: list[str] | None = None,
    head_sha: str | None = None,
    base_sha: str | None = None,
) -> dict[str, Any]:
    if not has_code_review_intent(text):
        raise ValueError("request does not contain a supported code-review alias")
    resolved_scope = (scope or "diff").casefold()
    if resolved_scope not in SCOPES:
        raise ValueError(f"unsupported scope: {resolved_scope}")
    if resolved_scope in {"files", "module"} and not paths:
        raise ValueError(f"{resolved_scope} scope requires at least one path")
    result: dict[str, Any] = {
        "intent": "code_review",
        "scope": resolved_scope,
        "request": text.strip(),
        "paths": paths or [],
    }
    if head_sha:
        result["head_sha"] = head_sha
    if base_sha:
        result["base_sha"] = base_sha
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("text", nargs="?", help="request text; stdin is used when omitted")
    parser.add_argument("--scope", choices=sorted(SCOPES), default=None)
    parser.add_argument("--path", action="append", dest="paths", default=[])
    parser.add_argument("--head-sha")
    parser.add_argument("--base-sha")
    args = parser.parse_args()
    text = args.text if args.text is not None else sys.stdin.read()
    try:
        result = normalize_request(
            text,
            scope=args.scope,
            paths=args.paths,
            head_sha=args.head_sha,
            base_sha=args.base_sha,
        )
    except ValueError as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
