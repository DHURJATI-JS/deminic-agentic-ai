"""Chat attachments for the "analyze" skill.

Upload -> ClamAV scan -> images re-encoded (Pillow, metadata stripped, <=1280px)
for the multimodal model; text / code / CSV / JSON / PDF converted to text.
Metadata lives in Redis per user (att:<uid>:<id>), files on disk per user.
"""
from __future__ import annotations

import io
import json
import uuid

from fastapi import HTTPException, UploadFile
from PIL import Image, ImageOps
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.core.state import redis
from app.security import clamav

MAX_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
TTL = 7 * 24 * 3600
TEXT_EXT = {"txt", "md", "csv", "tsv", "json", "py", "js", "ts", "tsx", "jsx", "html", "htm", "xml", "yaml", "yml", "log",
            "sql", "ini", "toml", "cfg", "java", "c", "h", "cpp", "hpp", "cs", "go", "rs", "rb", "php", "sh", "ps1",
            "css", "scss", "kt", "swift", "r", "env.example", "dockerfile"}
IMAGE_EXT = {"png", "jpg", "jpeg", "webp", "gif", "bmp"}


def _dir(uid: int):
    d = settings.uploads_dir / "attachments" / f"u{int(uid)}"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def save(uid: int, upload: UploadFile) -> dict:
    name = (upload.filename or "file").replace("\\", "/").split("/")[-1][:120] or "file"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    data = await upload.read(MAX_BYTES + 1)
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "Attachments must be under 10 MB")
    scan = await clamav.scan_bytes(data)
    if scan.error:
        raise HTTPException(503, f"Virus scanner unavailable - try again shortly ({scan.error})")
    if not scan.clean:
        raise HTTPException(400, f"Rejected by ClamAV: {scan.signature}")
    aid = uuid.uuid4().hex
    if ext in IMAGE_EXT:
        def _img() -> bytes:
            try:
                img = Image.open(io.BytesIO(data))
                img.verify()
                img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
            except Exception as e:  # noqa: BLE001
                raise HTTPException(400, "Not a valid image") from e
            img.thumbnail((1280, 1280))
            out = io.BytesIO()
            img.save(out, "WEBP", quality=85)
            return out.getvalue()

        blob = await run_in_threadpool(_img)
        path = _dir(uid) / f"{aid}.webp"
        path.write_bytes(blob)
        meta = {"id": aid, "name": name, "kind": "image", "path": path.name, "chars": 0}
    else:
        if ext == "pdf":
            def _pdf() -> str:
                from pypdf import PdfReader

                try:
                    reader = PdfReader(io.BytesIO(data))
                    return "\n\n".join((p.extract_text() or "") for p in reader.pages[:60])
                except Exception as e:  # noqa: BLE001
                    raise HTTPException(400, "Could not read that PDF") from e

            text = await run_in_threadpool(_pdf)
        elif ext in TEXT_EXT or not ext:
            text = data.decode("utf-8", errors="replace")
        else:
            raise HTTPException(415, f".{ext} files are not supported - attach text, code, CSV, JSON, PDF or an image")
        text = text[:MAX_TEXT_CHARS]
        path = _dir(uid) / f"{aid}.txt"
        path.write_text(text, encoding="utf-8")
        meta = {"id": aid, "name": name, "kind": "text", "path": path.name, "chars": len(text)}
    await redis.set(f"att:{uid}:{aid}", json.dumps(meta), ex=TTL)
    return {k: meta[k] for k in ("id", "name", "kind", "chars")}


async def resolve(uid: int, ids: list[str]) -> list[dict]:
    out = []
    for aid in (ids or [])[:3]:
        if not (isinstance(aid, str) and len(aid) == 32 and aid.isalnum()):
            continue
        raw = await redis.get(f"att:{uid}:{aid}")  # scoped by user id -> no cross-account access
        if raw:
            out.append(json.loads(raw))
    return out


def load_content(uid: int, meta: dict) -> tuple[str, str]:
    """Returns (kind, content) - text, or a data: URL for images."""
    p = _dir(uid) / meta["path"]
    if not p.exists():
        return meta["kind"], ""
    if meta["kind"] == "image":
        import base64

        return "image", "data:image/webp;base64," + base64.b64encode(p.read_bytes()).decode()
    return "text", p.read_text(encoding="utf-8", errors="replace")
