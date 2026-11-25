"""HTTP security middleware.

* WebSecurityMiddleware - per-request CSP nonce + hardened headers, session
  loading (Redis), and **signed double-submit cookie CSRF** protection: the
  ``dm_csrf`` cookie holds ``random.HMAC(random|binding)`` where binding is the
  current session (or anonymous visitor) id. Every unsafe request must echo the
  cookie in ``X-CSRF-Token``; the HMAC ties it to the session so a planted
  cookie is useless.
* IdempotencyMiddleware - **double-submit protection**: unsafe API calls carry
  ``X-Idempotency-Key``; a duplicate while the first is running gets 409 and a
  duplicate after completion replays the original response.
"""
from __future__ import annotations

import hmac
import json
import re
import secrets

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.state import redis
from app.security.crypto import hmac_hex, random_token
from app.security.sessions import ANON_COOKIE, SESSION_COOKIE, load_session, login_token_valid, mark_replaced

CSRF_COOKIE = "dm_csrf"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_IDEM_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


def make_csrf(binding: str) -> str:
    rnd = secrets.token_urlsafe(18)
    return f"{rnd}.{hmac_hex(rnd + '|' + binding, 'csrf')}"


def csrf_valid(token: str | None, binding: str) -> bool:
    if not token or "." not in token or len(token) > 200:
        return False
    rnd, sig = token.split(".", 1)
    return hmac.compare_digest(sig, hmac_hex(rnd + "|" + binding, "csrf"))


def is_https(request: Request) -> bool:
    return request.url.scheme == "https"


def build_csp(nonce: str, host: str) -> str:
    ws = f"ws://{host} wss://{host}" if host else ""
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            f"style-src 'self' 'nonce-{nonce}'",
            "style-src-attr 'unsafe-inline'",
            "img-src 'self' data: blob: https:",
            "media-src 'self' blob: data: https: http:",
            "font-src 'self' data:",
            f"connect-src 'self' {ws}".strip(),
            "frame-src https://www.youtube-nocookie.com https://www.youtube.com",
            "worker-src 'self' blob:",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
    )


def apply_security_headers(response: Response, nonce: str, host: str, path: str) -> None:
    h = response.headers
    if "content-security-policy" not in h:
        h["Content-Security-Policy"] = build_csp(nonce, host)
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "strict-origin-when-cross-origin"
    h["Permissions-Policy"] = "camera=(), microphone=(self), geolocation=(self), payment=(), usb=()"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["X-Permitted-Cross-Domain-Policies"] = "none"
    if "cache-control" not in h:
        # static assets: cache but always revalidate (ETag -> 304); everything else: never cache
        h["Cache-Control"] = "no-cache" if path.startswith(("/static/", "/media/")) else "no-store"


class WebSecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        path = request.url.path
        host = request.headers.get("host", "")

        # ---- session ----------------------------------------------------
        request.state.uid = None
        request.state.session_hash = None
        request.state.session_replaced = False
        loaded = await load_session(request.cookies.get(SESSION_COOKIE))
        if loaded:
            data, th = loaded
            uid = int(data.get("uid"))
            # ONE LOGIN PER ACCOUNT - token checker against the login token stored in the DB.
            if not data.get("replaced") and await login_token_valid(uid, th):
                request.state.uid = uid
                request.state.session_hash = th
            else:
                if not data.get("replaced"):
                    await mark_replaced(th, uid)
                request.state.session_replaced = True
        anon = request.cookies.get(ANON_COOKIE) or ""
        new_anon = False
        if not anon or len(anon) > 64:
            anon = random_token(16)
            new_anon = True
        request.state.anon_id = anon
        binding = f"s:{request.state.session_hash}" if request.state.session_hash else f"a:{anon}"
        request.state.csrf_binding = binding

        if request.state.session_replaced and path.startswith("/api/") and not path.startswith("/api/auth/"):
            resp = JSONResponse({"detail": "You were signed out because your account signed in on another device.",
                                 "code": "session_replaced"}, status_code=401)
            resp.delete_cookie(SESSION_COOKIE, path="/")
            resp.set_cookie(CSRF_COOKIE, make_csrf(binding), httponly=False, samesite="strict",
                            secure=is_https(request), path="/")
            apply_security_headers(resp, nonce, host, path)
            return resp

        # ---- CSRF (signed double-submit cookie) ---------------------------
        if request.method not in SAFE_METHODS and path.startswith("/api/"):
            header = request.headers.get(CSRF_HEADER, "")
            cookie = request.cookies.get(CSRF_COOKIE, "")
            if not header or not hmac.compare_digest(header, cookie) or not csrf_valid(header, binding):
                resp = JSONResponse(
                    {"detail": "CSRF validation failed - refresh the page and try again."}, status_code=403
                )
                resp.set_cookie(CSRF_COOKIE, make_csrf(binding), httponly=False, samesite="strict",
                                secure=is_https(request), path="/")
                if new_anon:
                    resp.set_cookie(ANON_COOKIE, anon, httponly=True, samesite="lax",
                                    secure=is_https(request), path="/", max_age=365 * 86400)
                apply_security_headers(resp, nonce, host, path)
                return resp

        response = await call_next(request)

        # login/logout handlers set request.state.new_binding
        final_binding = getattr(request.state, "new_binding", None) or binding
        current_cookie = request.cookies.get(CSRF_COOKIE, "")
        if getattr(request.state, "new_binding", None) or not csrf_valid(current_cookie, final_binding):
            response.set_cookie(CSRF_COOKIE, make_csrf(final_binding), httponly=False, samesite="strict",
                                secure=is_https(request), path="/")
        if new_anon:
            response.set_cookie(ANON_COOKIE, anon, httponly=True, samesite="lax",
                                secure=is_https(request), path="/", max_age=365 * 86400)
        if request.state.session_replaced and not getattr(request.state, "new_binding", None):
            response.delete_cookie(SESSION_COOKIE, path="/")
        apply_security_headers(response, nonce, host, path)
        return response


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Rejects / replays duplicate submissions identified by X-Idempotency-Key."""

    TTL = 900
    MAX_CACHE = 256 * 1024

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        key = request.headers.get("x-idempotency-key")
        if request.method in SAFE_METHODS or not key or not request.url.path.startswith("/api/"):
            return await call_next(request)
        if not _IDEM_RE.match(key):
            return JSONResponse({"detail": "Malformed X-Idempotency-Key"}, status_code=400)
        owner = f"u{request.state.uid}" if getattr(request.state, "uid", None) else f"a{getattr(request.state, 'anon_id', '')}"
        rkey = f"idem:{owner}:{request.method}:{request.url.path}:{key}"
        if not await redis.set(rkey, "pending", nx=True, ex=self.TTL):
            cached = await redis.get(rkey)
            if not cached or cached == "pending":
                return JSONResponse(
                    {"detail": "Duplicate submission blocked - the same request is already being processed."},
                    status_code=409,
                    headers={"Idempotent-Replay": "in-progress"},
                )
            data = json.loads(cached)
            return Response(
                content=data.get("body", "").encode(),
                status_code=data.get("status", 200),
                media_type=data.get("ct", "application/json"),
                headers={"Idempotent-Replay": "true"},
            )
        try:
            response = await call_next(request)
        except Exception:
            await redis.delete(rkey)
            raise
        body = b""
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            body += chunk if isinstance(chunk, bytes) else chunk.encode()
        if response.status_code >= 500 or response.status_code in (401, 403, 429):
            await redis.delete(rkey)  # allow a genuine retry
        else:
            ct = response.headers.get("content-type", "application/json")
            store = {"status": response.status_code, "ct": ct.split(";")[0]}
            if len(body) <= self.MAX_CACHE and ("json" in ct or "text" in ct):
                store["body"] = body.decode("utf-8", "replace")
            await redis.set(rkey, json.dumps(store), ex=self.TTL)
        new = Response(content=body, status_code=response.status_code)
        new.raw_headers = [
            (k, v) for (k, v) in response.raw_headers if k.lower() not in (b"content-length", b"transfer-encoding")
        ] + [(b"content-length", str(len(body)).encode())]
        return new
