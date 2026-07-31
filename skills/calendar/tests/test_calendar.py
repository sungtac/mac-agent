import unittest
from datetime import date

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


if __name__ == "__main__":
    unittest.main()
