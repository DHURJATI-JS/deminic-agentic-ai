"""Movies & music.

* Movies play ONLY from YouTube: YouTube Data API v3 when the user saved a key,
  otherwise a best-effort parse of the public youtube.com results page.
* "Where to watch": TMDB watch providers (JustWatch data) for the user's
  region when a TMDB key exists, otherwise search links (Netflix, Prime Video,
  Disney+ Hotstar, Apple TV, JustWatch).
* Music: Radio Browser stations (genre / mood / name) - plays alongside the
  chat in the persistent player; YouTube fallback for a specific song.
All results of public lookups are cached in Redis for a while.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from urllib.parse import quote_plus

from app.ai import failsafe
from app.ai.providers import parse_json_loose
from app.core.state import http, redis
from app.models import UserSettings
from app.security.crypto import decrypt

log = logging.getLogger("deminic.media")

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36")
_VIDEO_ID = re.compile(r'"videoId":"([A-Za-z0-9_-]{11})"')  # exact syntax
_VALID_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
TMDB = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/"
RADIO_BASES = ("https://de1.api.radio-browser.info", "https://fi1.api.radio-browser.info",
               "https://de2.api.radio-browser.info", "https://all.api.radio-browser.info")
_MUSIC_STOP = set("a an the some music song songs track tracks play playing put on me please for to of radio station "
                  "stations listen listening want i like any something good nice".split())


def _ck(prefix: str, *parts: object) -> str:
    return prefix + ":" + hashlib.sha1("|".join(str(p).lower() for p in parts).encode()).hexdigest()


async def _cache_get(key: str):
    try:
        v = await redis.get(key)
        return json.loads(v) if v else None
    except Exception:  # noqa: BLE001
        return None


async def _cache_set(key: str, value, ttl: int) -> None:
    try:
        await redis.set(key, json.dumps(value), ex=ttl)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- YouTube
def _walk_video_renderers(node, out: list, limit: int) -> None:
    if len(out) >= limit:
        return
    if isinstance(node, dict):
        vr = node.get("videoRenderer")
        if isinstance(vr, dict) and vr.get("videoId"):
            runs = (vr.get("title") or {}).get("runs") or [{}]
            owner = ((vr.get("ownerText") or {}).get("runs") or [{}])[0].get("text", "")
            out.append({
                "videoId": vr["videoId"],
                "title": runs[0].get("text", "") if runs else "",
                "channel": owner,
                "duration": (vr.get("lengthText") or {}).get("simpleText", ""),
                "thumb": f"https://i.ytimg.com/vi/{vr['videoId']}/hqdefault.jpg",
            })
            return
        for v in node.values():
            _walk_video_renderers(v, out, limit)
    elif isinstance(node, list):
        for v in node:
            _walk_video_renderers(v, out, limit)


async def _youtube_scrape(query: str, limit: int) -> list[dict]:
    r = await http().get("https://www.youtube.com/results", params={"search_query": query, "hl": "en"},
                         headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=12)
    if r.status_code != 200:
        return []
    html = r.text
    out: list[dict] = []
    marker = "var ytInitialData = "
    i = html.find(marker)
    if i != -1:
        j = html.find(";</script>", i)
        try:
            data = json.loads(html[i + len(marker): j])
            _walk_video_renderers(data, out, limit)
        except ValueError:
            out = []
    if not out:
        seen = []
        for vid in _VIDEO_ID.findall(html):
            if vid not in seen:
                seen.append(vid)
            if len(seen) >= limit:
                break
        out = [{"videoId": v, "title": query, "channel": "", "duration": "",
                "thumb": f"https://i.ytimg.com/vi/{v}/hqdefault.jpg"} for v in seen]
    return out


async def _youtube_api(query: str, key: str, limit: int) -> list[dict]:
    r = await http().get("https://www.googleapis.com/youtube/v3/search",
                         params={"part": "snippet", "type": "video", "maxResults": limit, "q": query, "key": key,
                                 "safeSearch": "moderate"}, timeout=12)
    if r.status_code != 200:
        raise ValueError(f"YouTube API HTTP {r.status_code}")
    out = []
    for it in r.json().get("items", []):
        vid = (it.get("id") or {}).get("videoId")
        sn = it.get("snippet") or {}
        if vid:
            out.append({"videoId": vid, "title": sn.get("title", ""), "channel": sn.get("channelTitle", ""),
                        "duration": "", "thumb": ((sn.get("thumbnails") or {}).get("high") or {}).get("url")
                        or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"})
    return out


async def youtube_search(query: str, api_key: str = "", limit: int = 6) -> list[dict]:
    query = " ".join((query or "").split())[:150]
    if not query:
        return []
    key = _ck("yt", query, limit)
    cached = await _cache_get(key)
    if cached is not None:
        return cached
    results: list[dict] = []
    if api_key:
        try:
            results = await _youtube_api(query, api_key, limit)
        except Exception as e:  # noqa: BLE001
            log.info("YouTube API failed (%s) - falling back to public search page", e)
    if not results:
        try:
            results = await _youtube_scrape(query, limit)
        except Exception as e:  # noqa: BLE001
            log.info("YouTube search page failed: %s", e)
            results = []
    results = [r for r in results if _VALID_ID.match(r.get("videoId", ""))]
    for r in results:
        r["title"] = (r.get("title") or "").replace("&amp;", "&")[:200]
    if results:
        await _cache_set(key, results, 1800)
    return results


def youtube_search_url(query: str) -> str:
    return "https://www.youtube.com/results?search_query=" + quote_plus(query)


_TITLE_STOP = set("the a an of and in on to part movie film full hd official trailer watch new latest".split())


def _words(s: str) -> list[str]:
    out = []
    for raw in (s or "").lower().replace(":", " ").replace("-", " ").replace("'", "").split():
        w = "".join(ch for ch in raw if ch.isalnum())
        if w and w not in _TITLE_STOP:
            out.append(w)
    return out


def relevant(video_title: str, movie_title: str) -> bool:
    """The video title must START with the movie title (stop words ignored):
    'Interstellar (2014) Full Movie' -> yes, 'MISSION INTERSTELLAR - ...' -> no."""
    need = _words(movie_title)
    if not need:
        return True
    return _words(video_title)[: len(need)] == need


async def find_movie_video(title: str, year: str = "", api_key: str = "") -> tuple[dict | None, str, list[dict]]:
    """Best YouTube video for a movie: the full movie if YouTube has it,
    otherwise the official trailer. Returns (video, kind, alternatives)."""
    full = await youtube_search(f"{title} {year} full movie".strip(), api_key, limit=6)
    hits = [v for v in full if relevant(v.get("title", ""), title) and "trailer" not in v.get("title", "").lower()]
    if hits:
        return hits[0], "movie", hits[1:4]
    trailers = await youtube_search(f"{title} {year} official trailer".strip(), api_key, limit=6)
    thits = [v for v in trailers if relevant(v.get("title", ""), title)] or trailers[:1]
    if thits:
        return thits[0], "trailer", thits[1:4]
    return None, "none", []


# --------------------------------------------------------------------------- TMDB / where to watch
def _tmdb_auth(key: str) -> tuple[dict, dict]:
    key = key.strip()
    if key.startswith("eyJ"):  # v4 read-access token
        return {"Authorization": f"Bearer {key}", "Accept": "application/json"}, {}
    return {"Accept": "application/json"}, {"api_key": key}


async def _tmdb(key: str, path: str, params: dict | None = None) -> dict | None:
    headers, auth = _tmdb_auth(key)
    r = await http().get(f"{TMDB}{path}", params={**(params or {}), **auth}, headers=headers, timeout=10)
    if r.status_code != 200:
        log.info("TMDB %s -> HTTP %s", path, r.status_code)
        return None
    return r.json()


async def tmdb_search(key: str, query: str, year: str | int | None = None, limit: int = 8) -> list[dict]:
    params = {"query": query[:120], "include_adult": "false", "language": "en-US"}
    if year and str(year).isdigit():
        params["year"] = str(year)
    data = await _tmdb(key, "/search/movie", params)
    out = []
    for m in (data or {}).get("results", [])[:limit]:
        out.append({"tmdb_id": m.get("id"), "title": m.get("title", ""), "year": (m.get("release_date") or "")[:4],
                    "overview": (m.get("overview") or "")[:600],
                    "poster": f"{TMDB_IMG}w342{m['poster_path']}" if m.get("poster_path") else None,
                    "rating": m.get("vote_average")})
    return out


def fallback_watch_links(title: str, region: str = "IN") -> list[dict]:
    q = quote_plus(title)
    r = (region or "IN").lower()
    links = [
        {"name": "Netflix", "url": f"https://www.netflix.com/search?q={q}"},
        {"name": "Prime Video", "url": f"https://www.primevideo.com/search/ref=atv_nb_sug?ie=UTF8&phrase={q}"},
    ]
    if r == "in":
        links.append({"name": "Disney+ Hotstar", "url": f"https://www.hotstar.com/in/search?q={q}"})
    else:
        links.append({"name": "Disney+", "url": f"https://www.disneyplus.com/search?q={q}"})
    links += [
        {"name": "Apple TV", "url": f"https://tv.apple.com/search?term={q}"},
        {"name": "JustWatch", "url": f"https://www.justwatch.com/{r}/search?q={q}"},
        {"name": "YouTube", "url": youtube_search_url(f"{title} full movie")},
    ]
    return links


async def where_to_watch(s: UserSettings, title: str, year: str | int | None = None) -> dict:
    region = (s.watch_region or "IN").upper()[:2]
    out = {"title": title, "year": str(year or ""), "region": region, "source": "links",
           "links": fallback_watch_links(title, region), "flatrate": [], "rent": [], "buy": [], "free": [],
           "link": None, "poster": None, "overview": ""}
    key = decrypt(s.tmdb_api_key_enc)
    if not key:
        return out
    ck = _ck("tmdbw", title, year, region)
    cached = await _cache_get(ck)
    if cached:
        return {**out, **cached}
    try:
        found = await tmdb_search(key, title, year, limit=1)
        if not found:
            return out
        m = found[0]
        prov = await _tmdb(key, f"/movie/{m['tmdb_id']}/watch/providers")
        res = ((prov or {}).get("results") or {}).get(region) or {}

        def names(kind: str) -> list[dict]:
            return [{"name": p.get("provider_name", ""),
                     "logo": f"{TMDB_IMG}w92{p['logo_path']}" if p.get("logo_path") else None}
                    for p in res.get(kind, [])][:10]

        extra = {"source": "tmdb", "title": m["title"] or title, "year": m["year"], "poster": m["poster"],
                 "overview": m["overview"], "link": res.get("link"), "flatrate": names("flatrate"),
                 "rent": names("rent"), "buy": names("buy"), "free": names("free") + names("ads")}
        await _cache_set(ck, extra, 6 * 3600)
        return {**out, **extra}
    except Exception as e:  # noqa: BLE001
        log.info("TMDB lookup failed: %s", e)
        return out


# --------------------------------------------------------------------------- recommendations
MOVIES_SCHEMA = {
    "type": "object",
    "properties": {
        "movies": {
            "type": "array", "maxItems": 8,
            "items": {"type": "object",
                      "properties": {"title": {"type": "string"}, "year": {"type": "string"},
                                     "genre": {"type": "string"}, "why": {"type": "string"}},
                      "required": ["title", "year", "why"]},
        }
    },
    "required": ["movies"],
}


async def recommend_movies(s: UserSettings, uid: int, request: str, count: int = 6, *, background: bool = False) -> list[dict]:
    count = max(1, min(int(count or 6), 8))
    raw, _ = await failsafe.complete(
        s, "personal",
        [{"role": "system", "content": f"You are a film expert. Recommend exactly {count} real, well-known movies that "
                                       "match the request. Use correct titles and release years. 'why' is one short "
                                       "sentence. JSON only."},
         {"role": "user", "content": (request or "a great movie for tonight")[:600]}],
        uid=uid, json_schema=MOVIES_SCHEMA, max_tokens=700, temperature=0.5, background=background, allow_online=False)
    data = parse_json_loose(raw) or {}
    out = []
    for m in data.get("movies") or []:
        if isinstance(m, dict) and m.get("title"):
            out.append({"title": str(m["title"])[:120], "year": str(m.get("year") or "")[:4],
                        "genre": str(m.get("genre") or "")[:60], "why": str(m.get("why") or "")[:240]})
    return out[:count]


# --------------------------------------------------------------------------- radio
def _station(s: dict) -> dict:
    url = s.get("url_resolved") or s.get("url") or ""
    return {"name": (s.get("name") or "Radio").strip()[:120], "url": url, "homepage": s.get("homepage") or "",
            "favicon": s.get("favicon") if (s.get("favicon") or "").startswith("https://") else "",
            "tags": (s.get("tags") or "")[:160], "country": s.get("country") or "", "codec": s.get("codec") or "",
            "bitrate": s.get("bitrate") or 0, "votes": s.get("votes") or 0, "clicks": s.get("clickcount") or 0,
            "https": url.startswith("https://"), "ok": bool(s.get("lastcheckok", 1))}


async def _radio_query(params: dict) -> list[dict] | None:
    for base in RADIO_BASES:
        try:
            r = await http().get(f"{base}/json/stations/search",
                                 params={**params, "hidebroken": "true", "order": "clickcount", "reverse": "true",
                                         "limit": 40},
                                 headers={"User-Agent": "DeminicAI/1.0"}, timeout=8)
            if r.status_code == 200:
                return [_station(s) for s in r.json() if s.get("url_resolved") or s.get("url")]
        except Exception:  # noqa: BLE001
            continue
    return None


async def radio_search(query: str, limit: int = 12) -> list[dict]:
    q = " ".join((query or "").lower().split())[:80]
    if not q:
        q = "chillout"
    ck = _ck("radio", q, limit)
    cached = await _cache_get(ck)
    if cached is not None:
        return cached
    words = [w for w in q.replace(",", " ").split() if w not in _MUSIC_STOP]
    attempts: list[dict] = []
    core = " ".join(words) or q
    attempts.append({"tag": core})
    attempts.append({"name": core})
    for w in sorted(set(words), key=len, reverse=True):
        if w != core:
            attempts.append({"tag": w})
    stations: list[dict] = []
    for params in attempts[:6]:
        got = await _radio_query(params)
        if got is None:
            break
        if got:
            stations = got
            break
    stations = [s for s in stations if s["ok"] and s["url"].startswith(("https://", "http://"))]
    wanted = set(words or [core])

    def tag_match(s: dict) -> int:
        tags = {t.strip().lower() for t in (s.get("tags") or "").split(",")}
        name = (s.get("name") or "").lower()
        if wanted & tags:
            return 0
        if any(w in name for w in wanted):
            return 1
        return 2

    stations.sort(key=lambda s: (tag_match(s), not s["https"], -int(s["clicks"] or 0)))
    stations = stations[:limit]
    await _cache_set(ck, stations, 900)
    return stations
