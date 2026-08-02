#!/usr/bin/env python3
"""Gate a product recommendation against a product_researcher JSON report.

The gate prevents overclaiming while staying usable across product categories:
consumables with parsed quantities need unit-price evidence; electronics and
other non-quantity products need an explicit non-applicable caveat instead.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ALLOWED_CONFIDENCE = {
    "official_store_parsed",
    "marketplace_search_parsed",
    "conditional_price_parsed",
    "search_path_only",
    "fetch_failed",
}


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _valid_public_https_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)}
    except (OSError, ValueError):
        # A report may be validated offline. Host resolution is not required,
        # but literal private/loopback destinations are always rejected.
        addresses = set()
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal = None
    return not literal or not (literal.is_private or literal.is_loopback or literal.is_link_local or literal.is_reserved)


def validate_report(payload: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if not payload.get("query"):
        issues.append("missing query")
    if not payload.get("generated_at"):
        issues.append("missing generated_at")
    else:
        try:
            generated = str(payload["generated_at"]).replace("Z", "+00:00")
            dt.datetime.fromisoformat(generated)
        except (TypeError, ValueError):
            issues.append("generated_at is not ISO-8601")

    probes = _list_of_dicts(payload.get("sources_checked"))
    if len(probes) < 2:
        issues.append("fewer than two source probes")
    probe_sources = {p.get("source") for p in probes}
    if "danawa" not in probe_sources:
        issues.append("danawa probe missing")
    for probe in probes:
        if not _valid_public_https_url(probe.get("url")):
            issues.append(f"source probe URL is not a public HTTPS URL: {probe.get('source')}")

    candidates = _list_of_dicts(payload.get("candidates"))
    if not candidates:
        issues.append("no candidates")
    priced = [c for c in candidates if c.get("price") or c.get("conditional_price")]
    eligible_priced = [c for c in priced if not c.get("excluded_reason")]
    if not priced:
        issues.append("no priced candidates")
    if priced and not eligible_priced:
        issues.append("no eligible priced candidates after accessory/low-relevance exclusions")
    for candidate in candidates:
        if not _valid_public_https_url(candidate.get("url")):
            issues.append(f"candidate URL is not a public HTTPS URL: {candidate.get('name')}")

    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    caveats = "\n".join(str(c) for c in (summary.get("caveats") or []))
    unit_price_applicable = bool(summary.get("unit_price_applicable")) or any(c.get("quantity_kg") is not None for c in candidates)
    unit_price_count = int(summary.get("unit_price_candidate_count") or 0)
    if unit_price_applicable and unit_price_count <= 0 and not any(c.get("unit_price_per_100g") is not None for c in eligible_priced):
        issues.append("unit-price applicable but no unit-price evidence among priced candidates")
    if not unit_price_applicable and "unit-price" not in caveats.lower():
        issues.append("unit-price non-applicable caveat missing")

    bad_confidence = [c.get("confidence") for c in candidates if c.get("confidence") not in ALLOWED_CONFIDENCE]
    if bad_confidence:
        issues.append(f"unknown confidence labels: {bad_confidence}")
    if any(c.get("confidence") == "confirmed" for c in candidates):
        issues.append("overconfident legacy label 'confirmed' is not allowed")

    has_conditional = any(c.get("confidence") == "conditional_price_parsed" or c.get("conditional_price") for c in candidates)
    has_coupon_caveat = "coupon" in caveats.lower() or any("coupon" in str(c.get("condition") or "").lower() for c in candidates)
    if not has_conditional and not has_coupon_caveat:
        issues.append("no coupon/conditional-price evidence or caveat")
    if "checkout" not in caveats.lower():
        issues.append("checkout caveat missing")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate product research report before recommendation answer")
    parser.add_argument("report_json")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    payload = json.loads(Path(args.report_json).read_text(encoding="utf-8"))
    issues = validate_report(payload)
    result = {"ok": not issues, "issues": issues}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("OK" if not issues else "FAIL")
        for issue in issues:
            print(f"- {issue}")
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
