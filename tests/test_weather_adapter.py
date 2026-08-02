import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import weather_adapter  # noqa: E402


class WeatherAdapterTests(unittest.TestCase):
    def test_weather_intent_and_location(self):
        self.assertTrue(weather_adapter.is_weather_request("오늘 무안군 날씨 알려줘"))
        self.assertEqual(weather_adapter.extract_location("오늘 무안군 날씨 알려줘"), "무안군")
        self.assertEqual(weather_adapter.extract_location("오늘 날씨 알려줘"), "서울")

    def test_fetch_weather_uses_korean_location_and_formats_result(self):
        payloads = [
            {"results": [{"name": "무안군", "admin1": "전라남도", "country_code": "KR", "latitude": 34.99, "longitude": 126.48}]},
            {
                "current": {"temperature_2m": 30, "apparent_temperature": 34},
                "daily": {
                    "time": ["2026-08-02"],
                    "temperature_2m_min": [25],
                    "temperature_2m_max": [34],
                    "precipitation_probability_max": [20],
                    "weather_code": [1],
                },
            },
        ]

        with patch.object(weather_adapter, "_request_json", side_effect=payloads):
            result = weather_adapter.fetch_weather("오늘 무안군 날씨 알려줘")

        self.assertIn("무안군", result.as_text())
        self.assertIn("맑음", result.as_text())
        self.assertIn("34℃", result.as_text())
        self.assertIn("open-meteo.com", result.as_text())


if __name__ == "__main__":
    unittest.main()
