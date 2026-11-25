"""Jinja2 environment shared by all page routes."""
from __future__ import annotations

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.requests import Request

from app.config import DEVELOPER_EMAIL, DEVELOPER_GITHUB, ROOT
from app.core.utils import render_business_vars

templates = Jinja2Templates(directory=str(ROOT / "templates"))
env = templates.env


def asset_version() -> str:
    """Short hash over static css/js (size + mtime) - changes whenever the UI changes."""
    import hashlib

    h = hashlib.sha1()
    for sub in ("css", "js"):
        for f in sorted((ROOT / "static" / sub).rglob("*")):
            if f.is_file():
                st = f.stat()
                h.update(f"{f.name}:{st.st_size}:{int(st.st_mtime)}".encode())
    return h.hexdigest()[:10]


env.globals.update(
    DEVELOPER_EMAIL=DEVELOPER_EMAIL,
    DEVELOPER_GITHUB=DEVELOPER_GITHUB,
    APP_NAME="Deminic",
    ASSET_VERSION=asset_version(),
)


def _load_icons() -> dict[str, str]:
    """Single source of truth: parse the icon set from static/js/icons.js."""
    import re

    src = (ROOT / "static" / "js" / "icons.js").read_text(encoding="utf-8")
    return dict(re.findall(r"^\s*'([a-z0-9-]+)':\s*'(.*)',\s*$", src, flags=re.M))


_ICONS = _load_icons()


def icon(name: str, cls: str = "w-4 h-4") -> Markup:
    inner = _ICONS.get(name) or _ICONS.get("sparkles", "")
    return Markup(f'<svg class="{escape(cls)}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
                  f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{inner}</svg>')


env.globals["icon"] = icon


def bizvars(text: str, business_name: str = "") -> Markup:
    """Jinja2 filter: substitute the {business_name} variable with the CURRENT
    business name (HTML-escaped) - renames show up everywhere instantly."""
    return Markup(render_business_vars(str(escape(text or "")), business_name, escape=True))


env.filters["bizvars"] = bizvars


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    ctx = {"nonce": getattr(request.state, "csp_nonce", ""), **(context or {})}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
