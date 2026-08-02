---
name: calendar
description: Query, add, and manage the user's Google Calendar via $HOME/mac-agent/skills/calendar/google_calendar.py after OAuth setup.
---

# Calendar Skill

Use this skill for Google Calendar requests: 일정, 캘린더, Google Calendar, 오늘/내일 일정, 일정 추가/수정/삭제.

## Current integration

Google Calendar OAuth is configured for `sungtac@gmail.com` using:

```bash
python3 "$HOME/mac-agent/skills/calendar/google_calendar.py" calendars
python3 "$HOME/mac-agent/skills/calendar/google_calendar.py" upcoming --days 7 --max-results 20
python3 "$HOME/mac-agent/skills/calendar/google_calendar.py" add --title "제목" --start "2026-05-14T15:00:00+09:00" --end "2026-05-14T16:00:00+09:00"
```

Token/client files live under `~/.edge-agent/secrets/calendar/` and must not be printed.

## Event creation rules

- Title convention: `[핵심키워드] 일정 내용`, compact for calendar readability. Use `--keyword` only when the project/company/category is clear; do not guess.
- `google_calendar.py add` supports Korean date/time helpers via `--date-text`, `--time-text`, `--duration-text`, and `--duration-min` for expressions such as `내일`, `모레`, `5월 20일`, `이번 주 금요일`, `다음 주 월요일`, `오후 3시`, `오전 10시 30분`.
- If date/time, AM/PM, timezone, title, or all-day vs timed status is unclear, ask one concise question before creating the event. The script intentionally rejects ambiguous bare early hours such as `3시`.
- Deadlines, result announcements, trips, and time-unknown events should usually be all-day events; use `--date-text ... --all-day`.
- Preserve original user memo in the event description with `--raw-note`; do not silently rewrite important source text.
- Put location, attendees, links, and preparation items into dedicated description sections when available.
- Default timezone is `Asia/Seoul`.

## Safety rules

- Reading calendars/upcoming events is allowed for clear user requests.
- Adding/modifying events is allowed when the user explicitly asks and date/time/title are clear.
- Deleting events requires explicit confirmation.
- Do not expose raw OAuth client secret, access token, refresh token, or token file contents.
- For ambiguous dates/times, ask one concise clarifying question.

## Reporting style

For Telegram, use compact bullet cards, not markdown tables.

## Quality follow-up

Run `python3 scripts/skill_quality_audit.py --skill <this-skill> --run-tests --json` after creating or changing this skill.
