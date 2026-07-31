#!/usr/bin/env python3
"""Google Calendar OAuth helper for OpenClaw/Sukja.

No third-party Python dependencies. Stores OAuth client and token under
~/.openclaw/secrets by default. Supports read/write calendar scopes after the
user explicitly authorizes them in Google OAuth.

Setup:
  1) Put OAuth Desktop client JSON at ~/.openclaw/secrets/google_calendar_client.json
  2) python3 scripts/google_calendar.py auth-url
  3) Open URL, approve, copy code
  4) python3 scripts/google_calendar.py exchange --code '...'
  5) python3 scripts/google_calendar.py upcoming --days 7
"""
from __future__ import annotations

import argparse
import datetime as dt
import http.server
import json
import os
import socketserver
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SECRETS_DIR = Path(
    os.environ.get("EDGE_AGENT_CALENDAR_SECRETS_DIR", "~/.edge-agent/secrets")
).expanduser().resolve()
CLIENT_FILE = Path(os.environ.get("GOOGLE_CALENDAR_CLIENT_FILE", SECRETS_DIR / "google_calendar_client.json"))
TOKEN_FILE = Path(os.environ.get("GOOGLE_CALENDAR_TOKEN_FILE", SECRETS_DIR / "google_calendar_token.json"))
SCOPES = ["https://www.googleapis.com/auth/calendar"]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/calendar/v3"
DEFAULT_REDIRECT_URI = "http://localhost"
MAX_TITLE_CHARS = 28
DEFAULT_TZ = "Asia/Seoul"
WEEKDAYS_KO = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}


def format_calendar_title(title: str, *, keyword: str | None = None) -> str:
    """Apply the user's calendar title convention: [핵심키워드] 내용.

    The helper is intentionally conservative: it never guesses a keyword. Callers
    pass --keyword when a project/company/category is clear. Long titles are
    shortened for calendar readability while preserving the leading keyword.
    """
    clean = " ".join(str(title).split())
    if keyword:
        prefix = f"[{keyword.strip('[] ')}]"
        if not clean.startswith(prefix):
            clean = f"{prefix} {clean}"
    if len(clean) <= MAX_TITLE_CHARS:
        return clean
    return clean[: MAX_TITLE_CHARS - 1].rstrip() + "…"


def build_description(description: str | None, *, raw_note: str | None = None,
                      attendees: str | None = None, links: str | None = None,
                      materials: str | None = None) -> str | None:
    """Build a structured Calendar description while preserving raw input."""
    sections: list[str] = []
    if description:
        sections.append(str(description).strip())
    extras = [("참석자", attendees), ("관련 링크", links), ("준비물", materials)]
    for label, value in extras:
        if value:
            sections.append(f"{label}:\n{str(value).strip()}")
    if raw_note:
        sections.append(f"원문 메모:\n```\n{str(raw_note)}\n```")
    return "\n\n".join(s for s in sections if s).strip() or None


def parse_korean_date(text: str, *, now: dt.datetime | None = None, timezone: str = DEFAULT_TZ) -> dt.date:
    """Parse conservative Korean date expressions for calendar creation.

    Supported examples: 오늘, 내일, 모레, 2026-05-20, 2026.5.20,
    5월 20일, 이번 주 금요일, 다음 주 월요일. Ambiguous expressions raise.
    """
    import re

    tz = ZoneInfo(timezone)
    base = (now or dt.datetime.now(tz)).astimezone(tz).date()
    s = " ".join(text.strip().split())
    if not s:
        raise ValueError("date text is empty")
    if "오늘" in s:
        return base
    if "내일" in s:
        return base + dt.timedelta(days=1)
    if "모레" in s:
        return base + dt.timedelta(days=2)
    m = re.search(r"(20\d{2})[-./년\s]+(\d{1,2})[-./월\s]+(\d{1,2})", s)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일?", s)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        candidate = dt.date(base.year, month, day)
        # Future-oriented default: if already past this year, use next year.
        if candidate < base:
            candidate = dt.date(base.year + 1, month, day)
        return candidate
    m = re.search(r"(?:(이번|다음)\s*주\s*)?([월화수목금토일])\s*요일?", s)
    if m:
        week_hint = m.group(1) or "이번"
        target = WEEKDAYS_KO[m.group(2)]
        delta = target - base.weekday()
        if week_hint == "다음":
            delta += 7
        elif delta < 0:
            # Future-oriented default for bare/이번 weekday.
            delta += 7
        return base + dt.timedelta(days=delta)
    raise ValueError(f"unsupported or ambiguous date expression: {text}")


def parse_korean_time(text: str) -> tuple[int, int]:
    """Parse conservative Korean time expressions like 오후 3시 30분."""
    import re

    s = " ".join(text.strip().split())
    if not s:
        raise ValueError("time text is empty")
    m = re.search(r"(오전|오후|아침|저녁|밤)?\s*(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분?)?", s)
    if not m:
        m = re.search(r"(오전|오후|아침|저녁|밤)?\s*(\d{1,2}):(\d{2})", s)
    if not m:
        raise ValueError(f"unsupported or ambiguous time expression: {text}")
    marker = m.group(1) or ""
    hour = int(m.group(2))
    minute = int(m.group(3) or 0)
    if hour > 23 or minute > 59:
        raise ValueError(f"invalid time expression: {text}")
    if marker in {"오후", "저녁", "밤"} and hour < 12:
        hour += 12
    if marker in {"오전", "아침"} and hour == 12:
        hour = 0
    # Bare 1~7시 is ambiguous enough to require AM/PM.
    if not marker and 1 <= hour <= 7:
        raise ValueError("AM/PM is required for bare 1~7시")
    return hour, minute


def parse_duration_minutes(text: str | None, default: int = 60) -> int:
    import re

    if not text:
        return default
    s = text.strip()
    total = 0
    h = re.search(r"(\d+)\s*시간", s)
    m = re.search(r"(\d+)\s*분", s)
    if h:
        total += int(h.group(1)) * 60
    if m:
        total += int(m.group(1))
    if total <= 0:
        raise ValueError(f"unsupported duration expression: {text}")
    return total


def parse_natural_event(date_text: str, time_text: str | None, *, duration_text: str | None = None,
                        duration_min: int | None = None, timezone: str = DEFAULT_TZ,
                        all_day: bool = False) -> tuple[dict[str, str], dict[str, str], bool]:
    event_date = parse_korean_date(date_text, timezone=timezone)
    if all_day or not time_text:
        return {"date": event_date.isoformat()}, {"date": (event_date + dt.timedelta(days=1)).isoformat()}, True
    hour, minute = parse_korean_time(time_text)
    tz = ZoneInfo(timezone)
    start = dt.datetime(event_date.year, event_date.month, event_date.day, hour, minute, tzinfo=tz)
    mins = duration_min if duration_min is not None else parse_duration_minutes(duration_text)
    end = start + dt.timedelta(minutes=mins)
    return (
        {"dateTime": start.isoformat(), "timeZone": timezone},
        {"dateTime": end.isoformat(), "timeZone": timezone},
        False,
    )


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Missing file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def client_config() -> dict[str, str]:
    raw = load_json(CLIENT_FILE)
    cfg = raw.get("installed") or raw.get("web") or raw
    client_id = cfg.get("client_id")
    client_secret = cfg.get("client_secret")
    if not client_id or not client_secret:
        raise SystemExit(f"Invalid OAuth client JSON: {CLIENT_FILE}")
    return {"client_id": client_id, "client_secret": client_secret}


def urlencode(data: dict[str, str]) -> bytes:
    return urllib.parse.urlencode(data).encode("utf-8")


def request_json(url: str, *, method: str = "GET", data: dict[str, str] | None = None, token: str | None = None) -> dict[str, Any]:
    body = urlencode(data) if data is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {e.code} {url}: {detail}")


def build_auth_url(redirect_uri: str) -> str:
    cfg = client_config()
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def cmd_auth_url(args: argparse.Namespace) -> None:
    print(build_auth_url(args.redirect_uri))


def cmd_auth_local(args: argparse.Namespace) -> None:
    redirect_uri = f"http://localhost:{args.port}/"
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *a: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if "code" in params:
                result["code"] = params["code"][0]
                body = "Google Calendar authorization received. You can close this window."
            elif "error" in params:
                result["error"] = params["error"][0]
                body = f"Authorization failed: {result['error']}"
            else:
                body = "No authorization code found."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

    with socketserver.TCPServer(("127.0.0.1", args.port), Handler) as httpd:
        url = build_auth_url(redirect_uri)
        print(json.dumps({"ok": True, "authUrl": url, "redirectUri": redirect_uri}, ensure_ascii=False, indent=2), flush=True)
        if args.open_browser:
            webbrowser.open(url)
        httpd.timeout = args.timeout
        while "code" not in result and "error" not in result:
            httpd.handle_request()
        if result.get("error"):
            raise SystemExit(f"OAuth error: {result['error']}")
    exchange_code(result["code"], redirect_uri)


def exchange_code(code: str, redirect_uri: str) -> None:
    cfg = client_config()
    token = request_json(TOKEN_URL, method="POST", data={
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    })
    token["created_at"] = int(dt.datetime.now(dt.timezone.utc).timestamp())
    save_json(TOKEN_FILE, token)
    print(json.dumps({"ok": True, "tokenFile": str(TOKEN_FILE), "scope": token.get("scope")}, ensure_ascii=False, indent=2))


def cmd_exchange(args: argparse.Namespace) -> None:
    exchange_code(args.code, args.redirect_uri)


def load_token() -> dict[str, Any]:
    token = load_json(TOKEN_FILE)
    if not token.get("access_token"):
        raise SystemExit(f"No access_token in {TOKEN_FILE}; run auth-url/exchange first")
    expires_in = int(token.get("expires_in") or 0)
    created_at = int(token.get("created_at") or 0)
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    if token.get("refresh_token") and expires_in and now > created_at + expires_in - 120:
        token = refresh_token(token)
    return token


def refresh_token(token: dict[str, Any]) -> dict[str, Any]:
    cfg = client_config()
    refreshed = request_json(TOKEN_URL, method="POST", data={
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": token["refresh_token"],
        "grant_type": "refresh_token",
    })
    token.update(refreshed)
    token["created_at"] = int(dt.datetime.now(dt.timezone.utc).timestamp())
    save_json(TOKEN_FILE, token)
    return token


def api_get(path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    token = load_token()["access_token"]
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return request_json(url, token=token)


def api_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = load_token()["access_token"]
    url = API_BASE + path
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {e.code} {url}: {detail}")


def rfc3339(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def cmd_calendars(_: argparse.Namespace) -> None:
    data = api_get("/users/me/calendarList")
    items = [{"id": i.get("id"), "summary": i.get("summary"), "primary": i.get("primary", False)} for i in data.get("items", [])]
    print(json.dumps({"ok": True, "calendars": items}, ensure_ascii=False, indent=2))


def cmd_upcoming(args: argparse.Namespace) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    end = now + dt.timedelta(days=args.days)
    params = {
        "timeMin": rfc3339(now),
        "timeMax": rfc3339(end),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": str(args.max_results),
    }
    data = api_get(f"/calendars/{urllib.parse.quote(args.calendar_id, safe='')}/events", params)
    events = []
    for item in data.get("items", []):
        start = item.get("start", {})
        end_obj = item.get("end", {})
        events.append({
            "id": item.get("id"),
            "summary": item.get("summary", "(제목 없음)"),
            "start": start.get("dateTime") or start.get("date"),
            "end": end_obj.get("dateTime") or end_obj.get("date"),
            "location": item.get("location"),
            "status": item.get("status"),
            "htmlLink": item.get("htmlLink"),
        })
    print(json.dumps({"ok": True, "calendarId": args.calendar_id, "events": events}, ensure_ascii=False, indent=2))


def cmd_add(args: argparse.Namespace) -> None:
    if args.date_text:
        try:
            start_obj, end_obj, parsed_all_day = parse_natural_event(
                args.date_text,
                args.time_text,
                duration_text=args.duration_text,
                duration_min=args.duration_min,
                timezone=args.timezone,
                all_day=args.all_day,
            )
        except ValueError as e:
            raise SystemExit(f"Ambiguous calendar date/time: {e}")
    else:
        if not args.start or not args.end:
            raise SystemExit("Provide --start/--end or use --date-text [--time-text].")
        if not args.all_day and ("T" not in args.start or "T" not in args.end):
            raise SystemExit("Timed events require RFC3339 dateTime values with timezone, e.g. 2026-05-14T15:00:00+09:00. Use --all-day for date-only events.")
        if args.all_day:
            start_obj, end_obj, parsed_all_day = {"date": args.start}, {"date": args.end}, True
        else:
            start_obj, end_obj, parsed_all_day = {"dateTime": args.start, "timeZone": args.timezone}, {"dateTime": args.end, "timeZone": args.timezone}, False
    payload: dict[str, Any] = {"summary": format_calendar_title(args.title, keyword=args.keyword)}
    if args.location:
        payload["location"] = args.location
    description = build_description(
        args.description,
        raw_note=args.raw_note,
        attendees=args.attendees,
        links=args.links,
        materials=args.materials,
    )
    if description:
        payload["description"] = description
    payload["start"] = start_obj
    payload["end"] = end_obj
    payload["extendedProperties"] = {"private": {"createdBy": "openclaw-google-calendar.py", "parsedAllDay": str(parsed_all_day).lower()}}
    result = api_post(f"/calendars/{urllib.parse.quote(args.calendar_id, safe='')}/events", payload)
    print(json.dumps({"ok": True, "event": {"id": result.get("id"), "summary": result.get("summary"), "htmlLink": result.get("htmlLink")}}, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Google Calendar OAuth helper")
    sub = parser.add_subparsers(dest="command", required=True)
    p_auth = sub.add_parser("auth-url")
    p_auth.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI)
    p_auth.set_defaults(func=cmd_auth_url)
    p_local = sub.add_parser("auth-local", help="Start a localhost OAuth callback server and exchange the received code")
    p_local.add_argument("--port", type=int, default=8765)
    p_local.add_argument("--timeout", type=int, default=300)
    p_local.add_argument("--open-browser", action="store_true")
    p_local.set_defaults(func=cmd_auth_local)
    p_ex = sub.add_parser("exchange")
    p_ex.add_argument("--code", required=True)
    p_ex.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI)
    p_ex.set_defaults(func=cmd_exchange)
    sub.add_parser("calendars").set_defaults(func=cmd_calendars)
    p_up = sub.add_parser("upcoming")
    p_up.add_argument("--calendar-id", default="primary")
    p_up.add_argument("--days", type=int, default=7)
    p_up.add_argument("--max-results", type=int, default=20)
    p_up.set_defaults(func=cmd_upcoming)
    p_add = sub.add_parser("add")
    p_add.add_argument("--calendar-id", default="primary")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--keyword", help="핵심 키워드. Adds [keyword] prefix without guessing.")
    p_add.add_argument("--start", help="RFC3339 dateTime, or YYYY-MM-DD with --all-day")
    p_add.add_argument("--end", help="RFC3339 dateTime, or YYYY-MM-DD exclusive with --all-day")
    p_add.add_argument("--date-text", help="Korean/relative date, e.g. 내일, 다음 주 월요일, 5월 20일")
    p_add.add_argument("--time-text", help="Korean time, e.g. 오후 3시, 오전 10시 30분")
    p_add.add_argument("--duration-text", help="Duration, e.g. 2시간, 90분")
    p_add.add_argument("--duration-min", type=int, help="Duration in minutes when using --date-text/--time-text")
    p_add.add_argument("--timezone", default="Asia/Seoul")
    p_add.add_argument("--location")
    p_add.add_argument("--description")
    p_add.add_argument("--raw-note", help="Original user memo to preserve verbatim in the description")
    p_add.add_argument("--attendees", help="Attendees/people to record in the description")
    p_add.add_argument("--links", help="Related links to record in the description")
    p_add.add_argument("--materials", help="Preparation items/materials to record in the description")
    p_add.add_argument("--all-day", action="store_true")
    p_add.set_defaults(func=cmd_add)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
