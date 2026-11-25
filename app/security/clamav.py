"""Minimal async clamd client (INSTREAM over TCP)."""
from __future__ import annotations

import asyncio
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings

CHUNK = 64 * 1024
MAX_SCAN_BYTES = 25 * 1024 * 1024


@dataclass
class ScanResult:
    clean: bool
    signature: str | None = None
    error: str | None = None

    @property
    def status(self) -> str:
        if self.error:
            return "error"
        return "clean" if self.clean else "infected"


@dataclass
class TreeScan:
    scanned: int = 0
    infected: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    skipped: int = 0


async def _open():
    return await asyncio.wait_for(asyncio.open_connection(settings.clamd_host, settings.clamd_port), timeout=5)


async def ping() -> bool:
    try:
        reader, writer = await _open()
        writer.write(b"zPING\0")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(64), timeout=5)
        writer.close()
        return data.strip(b"\0").strip() == b"PONG"
    except Exception:  # noqa: BLE001
        return False


async def scan_bytes(data: bytes) -> ScanResult:
    if len(data) > MAX_SCAN_BYTES:
        return ScanResult(clean=False, error="file too large to scan")
    try:
        reader, writer = await _open()
        writer.write(b"zINSTREAM\0")
        for i in range(0, len(data), CHUNK):
            chunk = data[i : i + CHUNK]
            writer.write(struct.pack("!I", len(chunk)) + chunk)
        writer.write(struct.pack("!I", 0))
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(4096), timeout=60)
        writer.close()
    except Exception as e:  # noqa: BLE001
        return ScanResult(clean=False, error=f"virus scanner unavailable ({type(e).__name__})")
    text = raw.strip(b"\0").decode(errors="replace").strip()
    if text.endswith("OK"):
        return ScanResult(clean=True)
    if text.endswith("FOUND"):
        sig = text.split(":", 1)[-1].replace("FOUND", "").strip()
        return ScanResult(clean=False, signature=sig)
    return ScanResult(clean=False, error=text or "unknown scanner response")


async def scan_file(path: Path) -> ScanResult:
    try:
        size = path.stat().st_size
        if size > MAX_SCAN_BYTES:
            return ScanResult(clean=False, error="file too large to scan")
        data = await asyncio.to_thread(path.read_bytes)
    except OSError as e:
        return ScanResult(clean=False, error=str(e))
    return await scan_bytes(data)


async def scan_tree(root: Path, progress=None) -> TreeScan:
    """Scan every regular file below root (skips .git internals)."""
    result = TreeScan()
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            files.append(Path(dirpath) / fn)
    total = max(len(files), 1)
    for i, f in enumerate(files):
        if f.is_symlink():
            result.skipped += 1
            continue
        r = await scan_file(f)
        result.scanned += 1
        rel = str(f.relative_to(root)).replace("\\", "/")
        if r.error:
            result.errors.append({"path": rel, "error": r.error})
        elif not r.clean:
            result.infected.append({"path": rel, "signature": r.signature})
        if progress and i % 10 == 0:
            await progress(i / total, f"scanned {i}/{total} files")
    return result
