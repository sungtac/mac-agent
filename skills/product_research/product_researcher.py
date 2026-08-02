#!/usr/bin/env python3
"""Read-only product research helper for model/price/link verification.

The goal is not to pretend checkout-level certainty. The helper collects public
shopping evidence, computes unit prices, records blocked/search-only paths, and
uses conservative confidence labels so recommendations do not overclaim.
"""

from __future__ import annotations

import argparse
import html
import ipaddress
import json
import os
import re
import socket
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

USER_AGENT = "Mozilla/5.0 (product-researcher; Edge-Agent)"
PUBLIC_SOURCE_HOSTS = frozenset({
    "search.danawa.com",
    "search.shopping.naver.com",
    "www.coupang.com",
    "search.naver.com",
    "brand.naver.com",
    "smartstore.naver.com",
})
ACCESSORY_TERMS = ("호환", "날개", "부품", "리모컨", "커버", "어댑터", "케이블", "충전기", "전용", "보호회로", "12.6v")

CONFIDENCE_ORDER = {
    "official_store_parsed": 0,
    "marketplace_search_parsed": 1,
    "conditional_price_parsed": 2,
    "search_path_only": 3,
    "fetch_failed": 4,
}


@dataclass
class SourceProbe:
    source: str
    url: str
    status: str
    notes: str = ""


@dataclass
class ProductCandidate:
    source: str
    name: str
    url: str
    price: int | None = None
    conditional_price: int | None = None
    condition: str = ""
    shipping_fee: int | None = None
    quantity_kg: float | None = None
    unit_price_per_100g: float | None = None
    confidence: str = "search_path_only"
    link_status: str = "unverified_direct_link"
    excluded_reason: str = ""
    notes: str = ""


def fetch_text(url: str, timeout: int = 20, *, allowed_hosts: set[str] | frozenset[str] = PUBLIC_SOURCE_HOSTS) -> tuple[int, str]:
    _validate_public_http_url(url, allowed_hosts=set(allowed_hosts))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(_PublicRedirectHandler(allowed_hosts=set(allowed_hosts)))
    with opener.open(req, timeout=timeout) as res:  # noqa: S310 - public read-only fetch
        return int(getattr(res, "status", 200) or 200), res.read().decode("utf-8", "ignore")


def _validate_public_http_url(url: str, *, allowed_hosts: set[str] | None = None) -> None:
    """Reject non-HTTP and local/private destinations before fetching.

    Product research accepts a user-supplied brand-store URL, so URL validation
    belongs at the network boundary rather than only in the prompt contract.
    The DNS result is checked as well as the literal hostname to block the usual
    loopback/private-network SSRF targets.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only http(s) public URLs are allowed")
    if parsed.username or parsed.password:
        raise ValueError("URL credentials are not allowed")
    hostname = parsed.hostname.rstrip(".").casefold()
    if allowed_hosts is not None and hostname not in {item.casefold() for item in allowed_hosts}:
        raise ValueError(f"URL host is not allowed: {hostname}")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise ValueError(f"URL host could not be resolved: {hostname}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError("private, loopback, link-local, or reserved destinations are not allowed")


def _safe_display_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if not parsed.username and not parsed.password:
        return url
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, f"[REDACTED]@{host}", parsed.path, parsed.query, parsed.fragment))


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class _PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, *, allowed_hosts: set[str] | None = None) -> None:
        super().__init__()
        self.allowed_hosts = set(PUBLIC_SOURCE_HOSTS) if allowed_hosts is None else allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        target = urllib.parse.urljoin(req.full_url, newurl)
        _validate_public_http_url(target, allowed_hosts=self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, target)


def clean_text(value: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value))).strip()


def decode_mojibake(value: str) -> str:
    value = html.unescape(value)
    try:
        return value.encode("latin1").decode("utf-8")
    except Exception:
        return value


def parse_int_price(value: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", value or "")
    return int(digits) if digits else None


def parse_quantity_kg(text: str) -> float | None:
    if not text:
        return None
    kg_match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*kg", text, re.I)
    if not kg_match:
        return None
    kg = float(kg_match.group(1))
    count = 1
    for pat in (r"(?:x|X|×)\s*([0-9]+)\s*개?", r"[, ]\s*([0-9]+)\s*개", r"([0-9]+)\s*봉"):
        m = re.search(pat, text)
        if m:
            count = int(m.group(1))
            break
    return round(kg * count, 3)


def unit_price_per_100g(price: int | None, quantity_kg: float | None) -> float | None:
    if not price or not quantity_kg or quantity_kg <= 0:
        return None
    return round(price / (quantity_kg * 10), 1)


def accessory_exclusion_reason(query: str, name: str) -> str:
    """Return a reason when a result looks like an accessory, not the target product.

    A term only excludes when it appears in the candidate but not in the user's
    query, so explicit searches like "선풍기 날개" can still surface blade parts.
    """
    lower_query = query.lower()
    lower_name = name.lower()
    # If the user explicitly asks for a part/accessory, do not suppress part results.
    if any(term.lower() in lower_query for term in ACCESSORY_TERMS):
        return ""
    for term in ACCESSORY_TERMS:
        if term.lower() in lower_name:
            return f"candidate appears to be accessory/compatible part: {term}"
    return ""


def stable_search_urls(query: str) -> dict[str, str]:
    quoted = urllib.parse.quote(query)
    return {
        "naver_shopping": f"https://search.shopping.naver.com/search/all?query={quoted}",
        "danawa": f"https://search.danawa.com/dsearch.php?query={quoted}",
        "coupang": f"https://www.coupang.com/np/search?q={quoted}",
        "naver_web": f"https://search.naver.com/search.naver?query={quoted}",
    }


def infer_shipping_fee(text: str) -> int | None:
    if "무료배송" in text:
        return 0
    m = re.search(r"배송비\s*([0-9]{1,3}(?:,[0-9]{3})+)\s*원", text)
    return parse_int_price(m.group(1)) if m else None


def extract_prices_excluding_shipping(text: str) -> list[int]:
    prices: list[int] = []
    for m in re.finditer(r"([0-9]{1,3}(?:,[0-9]{3})+)\s*원", text):
        prefix = text[max(0, m.start() - 12) : m.start()]
        if "배송비" in prefix or "택배비" in prefix:
            continue
        price = parse_int_price(m.group(1))
        if price:
            prices.append(price)
    return prices


def parse_danawa_html(query: str, url: str, text: str, limit: int = 12) -> list[ProductCandidate]:
    candidates: list[ProductCandidate] = []
    for part in text.split("prod_main_info")[1 : limit + 1]:
        cleaned = clean_text(part)
        name = re.sub(r'^">\s*(이미지보기\s*)?', "", cleaned).strip()[:160]
        prices = extract_prices_excluding_shipping(cleaned)
        price = prices[0] if prices else None
        shipping_fee = infer_shipping_fee(cleaned)
        quantity = parse_quantity_kg(name)
        excluded_reason = accessory_exclusion_reason(query, name)
        candidates.append(
            ProductCandidate(
                source="danawa",
                name=name or query,
                url=url,
                price=price,
                shipping_fee=shipping_fee,
                quantity_kg=quantity,
                unit_price_per_100g=unit_price_per_100g(price, quantity),
                confidence="marketplace_search_parsed" if price else "search_path_only",
                link_status="stable_search_url_only",
                excluded_reason=excluded_reason,
                notes="Danawa search result parsed. Seller, option, shipping, coupon, and final checkout price remain unverified.",
            )
        )
    return candidates or [
        ProductCandidate(source="danawa", name=query, url=url, confidence="search_path_only", link_status="stable_search_url_only", notes="no product chunks parsed")
    ]


def scrape_danawa(query: str, limit: int = 12) -> tuple[list[ProductCandidate], SourceProbe]:
    url = stable_search_urls(query)["danawa"]
    try:
        status, text = fetch_text(url, allowed_hosts={"search.danawa.com"})
    except Exception as exc:
        return [ProductCandidate(source="danawa", name=query, url=url, confidence="fetch_failed", link_status="stable_search_url_only", notes=f"fetch failed: {exc}")], SourceProbe("danawa", url, "fetch_failed", str(exc))
    return parse_danawa_html(query, url, text, limit), SourceProbe("danawa", url, f"http_{status}", "search page fetched and parsed")


def parse_naver_brand_html(store_url: str, keywords: Iterable[str], text: str, limit: int = 30, query: str = "") -> list[ProductCandidate]:
    candidates: list[ProductCandidate] = []
    display_store_url = _safe_display_url(store_url)
    seen: set[str] = set()
    keyword_list = [k for k in keywords if k]
    for keyword in keyword_list:
        for match in re.finditer(re.escape(keyword), text):
            chunk = text[max(0, match.start() - 3000) : match.start() + 3000]
            product_nos = re.findall(r'"productNo":(\d+)', chunk)
            names = re.findall(r'"name":"(.*?)"', chunk)
            sale_prices = [parse_int_price(v) for v in re.findall(r'"salePrice":(\d+)', chunk)]
            discounted = [parse_int_price(v) for v in re.findall(r'"discountedSalePrice":(\d+)', chunk)]
            mobile_discounted = [parse_int_price(v) for v in re.findall(r'"mobileDiscountedSalePrice":(\d+)', chunk)]
            if not product_nos or not names:
                continue
            product_no = product_nos[-1]
            if product_no in seen:
                continue
            seen.add(product_no)
            name = decode_mojibake(names[-1])
            if keyword_list and not any(k in name for k in keyword_list):
                continue
            sale_price = next((p for p in reversed(sale_prices) if p), None)
            condition_price = next((p for p in reversed(mobile_discounted + discounted) if p), None)
            quantity = parse_quantity_kg(name)
            effective = condition_price or sale_price
            confidence = "conditional_price_parsed" if condition_price else "official_store_parsed"
            excluded_reason = accessory_exclusion_reason(query or " ".join(keyword_list), name)
            candidates.append(
                ProductCandidate(
                    source="naver_brand",
                    name=name,
                    url=f"{display_store_url.rstrip('/')}/products/{product_no}",
                    price=sale_price,
                    conditional_price=condition_price,
                    condition="Official store page data shows discounted/mobile price; login, coupon ownership, store-follow, membership, card, and checkout state are not verified." if condition_price else "",
                    quantity_kg=quantity,
                    unit_price_per_100g=unit_price_per_100g(effective, quantity),
                    confidence=confidence,
                    link_status="direct_product_url_constructed_not_browser_verified",
                    excluded_reason=excluded_reason,
                    notes="Extracted from Naver brand store embedded page data. Treat direct product URL as less stable than store/search path.",
                )
            )
            if len(candidates) >= limit:
                return candidates
    return candidates or [ProductCandidate(source="naver_brand", name=" ".join(keyword_list), url=display_store_url, confidence="search_path_only", link_status="stable_store_url_only", notes="no matching product data parsed")]


def scrape_naver_brand(store_url: str, keywords: Iterable[str], limit: int = 30) -> tuple[list[ProductCandidate], SourceProbe]:
    display_url = _safe_display_url(store_url)
    try:
        _validate_public_http_url(store_url, allowed_hosts={"brand.naver.com", "smartstore.naver.com"})
    except ValueError as exc:
        return [ProductCandidate(source="naver_brand", name=" ".join(keywords), url=display_url, confidence="fetch_failed", link_status="stable_store_url_only", notes=f"URL rejected: {exc}")], SourceProbe("naver_brand", display_url, "url_rejected", str(exc))
    try:
        status, text = fetch_text(store_url, allowed_hosts={"brand.naver.com", "smartstore.naver.com"})
    except Exception as exc:
        return [ProductCandidate(source="naver_brand", name=" ".join(keywords), url=store_url, confidence="fetch_failed", link_status="stable_store_url_only", notes=f"fetch failed: {exc}")], SourceProbe("naver_brand", store_url, "fetch_failed", str(exc))
    keyword_list = list(keywords)
    return parse_naver_brand_html(store_url, keyword_list, text, limit, query=" ".join(keyword_list)), SourceProbe("naver_brand", store_url, f"http_{status}", "brand store page fetched and parsed")


def probe_search_path(source: str, url: str) -> SourceProbe:
    try:
        status, text = fetch_text(url, timeout=12)
        blocked_terms = ["Too Many Requests", "Access Denied", "I'm a teapot", "captcha", "로봇"]
        if any(term.lower() in text.lower() for term in blocked_terms):
            return SourceProbe(source, url, f"http_{status}_possibly_blocked", "page fetched but appears blocked or rate-limited")
        return SourceProbe(source, url, f"http_{status}", "stable search path fetched; product parsing not implemented")
    except Exception as exc:
        return SourceProbe(source, url, "fetch_failed", str(exc))


def rank_candidates(candidates: list[ProductCandidate]) -> list[ProductCandidate]:
    def key(c: ProductCandidate):
        unit = c.unit_price_per_100g if c.unit_price_per_100g is not None else float("inf")
        price = c.conditional_price or c.price or 10**12
        excluded = 1 if c.excluded_reason else 0
        return (excluded, unit, CONFIDENCE_ORDER.get(c.confidence, 99), price)

    return sorted(candidates, key=key)


def summarize_research(query: str, candidates: list[ProductCandidate], probes: list[SourceProbe]) -> dict[str, object]:
    ranked = rank_candidates(candidates)
    usable = [c for c in ranked if (c.price or c.conditional_price) and not c.excluded_reason]
    conditional = [c for c in usable if c.conditional_price]
    unit_candidates = [c for c in usable if c.unit_price_per_100g is not None]
    quantity_detected = any(c.quantity_kg is not None for c in candidates)
    caveats = [
        "No checkout-page verification; coupon/card/membership/shipping can change final price.",
        "Naver/Coupang search paths may block automated fetching; use stable search paths when direct URLs are unreliable.",
        "Do not call marketplace_search_parsed a final lowest price without user/browser checkout confirmation.",
    ]
    if not quantity_detected:
        caveats.append("Unit-price comparison was not applicable or quantity was not parsed for this product category.")
    if not conditional:
        caveats.append("No coupon/member/card conditional price was parsed; check store coupon pages or checkout manually when coupon accuracy matters.")
    return {
        "query": query,
        "candidate_count": len(candidates),
        "priced_candidate_count": len(usable),
        "conditional_price_count": len(conditional),
        "excluded_candidate_count": len([c for c in candidates if c.excluded_reason]),
        "unit_price_applicable": quantity_detected,
        "unit_price_candidate_count": len(unit_candidates),
        "sources_checked": [asdict(p) for p in probes],
        "best_unit_price": asdict(unit_candidates[0]) if unit_candidates else None,
        "best_effective_price": asdict(usable[0]) if usable else None,
        "caveats": caveats,
    }


def write_markdown(path: Path, payload: dict[str, object]) -> None:
    query = str(payload["query"])
    lines = [f"# Product Research Report: {query}", "", "## Source probes"]
    for p in payload["sources_checked"]:  # type: ignore[index]
        lines.append(f"- {p['source']}: {p['status']} — {p['url']} ({p['notes']})")
    lines.extend(["", "## Summary"])
    summary = payload["summary"]  # type: ignore[index]
    lines.append(f"- candidate_count: {summary['candidate_count']}")
    lines.append(f"- priced_candidate_count: {summary['priced_candidate_count']}")
    lines.append(f"- conditional_price_count: {summary['conditional_price_count']}")
    lines.append(f"- excluded_candidate_count: {summary['excluded_candidate_count']}")
    lines.append(f"- unit_price_applicable: {summary['unit_price_applicable']}")
    lines.append(f"- unit_price_candidate_count: {summary['unit_price_candidate_count']}")
    lines.extend(["", "## Ranked candidates"])
    for c in payload["candidates"]:  # type: ignore[index]
        lines.extend(
            [
                f"- **{c['name']}**",
                f"  - source: {c['source']}",
                f"  - url: {c['url']}",
                f"  - price: {c['price'] if c['price'] is not None else 'unknown'}",
                f"  - conditional_price: {c['conditional_price'] if c['conditional_price'] is not None else 'none'}",
                f"  - condition: {c['condition'] or 'none'}",
                f"  - shipping_fee: {c['shipping_fee'] if c['shipping_fee'] is not None else 'unknown'}",
                f"  - quantity_kg: {c['quantity_kg'] if c['quantity_kg'] is not None else 'unknown'}",
                f"  - unit_price_per_100g: {c['unit_price_per_100g'] if c['unit_price_per_100g'] is not None else 'unknown'}",
                f"  - confidence: {c['confidence']}",
                f"  - link_status: {c['link_status']}",
                f"  - excluded_reason: {c['excluded_reason'] or 'none'}",
                f"  - notes: {c['notes'] or 'none'}",
            ]
        )
    lines.extend(["", "## Caveats"])
    for caveat in summary["caveats"]:
        lines.append(f"- {caveat}")
    _atomic_write_text(path, "\n".join(lines) + "\n")


def run_research(query: str, naver_brand_url: str = "", keywords: list[str] | None = None, probe_all: bool = True) -> dict[str, object]:
    candidates: list[ProductCandidate] = []
    probes: list[SourceProbe] = []
    search_urls = stable_search_urls(query)

    danawa_candidates, danawa_probe = scrape_danawa(query)
    candidates.extend(danawa_candidates)
    probes.append(danawa_probe)

    if naver_brand_url:
        brand_candidates, brand_probe = scrape_naver_brand(naver_brand_url, keywords or query.split())
        candidates.extend(brand_candidates)
        probes.append(brand_probe)

    if probe_all:
        for source in ("naver_shopping", "coupang", "naver_web"):
            probes.append(probe_search_path(source, search_urls[source]))

    ranked = rank_candidates(candidates)
    payload = {
        "query": query,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "search_urls": search_urls,
        "sources_checked": [asdict(p) for p in probes],
        "candidates": [asdict(c) for c in ranked],
    }
    payload["summary"] = summarize_research(query, ranked, probes)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research product prices, coupons, unit prices, and stable links")
    parser.add_argument("query")
    parser.add_argument("--naver-brand-url", default="")
    parser.add_argument("--keyword", action="append", default=[])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", default="")
    parser.add_argument("--no-probe-all", action="store_true", help="skip Naver/Coupang search-path probes")
    args = parser.parse_args(argv)

    payload = run_research(args.query, args.naver_brand_url, args.keyword, probe_all=not args.no_probe_all)
    if args.out:
        out = Path(args.out)
        if out.suffix.lower() == ".json":
            _atomic_write_text(out, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        else:
            write_markdown(out, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for c in payload["candidates"][:10]:  # type: ignore[index]
            price = c["conditional_price"] or c["price"] or "unknown"
            print(f"{c['source']} | {c['confidence']} | {price} | {c['unit_price_per_100g']}원/100g | {c['name']} | {c['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
