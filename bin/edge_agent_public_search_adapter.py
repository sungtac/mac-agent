"""Bounded public-web search adapter for the Telegram team.

This adapter returns only URLs and titles/snippets observed in the search
provider response. It never fabricates a result and never fetches arbitrary
result URLs on behalf of a user.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import Request, urlopen


SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"
USER_AGENT = "EdgeAgentTelegramSearch/1.0 (+https://duckduckgo.com/)"
MAX_QUERY_CHARS = 240
MAX_RESULTS = 8
MAX_RESULT_TEXT_CHARS = 700
DEFAULT_TIMEOUT_SECONDS = 12


class PublicSearchError(RuntimeError):
    """The verified search adapter could not produce observed results."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._current_url = ""
        self._current_title: list[str] = []
        self._current_snippet: list[str] = []
        self._in_title = False
        self._in_snippet = False

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        value = dict(attrs).get("class") or ""
        return set(value.split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        if tag == "a" and "result__a" in classes:
            href = dict(attrs).get("href") or ""
            self._current_url = _normalize_result_url(href)
            self._current_title = []
            self._in_title = True
        elif "result__snippet" in classes:
            self._current_snippet = []
            self._in_snippet = True

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._current_title.append(data)
        if self._in_snippet:
            self._current_snippet.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._in_title = False
            if self._current_url and self._current_title:
                self.results.append(SearchResult(
                    title=_clean_text("".join(self._current_title)),
                    url=self._current_url,
                    snippet="",
                ))
        elif self._in_snippet and tag in {"a", "div"}:
            self._in_snippet = False
            if self.results and self.results[-1].snippet == "":
                self.results[-1] = SearchResult(
                    title=self.results[-1].title,
                    url=self.results[-1].url,
                    snippet=_clean_text("".join(self._current_snippet)),
                )


def _clean_text(value: str, limit: int = MAX_RESULT_TEXT_CHARS) -> str:
    text = re.sub(r"\s+", " ", html.unescape(value or "")).strip()
    return text[:limit].rstrip()


def _normalize_result_url(value: str) -> str:
    raw = html.unescape(value or "").strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    parsed = urlparse(raw)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path == "/l/":
        raw = unquote(parse_qs(parsed.query).get("uddg", [""])[0])
        parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return parsed._replace(fragment="").geturl()


def search(query: str, *, max_results: int = 5, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> list[SearchResult]:
    normalized = _clean_text(query, MAX_QUERY_CHARS)
    if not normalized:
        raise PublicSearchError("검색어가 비어 있습니다.")
    request = Request(
        f"{SEARCH_ENDPOINT}?{urlencode({'q': normalized})}",
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
    )
    try:
        with urlopen(request, timeout=max(3, min(20, int(timeout_seconds)))) as response:
            body = response.read(1_500_000).decode("utf-8", errors="replace")
    except Exception as exc:
        raise PublicSearchError(f"공개 검색 응답을 확인하지 못했습니다: {type(exc).__name__}") from exc
    parser = _DuckDuckGoParser()
    parser.feed(body)
    unique: list[SearchResult] = []
    seen: set[str] = set()
    for result in parser.results:
        if result.url in seen or not result.title:
            continue
        seen.add(result.url)
        unique.append(result)
        if len(unique) >= max(1, min(MAX_RESULTS, int(max_results))):
            break
    return unique


def render_results(query: str, results: list[SearchResult], *, observed_at: str | None = None) -> str:
    timestamp = observed_at or datetime.now(timezone.utc).isoformat()
    lines = [f"[검증된 공개 검색 결과] 조회 시각: {timestamp}", f"검색어: {_clean_text(query, MAX_QUERY_CHARS)}"]
    if not results:
        lines.append("검색 결과가 0건이었습니다. 링크를 만들어내지 마십시오.")
    for index, result in enumerate(results, 1):
        lines.append(f"{index}. {result.title}\n   URL: {result.url}")
        if result.snippet:
            lines.append(f"   요약: {result.snippet}")
    return "\n".join(lines)
