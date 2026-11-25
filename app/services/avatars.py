"""Profile photo handling: ClamAV scan -> Pillow verify/re-encode (strips
metadata, neutralises polyglots) -> random file name."""
from __future__ import annotations

import io
import uuid

from fastapi import HTTPException, UploadFile
from PIL import Image, ImageOps
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.security import clamav

MAX_AVATAR_BYTES = 6 * 1024 * 1024


def avatar_dir():
    d = settings.uploads_dir / "avatars"
    d.mkdir(parents=True, exist_ok=True)
    return d


def avatar_url(path: str | None) -> str | None:
    return f"/media/avatars/{path}" if path else None


async def save_avatar(upload: UploadFile) -> str:
    data = await upload.read(MAX_AVATAR_BYTES + 1)
    if not data:
        raise HTTPException(400, "Empty image")
    if len(data) > MAX_AVATAR_BYTES:
        raise HTTPException(413, "Profile photo must be under 6 MB")
    scan = await clamav.scan_bytes(data)
    if scan.error:
        raise HTTPException(503, f"Virus scanner unavailable - try again in a minute ({scan.error})")
    if not scan.clean:
        raise HTTPException(400, f"Upload rejected by ClamAV: {scan.signature}")

    def _process() -> bytes:
        try:
            img = Image.open(io.BytesIO(data))
            img.verify()
            img = Image.open(io.BytesIO(data))
            img = ImageOps.exif_transpose(img).convert("RGB")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, "Not a valid image file") from e
        img = ImageOps.fit(img, (320, 320), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, "WEBP", quality=88)
        return out.getvalue()

    webp = await run_in_threadpool(_process)
    name = f"{uuid.uuid4().hex}.webp"
    (avatar_dir() / name).write_bytes(webp)
    return name


def delete_avatar(name: str | None) -> None:
    if not name:
        return
    p = avatar_dir() / name
    if p.exists() and p.parent == avatar_dir():
        p.unlink(missing_ok=True)
