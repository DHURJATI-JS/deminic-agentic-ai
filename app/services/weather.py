"""Weather via Open-Meteo (free, no API key)."""
from __future__ import annotations

from app.core.state import http
from app.services.geo import geocode

WMO = {
    0: ("Clear sky", "☀️"), 1: ("Mainly clear", "\U0001f324️"), 2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"), 45: ("Fog", "\U0001f32b️"), 48: ("Rime fog", "\U0001f32b️"),
    51: ("Light drizzle", "\U0001f326️"), 53: ("Drizzle", "\U0001f326️"), 55: ("Dense drizzle", "\U0001f327️"),
    61: ("Light rain", "\U0001f326️"), 63: ("Rain", "\U0001f327️"), 65: ("Heavy rain", "\U0001f327️"),
    71: ("Light snow", "\U0001f328️"), 73: ("Snow", "❄️"), 75: ("Heavy snow", "❄️"),
    80: ("Rain showers", "\U0001f326️"), 81: ("Rain showers", "\U0001f327️"), 82: ("Violent showers", "⛈️"),
    95: ("Thunderstorm", "⛈️"), 96: ("Thunderstorm with hail", "⛈️"), 99: ("Severe thunderstorm", "⛈️"),
}


def describe(code: int | None) -> tuple[str, str]:
    return WMO.get(int(code or 0), ("Unknown", "\U0001f321️"))


async def forecast(place: str | None = None, lat: float | None = None, lon: float | None = None) -> dict:
    name = place or ""
    if lat is None or lon is None:
        g = await geocode(place or "")
        if not g:
            return {"ok": False, "error": f"Couldn't find a place called '{place}'."}
        lat, lon, name = g["lat"], g["lon"], g["name"]
    r = await http().get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 4,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
        },
        timeout=12,
    )
    if r.status_code != 200:
        return {"ok": False, "error": f"weather service HTTP {r.status_code}"}
    j = r.json()
    cur = j.get("current", {})
    d = j.get("daily", {})
    days = []
    for i, day in enumerate(d.get("time", [])):
        desc, icon = describe(d["weather_code"][i])
        days.append({"date": day, "desc": desc, "icon": icon, "max": d["temperature_2m_max"][i],
                     "min": d["temperature_2m_min"][i], "rain_pct": (d.get("precipitation_probability_max") or [None] * 9)[i]})
    desc, icon = describe(cur.get("weather_code"))
    return {
        "ok": True, "place": name or f"{lat:.2f},{lon:.2f}", "lat": lat, "lon": lon,
        "current": {"temp": cur.get("temperature_2m"), "feels": cur.get("apparent_temperature"),
                    "humidity": cur.get("relative_humidity_2m"), "wind": cur.get("wind_speed_10m"),
                    "precip": cur.get("precipitation"), "desc": desc, "icon": icon},
        "days": days,
    }
