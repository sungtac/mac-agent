import unittest
import os
import tempfile
from datetime import date
from pathlib import Path

from skills.calendar.google_calendar import (
    build_description,
    format_calendar_title,
    parse_duration_minutes,
    parse_korean_date,
    parse_korean_time,
)


class CalendarPureContractTests(unittest.TestCase):
    def test_title_and_description_preserve_user_note(self):
        self.assertEqual(format_calendar_title("회의", keyword="프로젝트"), "[프로젝트] 회의")
        self.assertIn("원문 메모", build_description("내용", raw_note="원문"))

    def test_conservative_date_time_parsing(self):
        self.assertEqual(parse_korean_date("내일", now=__import__("datetime").datetime(2026, 7, 31)), date(2026, 8, 1))
        self.assertEqual(parse_korean_time("오후 3시 30분"), (15, 30))
        self.assertEqual(parse_duration_minutes("1시간 30분"), 90)
        with self.assertRaises(ValueError):
            parse_korean_time("3시")
        from skills.calendar.google_calendar import parse_natural_event
        with self.assertRaises(ValueError):
            parse_natural_event("2026-08-03", "오후 3시", duration_min=-5)

    def test_credential_files_must_be_owner_only(self):
        from skills.calendar import google_calendar

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "client.json"
            path.write_text('{"installed":{"client_id":"id","client_secret":"secret"}}', encoding="utf-8")
            os.chmod(path, 0o644)
            original = google_calendar.CLIENT_FILE
            try:
                google_calendar.CLIENT_FILE = path
                with self.assertRaises(SystemExit):
                    google_calendar.client_config()
            finally:
                google_calendar.CLIENT_FILE = original


if __name__ == "__main__":
    unittest.main()
