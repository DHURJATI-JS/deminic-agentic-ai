"""Coding workspace + THE GATE.

Every new change (editor save, AI apply, restore, upload, new file) goes
through ``gate()`` before it may touch disk:

  1. dependency diff vs the previous content (manifests + new imports),
     unpinned packages resolved to the version that would be installed
  2. OSV.dev vulnerability check (blocking) + recommended fixes/alternatives
  3. ClamAV scan of the new content (infected -> block)
  4. secret scan (warning)

Only a "go" verdict writes the file; the previous content is snapshotted
into ``file_versions`` first (timestamped history / restore).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat
import sys
import time
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logs import ulog
from app.models import FileVersion, VulnScan
from app.security import clamav
from app.security.sentinel import redact_secrets
from app.services import osv
from app.services.workspace import PathError, resolve, user_root

ROOTS = ("scratch", "repos", "projects")
MAX_TEXT = 2 * 1024 * 1024
MAX_COPY_FILES = 3000
MAX_COPY_BYTES = 200 * 1024 * 1024
MAX_ZIP_FILES = 5000
MAX_ZIP_BYTES = 300 * 1024 * 1024
_FORBIDDEN_CHARS = set('<>:"/\\|?*')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".bmp", ".avif"}
IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
              ".ico": "image/x-icon", ".svg": "image/svg+xml", ".bmp": "image/bmp", ".avif": "image/avif"}
BINARY_EXT = {".exe", ".dll", ".so", ".dylib", ".bin", ".zip", ".gz", ".tar", ".7z", ".rar", ".pdf", ".woff", ".woff2",
              ".ttf", ".otf", ".mp3", ".mp4", ".mov", ".wav", ".ogg", ".pyc", ".class", ".jar", ".wasm", ".db", ".sqlite"}

WELCOME = '''"""Welcome to your Deminic coding workspace.

Everything you save here passes the security gate first:
OSV.dev checks new dependencies, ClamAV scans the content and secrets are flagged.
Try adding `import requests` below and saving - the gate checks the package.
"""


def greet(name: str) -> str:
    return f"Hello, {name}!"


if __name__ == "__main__":
    print(greet("world"))
'''


class GateBlocked(Exception):
    def __init__(self, path: str, report: dict):
        super().__init__(f"{path}: {report.get('message', 'blocked by the security gate')}")
        self.path = path
        self.report = report


class Protected(PathError):
    """Operation refused on a protected path (workspace containers, .git)."""


def ensure_layout(user_id: int) -> Path:
    root = user_root(user_id)
    (root / "repos").mkdir(exist_ok=True)
    (root / "projects").mkdir(exist_ok=True)
    scratch = root / "scratch"
    if not scratch.exists():
        scratch.mkdir()
        (scratch / "welcome.py").write_text(WELCOME, encoding="utf-8")
    return root


# --------------------------------------------------------------------------- per-chat projects
def project_rel(chat_no: int) -> str:
    return f"projects/chat-{int(chat_no)}"


def ensure_project(user_id: int, chat_no: int) -> str:
    ensure_layout(user_id)
    rel = project_rel(chat_no)
    resolve(user_id, rel).mkdir(parents=True, exist_ok=True)
    return rel


def in_chat_project(path: str, chat_no: int | None) -> str:
    """Resolve an AI/user path for a coding chat: explicit workspace paths
    (scratch/..., repos/..., projects/...) are kept, anything relative goes
    into that chat's project folder."""
    p = (path or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    p = p.strip("/")
    if not p:
        raise PathError("empty path")
    if p.split("/")[0] in ROOTS or not chat_no:
        return p if p.split("/")[0] in ROOTS else f"scratch/{p}"
    return f"{project_rel(chat_no)}/{p}"


def norm_rel(rel: str) -> str:
    rel = (rel or "").replace("\\", "/").strip().strip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts:
        raise PathError("empty path")
    if parts[0] not in ROOTS:
        raise PathError("files live under scratch/, repos/<name>/ or projects/chat-<N>/")
    if any(p.lower() == ".git" or p == ".." for p in parts):
        raise PathError("the .git directory is protected")
    if any(len(p) > 120 for p in parts) or len(parts) > 30:
        raise PathError("path too long")
    if any(ord(c) < 32 for p in parts for c in p):
        raise PathError("control characters are not allowed in names")
    return "/".join(parts)


def clean_segment(name: str) -> str:
    """Sanitise a NEW file/folder name: trims spaces/trailing dots, rejects
    traversal, separators, reserved device names and forbidden characters."""
    name = (name or "").strip().rstrip(". ").strip()
    if not name or name in (".", ".."):
        raise PathError("the name is empty")
    if any(c in _FORBIDDEN_CHARS or ord(c) < 32 for c in name):
        raise PathError('names cannot contain < > : " / \\ | ? * or control characters')
    if name.split(".")[0].lower() in _RESERVED:
        raise PathError(f"'{name}' is a reserved device name")
    if name.lower() == ".git":
        raise PathError("the .git directory is protected")
    if len(name) > 120:
        raise PathError("name too long (120 characters max)")
    return name


def clean_target(rel: str) -> str:
    rel = (rel or "").replace("\\", "/").strip().strip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PathError("path traversal is not allowed")
    return norm_rel("/".join(clean_segment(p) for p in parts))


def is_container(rel: str) -> bool:
    """Workspace containers that must never be renamed: the roots, each clone
    root (repos/<name>) and each chat project (projects/chat-<N>)."""
    parts = rel.split("/")
    return len(parts) == 1 or (parts[0] in ("repos", "projects") and len(parts) == 2)


def safe_path(user_id: int, rel: str) -> tuple[str, Path]:
    rel = norm_rel(rel)
    return rel, resolve(user_id, rel)


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def kind_of(path: Path, sample: bytes | None = None) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in BINARY_EXT:
        return "binary"
    if sample is not None:
        if b"\x00" in sample[:8192]:
            return "binary"
        try:
            sample[:8192].decode("utf-8")
        except UnicodeDecodeError:
            try:
                sample[:8000].decode("utf-8")  # a multibyte char may straddle the cut
            except UnicodeDecodeError:
                return "binary"
    return "text"


def read_text(path: Path) -> tuple[str | None, str | None]:
    """Returns (text, sha) or (None, None) when missing / not text."""
    if not path.exists() or not path.is_file():
        return None, None
    data = path.read_bytes()
    if len(data) > MAX_TEXT or kind_of(path, data) == "binary":
        return None, sha_of(data)
    try:
        return data.decode("utf-8"), sha_of(data)
    except UnicodeDecodeError:
        return None, sha_of(data)


def list_dir(user_id: int, rel: str) -> list[dict]:
    root = ensure_layout(user_id)
    base = root if not rel.strip("/") else resolve(user_id, norm_rel(rel))
    if not base.exists() or not base.is_dir():
        raise PathError("not a directory")
    out = []
    with os.scandir(base) as it:
        for e in it:
            if e.name == ".git" or e.is_symlink():
                continue
            p = Path(e.path)
            rel_p = str(p.relative_to(root)).replace("\\", "/")
            if e.is_dir():
                out.append({"name": e.name, "path": rel_p, "type": "dir"})
            else:
                st = e.stat()
                out.append({"name": e.name, "path": rel_p, "type": "file", "size": st.st_size,
                            "kind": kind_of(p), "modified": int(st.st_mtime)})
    out.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    return out[:2000]


# --------------------------------------------------------------------------- gate
def _project_root(user_id: int, rel: str) -> Path:
    parts = rel.split("/")
    root = user_root(user_id)
    return root / parts[0] / parts[1] if parts[0] in ("repos", "projects") and len(parts) > 2 else root / parts[0]


def _local_names(project: Path) -> set[str]:
    names: set[str] = set()
    count = 0
    for dirpath, dirnames, filenames in os.walk(project):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", ".venv", "venv", "__pycache__")]
        for d in dirnames:
            names.add(d)
        for f in filenames:
            if f.endswith(".py"):
                names.add(f[:-3])
            count += 1
        if count > 4000:
            break
    return names


def _manifest_pins(project: Path, start: Path) -> dict:
    """Pinned versions from the nearest manifests (so imports use the real pin)."""
    pins: dict = {}
    cur = start
    for _ in range(12):
        for name in ("requirements.txt", "pyproject.toml", "Pipfile", "poetry.lock", "package-lock.json", "package.json"):
            f = cur / name
            if f.exists() and f.stat().st_size < MAX_TEXT:
                try:
                    for d in osv.deps_from_text(f.read_text(encoding="utf-8", errors="replace"), name):
                        if d.version:
                            pins.setdefault(d.key, d.version)
                except OSError:
                    pass
        if cur == project or project not in cur.parents:
            break
        cur = cur.parent
    return pins


async def gate(user_id: int, rel: str, new_text: str, old_text: str | None) -> dict:
    t0 = time.monotonic()
    project = _project_root(user_id, rel)
    target = resolve(user_id, rel)
    ext = target.suffix.lower()
    local = _local_names(project) if ext in (".py", ".pyw") and project.exists() else set()
    pins = _manifest_pins(project, target.parent) if project.exists() else {}
    new_deps = osv.deps_from_text(new_text, rel, local_names=local, pins=pins)
    old_deps = osv.deps_from_text(old_text or "", rel, local_names=local, pins=pins) if old_text else []
    old_set = {(d.ecosystem, d.name, d.version, d.spec) for d in old_deps}
    changed = [d for d in new_deps if (d.ecosystem, d.name, d.version, d.spec) not in old_set]
    report: dict = {"path": rel, "checked": [], "findings": [], "unknown": [], "warnings": [], "new_dependencies": len(changed)}
    verdict = "go"
    if changed:
        try:
            res = await osv.scan_deps(changed, verify=True)
            report.update({k: res[k] for k in ("checked", "findings", "unknown")})
            if res["findings"]:
                verdict = "block"
            for u in res["unknown"]:
                report["warnings"].append(f"{u['name']} was not found on {u['ecosystem']} - typo or hallucinated package?")
        except osv.ScannerUnavailable as e:
            verdict = "error"
            report["warnings"].append(f"Vulnerability scanner unavailable ({e}) - nothing was written; try again.")
    scan = await clamav.scan_bytes(new_text.encode("utf-8"))
    report["clamav"] = {"status": scan.status, "signature": scan.signature, "error": scan.error}
    if scan.signature:
        verdict = "block"
    elif scan.error:
        report["warnings"].append(f"ClamAV: {scan.error}")
    _, secrets = redact_secrets(new_text)
    report["secrets"] = secrets
    if secrets:
        report["warnings"].append(f"{secrets} secret-looking value(s) (API keys / private keys) found - keep secrets out of code.")
    report["verdict"] = verdict
    report["duration_ms"] = int((time.monotonic() - t0) * 1000)
    if verdict == "go":
        report["message"] = (f"GO - {len(report['checked'])} new dependenc{'y' if len(report['checked']) == 1 else 'ies'} checked, no known vulnerabilities"
                             if changed else "GO - no new dependencies; content scanned")
    elif verdict == "block" and scan.signature:
        report["message"] = f"BLOCKED - ClamAV detected {scan.signature}"
    elif verdict == "block":
        names = ", ".join(f"{f['name']} {f['version']}" for f in report["findings"][:4])
        report["message"] = f"BLOCKED - known vulnerabilities in {names}. Use the recommended versions/alternatives."
    else:
        report["message"] = "BLOCKED - the vulnerability scanner could not verify the new dependencies"
    return report


async def record_decision(db: AsyncSession, user_id: int, rel: str, report: dict, *, kind: str = "gate",
                          job_id: str | None = None, source: dict | None = None) -> VulnScan:
    result = dict(report)
    if source:
        result["source"] = source
    row = VulnScan(user_id=user_id, target=rel[:500], kind=kind,
                   verdict="go" if report.get("verdict") == "go" else ("error" if report.get("verdict") == "error" else "block"),
                   result=result, job_id=job_id)
    db.add(row)
    await db.flush()
    return row


async def write_with_gate(db: AsyncSession, user_id: int, rel: str, content: str, *, base_sha: str | None = None,
                          force: bool = False, reason: str = "save", job_id: str | None = None,
                          source: dict | None = None) -> dict:
    """Gate + write one text file. Raises GateBlocked / FileExistsError / ValueError (conflict)."""
    rel, path = safe_path(user_id, rel)
    if len(content.encode("utf-8")) > MAX_TEXT:
        raise ValueError("file too large (2 MB max)")
    if path.exists() and path.is_dir():
        raise ValueError("path is a directory")
    old_text, old_sha = read_text(path)
    existed = path.exists()
    if existed and old_text is None:
        raise ValueError("cannot overwrite a binary / oversized file from the editor")
    if not force:
        if base_sha is None and existed:
            raise FileExistsError(old_sha or "")
        if base_sha is not None and existed and base_sha != old_sha:
            raise FileExistsError(old_sha or "")
    report = await gate(user_id, rel, content, old_text)
    scan_row = await record_decision(db, user_id, rel, report, job_id=job_id, source=source)
    report["scan_id"] = scan_row.id
    if report["verdict"] != "go":
        await db.commit()
        await ulog(user_id, f"gate BLOCKED {rel}: {report['message']}", level="warn", src="coding")
        raise GateBlocked(rel, report)
    fv = FileVersion(user_id=user_id, path=rel, content=old_text if existed else None, existed=existed,
                     sha256=old_sha or "", reason=reason, job_id=job_id)
    db.add(fv)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8")
    path.write_bytes(data)
    await db.commit()
    await ulog(user_id, f"gate GO -> wrote {rel} ({len(data)} bytes, version #{fv.id} snapshotted) - {report['message']}", src="coding")
    return {"ok": True, "path": rel, "sha": sha_of(data), "verdict": "go", "report": report, "version_id": fv.id,
            "scan_id": scan_row.id}


async def snapshot_before_delete(db: AsyncSession, user_id: int, rel: str, path: Path) -> None:
    text, sha = read_text(path)
    if text is not None:
        db.add(FileVersion(user_id=user_id, path=rel, content=text, existed=True, sha256=sha or "", reason="delete"))


def _make_writable_and_retry(func, target, *_exc):
    try:
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        func(target)
    except OSError:
        pass


def force_rmtree(path: Path) -> None:
    """rmtree that also removes read-only files (git pack files on Windows)."""
    if not path.exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_make_writable_and_retry)
    else:  # pragma: no cover
        shutil.rmtree(path, onerror=_make_writable_and_retry)


# --------------------------------------------------------------------------- rename / copy / zip
class TargetExists(Exception):
    def __init__(self, rel: str, is_dir: bool):
        super().__init__(f"{rel} already exists")
        self.rel = rel
        self.is_dir = is_dir


def _gate_needed(src_rel: str, dst_rel: str, text: str) -> bool:
    """Gate when the NEW name is a dependency manifest, or when the new name
    gives the content a different dependency meaning (e.g. .txt -> .py)."""
    dst_name = dst_rel.rsplit("/", 1)[-1]
    if osv.is_manifest(dst_name):
        return True
    new = {(d.ecosystem, d.name, d.version) for d in osv.deps_from_text(text, dst_rel)}
    old = {(d.ecosystem, d.name, d.version) for d in osv.deps_from_text(text, src_rel)}
    return bool(new) and new != old


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


async def _gate_or_raise(db: AsyncSession, user_id: int, rel: str, text: str, source: dict) -> dict:
    report = await gate(user_id, rel, text, None)
    row = await record_decision(db, user_id, rel, report, source=source)
    report["scan_id"] = row.id
    if report["verdict"] != "go":
        await db.commit()
        await ulog(user_id, f"gate BLOCKED {rel}: {report['message']}", level="warn", src="coding")
        raise GateBlocked(rel, report)
    return report


async def rename_path(db: AsyncSession, user_id: int, src_rel: str, dst_rel: str, *, overwrite: bool = False) -> dict:
    src_rel, src = safe_path(user_id, src_rel)
    dst_rel = clean_target(dst_rel)
    dst = resolve(user_id, dst_rel)
    await ulog(user_id, f"rename requested: {src_rel} -> {dst_rel}{' (overwrite confirmed)' if overwrite else ''}", src="coding")
    if is_container(src_rel):
        raise Protected(f"{src_rel} is a protected workspace container and cannot be renamed")
    if is_container(dst_rel):
        raise Protected("the new path must be inside scratch/, a cloned repo or a chat project")
    if not src.exists() or src.is_symlink():
        raise FileNotFoundError(src_rel)
    if src_rel == dst_rel:
        raise PathError("the new name is the same as the old one")
    if dst_rel.startswith(src_rel + "/"):
        raise PathError("a folder cannot be moved into itself")
    is_dir = src.is_dir()
    case_only = _same_file(src, dst)
    if dst.exists() and not case_only:
        if not overwrite:
            raise TargetExists(dst_rel, dst.is_dir())
        if dst.is_dir() or is_dir:
            raise Protected("folders are never overwritten - choose another name")
    report = None
    text, sha = (None, None) if is_dir else read_text(src)
    if text is not None and _gate_needed(src_rel, dst_rel, text):
        await ulog(user_id, f"rename {src_rel} -> {dst_rel}: new name is a dependency manifest/code file - running the gate", src="coding")
        report = await _gate_or_raise(db, user_id, dst_rel, text, {"rename_from": src_rel})
    # snapshots first (undo via history / renaming back)
    if is_dir:
        db.add(FileVersion(user_id=user_id, path=src_rel, content=None, existed=False, reason="rename_dir"))
    else:
        db.add(FileVersion(user_id=user_id, path=src_rel, content=text, existed=text is not None, sha256=sha or "",
                           reason="rename_from"))
        dst_text, dst_sha = read_text(dst) if dst.exists() and not case_only else (None, None)
        db.add(FileVersion(user_id=user_id, path=dst_rel, content=dst_text, existed=dst_text is not None,
                           sha256=dst_sha or "", reason="overwritten" if dst_text is not None else "rename_to"))
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except PermissionError:
        if dst.exists() and dst.is_file():
            os.chmod(dst, stat.S_IWRITE | stat.S_IREAD)
        os.replace(src, dst)
    await db.commit()
    await ulog(user_id, f"renamed {'folder' if is_dir else 'file'} {src_rel} -> {dst_rel} (snapshot kept for undo)", src="coding")
    return {"ok": True, "from": src_rel, "path": dst_rel, "type": "dir" if is_dir else "file", "gate": report}


def _walk_files(base: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d.lower() != ".git" and not os.path.islink(os.path.join(dirpath, d))]
        for fn in filenames:
            p = Path(dirpath) / fn
            if not p.is_symlink():
                out.append(p)
    return out


async def copy_path(db: AsyncSession, user_id: int, src_rel: str, dst_rel: str, *, overwrite: bool = False) -> dict:
    src_rel, src = safe_path(user_id, src_rel)
    dst_rel = clean_target(dst_rel)
    dst = resolve(user_id, dst_rel)
    await ulog(user_id, f"copy requested: {src_rel} -> {dst_rel}", src="coding")
    if not src.exists() or src.is_symlink():
        raise FileNotFoundError(src_rel)
    if is_container(dst_rel):
        raise Protected("the copy must go inside scratch/, a cloned repo or a chat project")
    if dst_rel == src_rel or dst_rel.startswith(src_rel + "/"):
        raise PathError("cannot copy something into itself")
    if dst.exists():
        if not overwrite:
            raise TargetExists(dst_rel, dst.is_dir())
        if dst.is_dir() or src.is_dir():
            raise Protected("folders are never overwritten - choose another name")
    if src.is_file():
        data = src.read_bytes()
        if len(data) > MAX_COPY_BYTES:
            raise ValueError("file too large to copy")
        text = data.decode("utf-8") if kind_of(src, data) == "text" and len(data) <= MAX_TEXT else None
        report = None
        if text is not None and osv.deps_from_text(text, dst_rel):
            report = await _gate_or_raise(db, user_id, dst_rel, text, {"copy_from": src_rel})
        dst_text, dst_sha = read_text(dst) if dst.exists() else (None, None)
        db.add(FileVersion(user_id=user_id, path=dst_rel, content=dst_text, existed=dst_text is not None,
                           sha256=dst_sha or "", reason="copy"))
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        await db.commit()
        await ulog(user_id, f"copied {src_rel} -> {dst_rel} ({len(data)} bytes)", src="coding")
        return {"ok": True, "from": src_rel, "path": dst_rel, "type": "file", "gate": report}
    files = await asyncio.to_thread(_walk_files, src)
    total = sum(f.stat().st_size for f in files)
    if len(files) > MAX_COPY_FILES or total > MAX_COPY_BYTES:
        raise ValueError(f"folder too large to copy ({len(files)} files, {total // 1024 // 1024} MB)")
    gated = 0
    for f in files:
        if osv.is_manifest(f.name) and f.stat().st_size <= MAX_TEXT and gated < 60:
            gated += 1
            inner = f.relative_to(src).as_posix()
            await _gate_or_raise(db, user_id, f"{dst_rel}/{inner}", f.read_text(encoding="utf-8", errors="replace"),
                                 {"copy_from": f"{src_rel}/{inner}"})

    def _copy() -> None:
        dst.mkdir(parents=True, exist_ok=True)
        for f in files:
            target = dst / f.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)

    await asyncio.to_thread(_copy)
    db.add(FileVersion(user_id=user_id, path=dst_rel, content=None, existed=False, reason="copy_dir"))
    await db.commit()
    await ulog(user_id, f"copied folder {src_rel} -> {dst_rel} ({len(files)} files, {gated} manifest(s) gated; "
                        ".git and symlinks skipped)", src="coding")
    return {"ok": True, "from": src_rel, "path": dst_rel, "type": "dir", "files": len(files), "manifests_gated": gated}


def build_zip(user_id: int, rel: str):
    """Zip a workspace folder into a spooled temp file (.git and symlinks excluded)."""
    import tempfile
    import zipfile

    rel, base = safe_path(user_id, rel)
    if not base.is_dir():
        raise PathError("not a folder")
    spool = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024)
    count = total = 0
    try:
        with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for p in _walk_files(base):
                st = p.stat()
                count += 1
                total += st.st_size
                if count > MAX_ZIP_FILES or total > MAX_ZIP_BYTES:
                    raise ValueError("folder too large to download as .zip")
                zf.write(p, f"{base.name}/{p.relative_to(base).as_posix()}")
    except Exception:
        spool.close()
        raise
    size = spool.tell()
    spool.seek(0)
    return spool, size, base.name, count


def delete_path(path: Path) -> None:
    if path.is_dir():
        force_rmtree(path)
    elif path.exists():
        try:
            path.unlink()
        except PermissionError:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            path.unlink()
