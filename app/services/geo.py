"""Reverse geocoding (browser coordinates -> place name) and place search."""
from __future__ import annotations

import logging

from app.core.state import http, redis

log = logging.getLogger("deminic.geo")


def _coords(location: dict | None) -> tuple[float, float] | None:
    if not isinstance(location, dict):
        return None
    try:
        lat, lon = float(location.get("lat")), float(location.get("lon"))
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


async def place_for(location: dict | None) -> str:
    c = _coords(location)
    if not c:
        return ""
    lat, lon = c
    key = f"geo:{lat:.3f}:{lon:.3f}"
    cached = await redis.get(key)
    if cached:
        return cached
    place = f"{lat:.3f}, {lon:.3f}"
    try:
        r = await http().get(
            "https://api.bigdatacloud.net/data/reverse-geocode-client",
            params={"latitude": lat, "longitude": lon, "localityLanguage": "en"},
            timeout=6,
        )
        if r.status_code == 200:
            j = r.json()
            parts = [j.get("locality") or j.get("city"), j.get("principalSubdivision"), j.get("countryName")]
            name = ", ".join(dict.fromkeys(p for p in parts if p))
            if name:
                place = name
    except Exception:  # noqa: BLE001
        log.debug("reverse geocode failed", exc_info=True)
    await redis.set(key, place, ex=7 * 86400)
    return place


async def geocode(name: str) -> dict | None:
    """Place name -> {name, lat, lon, country, timezone} via Open-Meteo geocoding."""
    name = (name or "").strip()[:120]
    if not name:
        return None
    key = f"geocode:{name.lower()}"
    cached = await redis.get(key)
    if cached:
        import json

        return json.loads(cached) if cached != "null" else None
    out = None
    try:
        r = await http().get("https://geocoding-api.open-meteo.com/v1/search",
                             params={"name": name.split(",")[0], "count": 1, "language": "en", "format": "json"}, timeout=8)
        res = (r.json().get("results") or []) if r.status_code == 200 else []
        if res:
            g = res[0]
            out = {"name": ", ".join(p for p in (g.get("name"), g.get("admin1"), g.get("country")) if p),
                   "lat": g["latitude"], "lon": g["longitude"], "country": g.get("country_code", ""),
                   "timezone": g.get("timezone", "")}
    except Exception:  # noqa: BLE001
        log.debug("geocode failed", exc_info=True)
    import json

    await redis.set(key, json.dumps(out), ex=30 * 86400)
    return out
