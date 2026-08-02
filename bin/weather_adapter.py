#!/usr/bin/env python3
"""Small keyless weather capability for factual Telegram questions.

Open-Meteo provides forecast data and geocoding without an account or API key.
The adapter is deliberately independent of an LLM: a model cannot invent a
forecast when this request path succeeds, and a network failure is explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


_WEATHER_WORDS = ("날씨", "기온", "기상", "비 와", "비가 와", "우산", "폭염", "눈 와")
_LOCATION_RE = re.compile(r"([가-힣A-Za-z·]{1,20}(?:특별시|광역시|자치시|자치도|시|군|구))")
_KNOWN_LOCATIONS = ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "제주")
_GEOCODING_ALIASES = {
    "서울": "Seoul",
    "부산": "Busan",
    "대구": "Daegu",
    "인천": "Incheon",
    "광주": "Gwangju",
    "대전": "Daejeon",
    "울산": "Ulsan",
    "세종": "Sejong",
    "제주": "Jeju",
    "무안군": "Muan",
}
_WEATHER_CODES = {
    0: "맑음", 1: "대체로 맑음", 2: "부분적으로 흐림", 3: "흐림",
    45: "안개", 48: "상고대 안개", 51: "약한 이슬비", 53: "이슬비",
    55: "강한 이슬비", 61: "약한 비", 63: "비", 65: "강한 비",
    71: "약한 눈", 73: "눈", 75: "강한 눈", 80: "약한 소나기",
    81: "소나기", 82: "강한 소나기", 95: "뇌우", 96: "우박 동반 뇌우", 99: "강한 우박 동반 뇌우",
}


@dataclass(frozen=True)
class WeatherResult:
    location: str
    admin1: str
    date: str
    current_temperature: float | int | None
    apparent_temperature: float | int | None
    minimum: float | int | None
    maximum: float | int | None
    precipitation_probability: int | float | None
    description: str
    source_url: str

    def as_text(self) -> str:
        day = datetime.fromisoformat(self.date).strftime("%-m월 %-d일")
        place = ", ".join(part for part in (self.location, self.admin1) if part)
        temperature = "현재 기온 확인 불가" if self.current_temperature is None else f"현재 {self.current_temperature:g}℃"
        feels = "" if self.apparent_temperature is None else f", 체감 {self.apparent_temperature:g}℃"
        low_high = ""
        if self.minimum is not None and self.maximum is not None:
            low_high = f"최저 {self.minimum:g}℃ / 최고 {self.maximum:g}℃"
        rain = "" if self.precipitation_probability is None else f", 강수확률 {self.precipitation_probability:g}%"
        details = " · ".join(item for item in (temperature + feels, low_high) if item)
        return (
            f"📍 {place}\n오늘({day}) {self.description}입니다.\n"
            f"{details}{rain}.\n"
            f"실시간 조회 출처: {self.source_url}"
        )


def is_weather_request(text: str) -> bool:
    normalized = " ".join(str(text or "").split()).casefold()
    return any(word.casefold() in normalized for word in _WEATHER_WORDS)


def extract_location(text: str) -> str:
    match = _LOCATION_RE.search(str(text or ""))
    if match:
        return match.group(1)
    for location in _KNOWN_LOCATIONS:
        if location in str(text or ""):
            return location
    return "서울"


def _request_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "edge-agent-weather/1.0"})
    with urlopen(request, timeout=12) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("weather endpoint returned an invalid payload")
    return payload


def fetch_weather(text: str, *, now: datetime | None = None) -> WeatherResult:
    if not is_weather_request(text):
        raise ValueError("not a weather request")
    query = extract_location(text)
    geocoding_query = _GEOCODING_ALIASES.get(query, query)
    geo_url = (
        "https://geocoding-api.open-meteo.com/v1/search?name="
        f"{quote(geocoding_query)}&count=10&language=en&format=json"
    )
    geocoding = _request_json(geo_url)
    results = geocoding.get("results") or []
    if not results:
        raise RuntimeError(f"location not found: {query}")
    location = next((item for item in results if item.get("country_code") == "KR"), results[0])
    latitude, longitude = location.get("latitude"), location.get("longitude")
    if latitude is None or longitude is None:
        raise RuntimeError("location coordinates are unavailable")
    tz = ZoneInfo("Asia/Seoul")
    current_date = (now or datetime.now(tz)).astimezone(tz).date().isoformat()
    forecast_url = (
        "https://api.open-meteo.com/v1/forecast?latitude="
        f"{latitude}&longitude={longitude}&current=temperature_2m,apparent_temperature"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"
        "&timezone=Asia%2FSeoul&forecast_days=1"
    )
    forecast = _request_json(forecast_url)
    current = forecast.get("current") or {}
    daily = forecast.get("daily") or {}
    dates = daily.get("time") or [current_date]
    index = dates.index(current_date) if current_date in dates else 0
    code_values = daily.get("weather_code") or [current.get("weather_code", 0)]
    return WeatherResult(
        location=query if query in _GEOCODING_ALIASES else str(location.get("name") or query),
        admin1=str(location.get("admin1") or ""),
        date=str(dates[index]),
        current_temperature=current.get("temperature_2m"),
        apparent_temperature=current.get("apparent_temperature"),
        minimum=(daily.get("temperature_2m_min") or [None])[index],
        maximum=(daily.get("temperature_2m_max") or [None])[index],
        precipitation_probability=(daily.get("precipitation_probability_max") or [None])[index],
        description=_WEATHER_CODES.get(int(code_values[index]), "기상 상태 확인 필요"),
        source_url="https://open-meteo.com/",
    )
