"""SSRF guard for every server-side fetch of a user/AI supplied URL."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.state import http


class UnsafeURL(ValueError):
    pass


def _ip_is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or (isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped and not _ip_is_public(str(addr.ipv4_mapped)))
    )


async def check_url(url: str, *, allow_private: bool = False) -> str:
    url = (url or "").strip()
    if len(url) > 2048:
        raise UnsafeURL("URL too long")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("Only http(s) URLs are allowed")
    if not parts.hostname:
        raise UnsafeURL("URL has no host")
    if parts.username or parts.password:
        raise UnsafeURL("Credentials in URLs are not allowed")
    if allow_private:
        return url
    host = parts.hostname
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                                              type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UnsafeURL(f"Cannot resolve host {host}") from e
    for info in infos:
        ip = info[4][0]
        if not _ip_is_public(ip):
            raise UnsafeURL("Refusing to fetch private / internal network addresses")
    return url


async def safe_get(url: str, *, max_bytes: int = 2_000_000, timeout: float = 20.0,
                   headers: dict | None = None, allow_private: bool = False) -> httpx.Response:
    """GET with manual redirect handling (each hop re-validated) and a size cap."""
    current = await check_url(url, allow_private=allow_private)
    client = http()
    for _ in range(6):
        req = client.build_request("GET", current, headers=headers or {}, timeout=timeout)
        resp = await client.send(req, stream=True)
        if resp.is_redirect and resp.headers.get("location"):
            await resp.aclose()
            current = await check_url(urljoin(current, resp.headers["location"]), allow_private=allow_private)
            continue
        body = b""
        async for chunk in resp.aiter_bytes():
            body += chunk
            if len(body) > max_bytes:
                break
        await resp.aclose()
        resp._content = body[:max_bytes]  # noqa: SLF001 - materialise the capped body
        return resp
    raise UnsafeURL("Too many redirects")
