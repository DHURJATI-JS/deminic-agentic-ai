"""Coding section API: workspace editor (+ the security gate), multi-file AI
apply (restore-point protected), vulnerability scanner, Locust stress tests
and GitHub (read + new branches only)."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.jobs import DuplicateJob, JobContext, jobs
from app.core.logs import ulog
from app.core.sockets import emit_user
from app.db import SessionLocal, get_db
from app.models import FileVersion, LocustRun, Repo, User, VulnScan
from app.security import clamav
from app.security.sessions import current_user
from app.services import editor, github, locust_runner, osv
from app.services.editor import GateBlocked
from app.services.workspace import PathError, user_root

router = APIRouter(prefix="/api/coding", tags=["coding"])


def _path_error(e: Exception) -> HTTPException:
    return HTTPException(400, str(e))


def _gate_http(e: GateBlocked) -> HTTPException:
    return HTTPException(422, {"message": e.report.get("message", "Blocked by the security gate"),
                               "verdict": e.report.get("verdict", "block"), "report": e.report, "path": e.path})


# =========================================================================== files
@router.get("/files/list")
async def list_files(path: str = "", user: User = Depends(current_user)):
    try:
        return {"path": path.strip("/"), "entries": editor.list_dir(user.id, path)}
    except PathError as e:
        raise _path_error(e) from e


@router.get("/files/read")
async def read_file(path: str, user: User = Depends(current_user)):
    try:
        rel, p = editor.safe_path(user.id, path)
    except PathError as e:
        raise _path_error(e) from e
    if not p.exists():
        raise HTTPException(404, "File not found")
    if p.is_dir():
        raise HTTPException(400, "That is a folder")
    size = p.stat().st_size
    kind = editor.kind_of(p)
    out = {"path": rel, "name": p.name, "size": size, "kind": kind, "modified": int(p.stat().st_mtime)}
    if kind == "binary" or size > editor.MAX_TEXT:
        out["kind"] = "binary" if kind == "binary" else "large"
        return out
    data = p.read_bytes()
    if kind == "image" and p.suffix.lower() != ".svg":
        out["sha"] = editor.sha_of(data)
        return out
    if editor.kind_of(p, data) == "binary":
        out["kind"] = "binary"
        return out
    out["content"] = data.decode("utf-8", errors="replace")
    out["sha"] = editor.sha_of(data)
    if kind != "image":
        out["kind"] = "text"
    return out


@router.get("/files/raw")
async def raw_file(path: str, user: User = Depends(current_user)):
    try:
        rel, p = editor.safe_path(user.id, path)
    except PathError as e:
        raise _path_error(e) from e
    if not p.is_file():
        raise HTTPException(404, "File not found")
    if p.stat().st_size > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, "File too large to preview")
    ext = p.suffix.lower()
    mime = editor.IMAGE_MIME.get(ext, "application/octet-stream")
    headers = {
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "inline" if ext in editor.IMAGE_MIME else f'attachment; filename="{p.name}"',
        "Cache-Control": "no-store",
    }
    return Response(content=p.read_bytes(), media_type=mime, headers=headers)


class SaveIn(BaseModel):
    path: str = Field(min_length=1, max_length=600)
    content: str = Field(max_length=2_100_000)
    base_sha: str | None = Field(default=None, max_length=64)
    force: bool = False
    source: str | None = Field(default=None, max_length=40)


@router.post("/files/save")
async def save_file(body: SaveIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    editor.ensure_layout(user.id)
    try:
        return await editor.write_with_gate(db, user.id, body.path, body.content, base_sha=body.base_sha, force=body.force,
                                            reason=body.source or "save")
    except PathError as e:
        raise _path_error(e) from e
    except GateBlocked as e:
        raise _gate_http(e) from e
    except FileExistsError as e:
        raise HTTPException(409, {"message": "The file changed on disk since you opened it (or already exists).",
                                  "code": "conflict", "current_sha": str(e)}) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


class NewIn(BaseModel):
    path: str = Field(min_length=1, max_length=600)
    kind: str = Field(default="file", pattern="^(file|folder)$")
    content: str = Field(default="", max_length=2_100_000)


@router.post("/files/new")
async def new_entry(body: NewIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    editor.ensure_layout(user.id)
    try:
        rel, p = editor.safe_path(user.id, body.path)
    except PathError as e:
        raise _path_error(e) from e
    if p.exists():
        raise HTTPException(409, "Something with that name already exists")
    if body.kind == "folder":
        p.mkdir(parents=True)
        await ulog(user.id, f"folder created: {rel}", src="coding")
        return {"ok": True, "path": rel, "type": "dir"}
    try:
        return await editor.write_with_gate(db, user.id, rel, body.content, reason="create")
    except GateBlocked as e:
        raise _gate_http(e) from e
    except (FileExistsError, ValueError) as e:
        raise HTTPException(409, str(e) or "conflict") from e


class PathIn(BaseModel):
    path: str = Field(min_length=1, max_length=600)


@router.post("/files/delete")
async def delete_entry(body: PathIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    try:
        rel, p = editor.safe_path(user.id, body.path)
    except PathError as e:
        raise _path_error(e) from e
    if rel in editor.ROOTS:
        raise HTTPException(400, "The workspace root folders cannot be deleted")
    if not p.exists():
        raise HTTPException(404, "Not found")
    if p.is_symlink():
        raise HTTPException(400, "Symbolic links are not managed by the editor")
    if p.is_file():
        await editor.snapshot_before_delete(db, user.id, rel, p)
    parts = rel.split("/")
    if p.is_dir() and parts[0] == "repos" and len(parts) == 2:
        repo = (await db.execute(select(Repo).where(Repo.user_id == user.id, Repo.cloned_path == rel))).scalar_one_or_none()
        if repo is not None:
            repo.cloned_path, repo.clone_status, repo.scan_report = "", "none", {}
    await asyncio.to_thread(editor.delete_path, p)
    await db.commit()
    await ulog(user.id, f"deleted {rel}", src="coding")
    await emit_user(user.id, "repo:changed", {"action": "workspace"})
    return {"ok": True}


# =========================================================================== per-chat projects
async def coding_chat_no(db: AsyncSession, user: User, ref: str | int | None) -> int:
    """'chat#12', 'chat#12.3' or 12 -> 12, only if it is THIS user's coding chat."""
    import re

    from app.models import Chat

    m = re.search(r"(\d+)", str(ref or ""))
    if not m:
        raise HTTPException(400, "Invalid chat reference")
    no = int(m.group(1))
    chat = (await db.execute(select(Chat.id, Chat.section).where(Chat.user_id == user.id, Chat.chat_no == no))).first()
    if chat is None:
        raise HTTPException(404, "Chat not found")
    if chat.section != "coding":
        raise HTTPException(400, "Only coding chats have a project workspace")
    return no


class ProjectIn(BaseModel):
    chat_no: int = Field(ge=1)


@router.post("/projects/ensure")
async def ensure_project(body: ProjectIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    no = await coding_chat_no(db, user, body.chat_no)
    root = user_root(user.id)
    existed = (root / editor.project_rel(no)).exists()
    rel = editor.ensure_project(user.id, no)
    if not existed:
        await ulog(user.id, f"created project workspace {rel} for coding chat#{no}", src="coding")
    return {"path": rel, "chat_no": no, "label": f"chat#{no} project"}


@router.get("/projects")
async def list_projects(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    from app.models import Chat

    editor.ensure_layout(user.id)
    base = user_root(user.id) / "projects"
    nos = sorted(int(d.name.split("-", 1)[1]) for d in base.iterdir()
                 if d.is_dir() and d.name.startswith("chat-") and d.name.split("-", 1)[1].isdigit())
    titles = {}
    if nos:  # one query for all titles (no N+1)
        rows = (await db.execute(select(Chat.chat_no, Chat.title).where(Chat.user_id == user.id, Chat.chat_no.in_(nos)))).all()
        titles = {r.chat_no: r.title for r in rows}
    return {"projects": [{"chat_no": n, "path": editor.project_rel(n), "title": titles.get(n, "(chat deleted)")} for n in nos]}


# =========================================================================== rename / copy / download
class MoveIn(BaseModel):
    path: str = Field(min_length=1, max_length=600)
    new_path: str = Field(min_length=1, max_length=600)
    overwrite: bool = False


def _move_errors(e: Exception) -> HTTPException:
    if isinstance(e, GateBlocked):
        return _gate_http(e)
    if isinstance(e, editor.TargetExists):
        return HTTPException(409, {"message": f"{e.rel} already exists - confirm to overwrite it", "code": "exists",
                                   "target": e.rel, "is_dir": e.is_dir})
    if isinstance(e, editor.Protected):
        return HTTPException(403, str(e))
    if isinstance(e, FileNotFoundError):
        return HTTPException(404, "Not found")
    if isinstance(e, (PathError, ValueError)):
        return HTTPException(400, str(e))
    if isinstance(e, OSError):
        return HTTPException(409, f"The file system refused the operation ({type(e).__name__}) - is the file open elsewhere?")
    raise e


@router.post("/files/rename")
async def rename_entry(body: MoveIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    try:
        res = await editor.rename_path(db, user.id, body.path, body.new_path, overwrite=body.overwrite)
    except (GateBlocked, editor.TargetExists, PathError, ValueError, OSError) as e:
        raise _move_errors(e) from e
    await emit_user(user.id, "repo:changed", {"action": "workspace", "paths": [res["from"], res["path"]]})
    return res


@router.post("/files/copy")
async def copy_entry(body: MoveIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    try:
        res = await editor.copy_path(db, user.id, body.path, body.new_path, overwrite=body.overwrite)
    except (GateBlocked, editor.TargetExists, PathError, ValueError, OSError) as e:
        raise _move_errors(e) from e
    await emit_user(user.id, "repo:changed", {"action": "workspace", "paths": [res["path"]]})
    return res


def _attachment(name: str) -> str:
    from urllib.parse import quote

    ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\;' else "_" for c in name) or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


DOWNLOAD_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
                    "Content-Security-Policy": "default-src 'none'; sandbox"}


@router.get("/files/download")
async def download(path: str, user: User = Depends(current_user)):
    from fastapi.responses import FileResponse, StreamingResponse

    try:
        rel, p = editor.safe_path(user.id, path)
    except PathError as e:
        raise _path_error(e) from e
    if not p.exists() or p.is_symlink():
        raise HTTPException(404, "Not found")
    if p.is_file():
        await ulog(user.id, f"download {rel} ({p.stat().st_size} bytes)", src="coding")
        return FileResponse(p, media_type="application/octet-stream",
                            headers={**DOWNLOAD_HEADERS, "Content-Disposition": _attachment(p.name)})
    try:
        spool, size, name, count = await asyncio.to_thread(editor.build_zip, user.id, rel)
    except ValueError as e:
        raise HTTPException(413, str(e)) from e
    await ulog(user.id, f"download {rel} as .zip ({count} files, {size} bytes; .git excluded)", src="coding")

    def _chunks():
        try:
            while True:
                chunk = spool.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            spool.close()

    return StreamingResponse(_chunks(), media_type="application/zip",
                             headers={**DOWNLOAD_HEADERS, "Content-Disposition": _attachment(f"{name}.zip"),
                                      "Content-Length": str(size)})


@router.post("/files/upload")
async def upload(dir: str = Form("scratch"), files: list[UploadFile] = File(...), db: AsyncSession = Depends(get_db),
                 user: User = Depends(current_user)):
    editor.ensure_layout(user.id)
    try:
        drel, dpath = editor.safe_path(user.id, dir)
    except PathError as e:
        raise _path_error(e) from e
    if not dpath.is_dir():
        raise HTTPException(400, "Upload target must be a folder")
    results = []
    limit = settings.max_upload_mb * 1024 * 1024
    for f in files[:20]:
        raw_name = os.path.basename((f.filename or "").replace("\\", "/")).strip()
        try:
            name = editor.clean_segment(raw_name)
        except PathError as e:
            results.append({"name": raw_name or "?", "ok": False, "error": f"invalid file name: {e}"})
            continue
        data = await f.read(limit + 1)
        if len(data) > limit:
            results.append({"name": name, "ok": False, "error": f"larger than {settings.max_upload_mb} MB"})
            continue
        scan = await clamav.scan_bytes(data)
        if scan.error:
            results.append({"name": name, "ok": False, "error": f"virus scanner unavailable ({scan.error}) - not saved"})
            continue
        if not scan.clean:
            results.append({"name": name, "ok": False, "error": f"ClamAV: {scan.signature}"})
            await ulog(user.id, f"upload {name} rejected by ClamAV ({scan.signature})", level="warn", src="coding")
            continue
        rel = f"{drel}/{name}"
        target = dpath / name
        if editor.kind_of(target, data) == "text" and len(data) <= editor.MAX_TEXT and target.suffix.lower() not in editor.IMAGE_EXT:
            try:
                res = await editor.write_with_gate(db, user.id, rel, data.decode("utf-8"), force=True, reason="upload")
                results.append({"name": name, "ok": True, "path": rel, "verdict": res["verdict"]})
            except GateBlocked as e:
                results.append({"name": name, "ok": False, "error": e.report.get("message"), "report": e.report})
            continue
        if target.exists() and target.is_file():
            await editor.snapshot_before_delete(db, user.id, rel, target)
        target.write_bytes(data)
        db.add(FileVersion(user_id=user.id, path=rel, content=None, existed=False, sha256=editor.sha_of(data), reason="upload"))
        await db.commit()
        results.append({"name": name, "ok": True, "path": rel, "verdict": "clean"})
        await ulog(user.id, f"uploaded {rel} ({len(data)} bytes, ClamAV clean)", src="coding")
    return {"results": results}


@router.get("/files/history")
async def history(path: str, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    try:
        rel = editor.norm_rel(path)
    except PathError as e:
        raise _path_error(e) from e
    rows = (await db.execute(
        select(FileVersion.id, FileVersion.reason, FileVersion.existed, FileVersion.created_at, FileVersion.sha256,
               func.length(FileVersion.content).label("size"))
        .where(FileVersion.user_id == user.id, FileVersion.path == rel)
        .order_by(FileVersion.created_at.desc()).limit(60))).all()
    return {"path": rel, "versions": [{"id": r.id, "reason": r.reason, "existed": r.existed, "size": r.size or 0,
                                       "sha": r.sha256, "created_at": r.created_at.isoformat()} for r in rows]}


@router.get("/files/version/{version_id}")
async def version(version_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    v = (await db.execute(select(FileVersion).where(FileVersion.id == version_id, FileVersion.user_id == user.id))).scalar_one_or_none()
    if v is None:
        raise HTTPException(404, "Version not found")
    return {"id": v.id, "path": v.path, "existed": v.existed, "content": v.content, "reason": v.reason,
            "created_at": v.created_at.isoformat()}


class RestoreIn(BaseModel):
    version_id: int


@router.post("/files/restore")
async def restore_version(body: RestoreIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    v = (await db.execute(select(FileVersion).where(FileVersion.id == body.version_id, FileVersion.user_id == user.id))).scalar_one_or_none()
    if v is None:
        raise HTTPException(404, "Version not found")
    try:
        rel, p = editor.safe_path(user.id, v.path)
    except PathError as e:
        raise _path_error(e) from e
    if not v.existed or v.content is None:
        if p.is_file():
            await editor.snapshot_before_delete(db, user.id, rel, p)
            p.unlink()
            await db.commit()
        await ulog(user.id, f"restored {rel} to the state before it existed (file removed, snapshot kept)", src="coding")
        return {"ok": True, "path": rel, "deleted": True}
    try:
        return await editor.write_with_gate(db, user.id, rel, v.content, force=True, reason="restore")
    except GateBlocked as e:
        raise _gate_http(e) from e


# =========================================================================== multi-file apply (protected)
class ApplyFile(BaseModel):
    path: str = Field(min_length=1, max_length=600)
    content: str = Field(max_length=2_100_000)


class ApplyIn(BaseModel):
    files: list[ApplyFile] = Field(min_length=1, max_length=20)
    chat_ref: str | None = Field(default=None, max_length=40)


async def apply_job(ctx: JobContext, files: list[dict], chat_ref: str | None) -> dict:
    uid = ctx.user_id
    editor.ensure_layout(uid)
    planned = []
    async with SessionLocal() as db:
        # 1) gate EVERY file first - nothing is written unless all pass
        for i, f in enumerate(files):
            rel, p = editor.safe_path(uid, f["path"])
            old_text, _ = editor.read_text(p)
            if p.exists() and old_text is None:
                raise ValueError(f"{rel} is binary / too large - not overwritten")
            report = await editor.gate(uid, rel, f["content"], old_text)
            row = await editor.record_decision(db, uid, rel, report, job_id=ctx.id, source={"chat_ref": chat_ref} if chat_ref else None)
            report["scan_id"] = row.id
            await db.commit()
            await ctx.progress(0.1 + 0.5 * (i + 1) / len(files), f"gate {rel}: {report['verdict']}")
            if report["verdict"] != "go":
                ctx.handle.meta["gate"] = {"path": rel, "message": report["message"], "scan_id": row.id,
                                           "findings": report.get("findings", [])[:5], "warnings": report.get("warnings", [])}
                raise GateBlocked(rel, report)
            planned.append((rel, p, f["content"], old_text, p.exists()))
        # 2) write them, recording a restore op BEFORE each write
        for j, (rel, p, content, old_text, existed) in enumerate(planned):
            await ctx.restore.record({"type": "restore_file", "path": rel, "existed": existed, "content": old_text or ""})
            db.add(FileVersion(user_id=uid, path=rel, content=old_text if existed else None, existed=existed,
                               sha256=editor.sha_of(old_text.encode()) if old_text is not None else "", reason="ai_apply",
                               job_id=ctx.id))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content.encode("utf-8"))
            await ctx.progress(0.6 + 0.4 * (j + 1) / len(planned), f"wrote {rel}")
        await db.commit()
    await emit_user(uid, "repo:changed", {"action": "workspace", "paths": [x[0] for x in planned]})
    return {"applied": [x[0] for x in planned]}


@router.post("/apply")
async def apply(body: ApplyIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    files = [f.model_dump() for f in body.files]
    chat_no = await coding_chat_no(db, user, body.chat_ref) if body.chat_ref else None
    try:
        for f in files:
            # relative paths from a coding chat land in that chat's project folder
            f["path"] = editor.clean_target(editor.in_chat_project(f["path"], chat_no))
    except PathError as e:
        raise _path_error(e) from e
    if chat_no:
        editor.ensure_project(user.id, chat_no)

    async def _job(ctx: JobContext):
        return await apply_job(ctx, files, body.chat_ref)

    try:
        h = await jobs.spawn(user.id, "code_apply", f"apply {len(files)} AI file change(s) (gated)", _job, cancellable=False,
                             dedupe_key=f"apply:{user.id}", retries=0, meta={"paths": [f["path"] for f in files]},
                             restore_label=f"before applying {len(files)} file(s)")
    except DuplicateJob as e:
        raise HTTPException(409, "Another apply is still running") from e
    return {"request_id": h.id, "paths": [f["path"] for f in files]}


# =========================================================================== vulnerability scanner
class ScanIn(BaseModel):
    mode: str = Field(pattern="^(text|file|repo)$")
    text: str | None = Field(default=None, max_length=1_000_000)
    filename: str | None = Field(default="auto", max_length=80)
    path: str | None = Field(default=None, max_length=600)
    repo_id: int | None = None


def _guess_name(text: str, filename: str | None) -> str:
    if filename and filename != "auto":
        return filename
    t = (text or "").lstrip()
    if t.startswith("{"):
        return "package-lock.json" if '"lockfileVersion"' in t[:4000] else "package.json"
    if "[project]" in t or "[tool.poetry" in t:
        return "pyproject.toml"
    if "[packages]" in t:
        return "Pipfile"
    return "requirements.txt"


def _walk_manifests(root: Path) -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build")]
        for fn in filenames:
            if osv.is_manifest(fn):
                found.append(Path(dirpath) / fn)
        if len(found) > 60:
            break
    return found


async def scan_job(ctx: JobContext, body: dict) -> dict:
    uid = ctx.user_id
    deps: list[osv.Dep] = []
    target = ""
    kind = "manual"
    await ctx.progress(0.05, "collecting dependencies")
    if body["mode"] == "text":
        name = _guess_name(body.get("text") or "", body.get("filename"))
        deps = osv.deps_from_text(body.get("text") or "", name)
        if not deps:
            deps = osv.parse_package_list(body.get("text") or "")
        target = f"pasted {name}"
    elif body["mode"] == "file":
        rel, p = editor.safe_path(uid, body.get("path") or "")
        text, _ = editor.read_text(p)
        if text is None:
            raise ValueError("file not found or not a text file")
        project = editor._project_root(uid, rel)  # noqa: SLF001
        pins = editor._manifest_pins(project, p.parent) if project.exists() else {}  # noqa: SLF001
        deps = osv.deps_from_text(text, rel, pins=pins, local_names=editor._local_names(project) if rel.endswith(".py") else None)  # noqa: SLF001
        target = rel
    else:
        kind = "repo"
        async with SessionLocal() as db:
            repo = (await db.execute(select(Repo).where(Repo.id == body.get("repo_id"), Repo.user_id == uid))).scalar_one_or_none()
        if repo is None or not repo.cloned_path:
            raise ValueError("repository is not cloned")
        root = user_root(uid) / repo.cloned_path
        manifests = _walk_manifests(root)
        await ctx.log(f"scanning {len(manifests)} manifest(s) in {repo.cloned_path}")
        for m in manifests:
            rel_m = str(m.relative_to(root)).replace("\\", "/")
            try:
                deps += [osv.Dep(d.ecosystem, d.name, d.version, d.spec, rel_m)
                         for d in osv.deps_from_text(m.read_text(encoding="utf-8", errors="replace"), m.name)]
            except OSError:
                continue
        target = repo.cloned_path
    uniq = {}
    for d in deps:
        uniq.setdefault((d.ecosystem, d.name, d.version, d.spec), d)
    deps = list(uniq.values())
    await ctx.progress(0.25, f"{len(deps)} dependencies - querying OSV.dev")
    report = await osv.scan_deps(deps)
    report["target"] = target
    report["message"] = (f"{report['vulnerable']} vulnerable package(s), {report['vuln_count']} advisories"
                         if report["findings"] else f"No known vulnerabilities in {len(report['checked'])} package(s)")
    async with SessionLocal() as db:
        row = VulnScan(user_id=uid, target=target[:500], kind=kind, verdict=report["verdict"], result=report, job_id=ctx.id)
        db.add(row)
        await db.commit()
        scan_id = row.id
    await emit_user(uid, "scan:result", {"scan_id": scan_id, "request_id": ctx.id, "verdict": report["verdict"],
                                         "vulnerable": report["vulnerable"], "target": target})
    await ctx.progress(1.0, report["message"])
    return {"scan_id": scan_id, "vulnerable": report["vulnerable"]}


@router.post("/scan")
async def start_scan(body: ScanIn, user: User = Depends(current_user)):
    data = body.model_dump()
    if body.mode == "text" and not (body.text or "").strip():
        raise HTTPException(400, "Paste a requirements.txt / package.json / package list first")
    if body.mode == "file":
        try:
            editor.safe_path(user.id, body.path or "")
        except PathError as e:
            raise _path_error(e) from e

    async def _job(ctx: JobContext):
        return await scan_job(ctx, data)

    h = await jobs.spawn(user.id, "vuln_scan", f"vulnerability scan ({body.mode})", _job, retries=1, timeout_s=600,
                         meta={"mode": body.mode})
    return {"request_id": h.id}


def scan_summary(r: VulnScan) -> dict:
    res = r.result or {}
    return {"id": r.id, "target": r.target, "kind": r.kind, "verdict": r.verdict, "created_at": r.created_at.isoformat(),
            "vulnerable": len(res.get("findings") or []), "checked": len(res.get("checked") or []),
            "message": res.get("message", "")}


@router.get("/scans")
async def list_scans(limit: int = 40, kind: str | None = None, db: AsyncSession = Depends(get_db),
                     user: User = Depends(current_user)):
    q = select(VulnScan).where(VulnScan.user_id == user.id)
    if kind:
        q = q.where(VulnScan.kind == kind)
    rows = (await db.execute(q.order_by(VulnScan.created_at.desc()).limit(min(max(limit, 1), 100)))).scalars().all()
    return {"scans": [scan_summary(r) for r in rows]}


@router.get("/scans/{scan_id}")
async def get_scan(scan_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    r = (await db.execute(select(VulnScan).where(VulnScan.id == scan_id, VulnScan.user_id == user.id))).scalar_one_or_none()
    if r is None:
        raise HTTPException(404, "Scan not found")
    return {**scan_summary(r), "result": r.result}


# =========================================================================== Locust
class LocustIn(BaseModel):
    target_url: str = Field(min_length=8, max_length=500)
    users: int = Field(default=20, ge=1, le=locust_runner.MAX_USERS)
    spawn_rate: float = Field(default=5.0, ge=0.1, le=locust_runner.MAX_RATE)
    duration: int = Field(default=60, ge=5, le=locust_runner.MAX_DURATION)
    paths: list[str] = Field(default_factory=lambda: ["/"], max_length=25)
    method: str = Field(default="GET", pattern="^(GET|HEAD)$")
    authorized: bool = False


def run_view(r: LocustRun) -> dict:
    stats = r.stats or {}
    return {"id": r.id, "request_id": r.job_id, "target_url": r.target_url, "users": r.users, "spawn_rate": r.spawn_rate,
            "duration": r.duration_s, "paths": r.paths, "status": r.status, "totals": stats.get("totals") or {},
            "created_at": r.created_at.isoformat(), "finished_at": r.finished_at.isoformat() if r.finished_at else None}


@router.post("/locust")
async def start_locust(body: LocustIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    if not body.authorized:
        raise HTTPException(400, "Confirm that you own or are authorised to stress-test this target")
    try:
        target, paths = locust_runner.validate(body.target_url, body.users, body.spawn_rate, body.duration, body.paths)
    except locust_runner.StressError as e:
        raise HTTPException(400, str(e)) from e
    if len(jobs.running_for(user.id, "locust")) >= 3:
        raise HTTPException(429, "At most 3 stress tests can run at the same time")
    row = LocustRun(user_id=user.id, target_url=target, users=body.users, spawn_rate=body.spawn_rate,
                    duration_s=body.duration, paths=paths, status="running", stats={})
    db.add(row)
    await db.commit()
    run_id = row.id

    async def _job(ctx: JobContext):
        return await locust_runner.run_job(ctx, run_id, target, body.users, body.spawn_rate, body.duration, paths, body.method)

    h = await jobs.spawn(user.id, "locust", f"stress test #{run_id} {target} ({body.users} users, {body.duration}s)", _job,
                         retries=0, timeout_s=body.duration + 180, meta={"run_id": run_id, "target": target})
    row.job_id = h.id
    await db.commit()
    return {"request_id": h.id, "run_id": run_id, "run": run_view(row)}


@router.get("/locust")
async def list_runs(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    rows = (await db.execute(select(LocustRun).where(LocustRun.user_id == user.id)
                             .order_by(LocustRun.created_at.desc()).limit(40))).scalars().all()
    return {"runs": [run_view(r) for r in rows]}


@router.get("/locust/{run_id}")
async def get_run(run_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    r = (await db.execute(select(LocustRun).where(LocustRun.id == run_id, LocustRun.user_id == user.id))).scalar_one_or_none()
    if r is None:
        raise HTTPException(404, "Run not found")
    return {**run_view(r), "stats": r.stats or {}}


# =========================================================================== GitHub
class LinkIn(BaseModel):
    token: str = Field(min_length=10, max_length=255)


@router.post("/github/link")
async def gh_link(body: LinkIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    try:
        login = await github.link(db, user, body.token)
    except github.GitHubError as e:
        raise HTTPException(e.status if e.status < 500 else 502, str(e)) from e
    try:
        await jobs.spawn(user.id, "github_sync", "sync GitHub repositories", github.sync_job,
                         dedupe_key=f"ghsync:{user.id}", retries=1, timeout_s=900)
    except DuplicateJob:
        pass
    return {"ok": True, "login": login}


@router.delete("/github/link")
async def gh_unlink(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    await github.unlink(db, user)
    await emit_user(user.id, "repo:changed", {"action": "unlinked"})
    return {"ok": True}


@router.get("/github/repos")
async def gh_repos(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    rows = (await db.execute(select(Repo).where(Repo.user_id == user.id).order_by(Repo.pushed_at.desc().nulls_last()))).scalars().all()
    running = {j["meta"].get("repo_id"): j["request_id"] for j in jobs.list_for(user.id)
               if j["kind"] in ("github_clone", "github_scan") and j["status"] == "running"}
    return {"linked": bool(user.settings.github_token_enc), "login": user.settings.github_login,
            "git": await github.git_available(),
            "repos": [{**github.repo_view(r), "job": running.get(r.id)} for r in rows]}


@router.post("/github/sync")
async def gh_sync(user: User = Depends(current_user)):
    if not user.settings.github_token_enc:
        raise HTTPException(400, "Link GitHub first")
    try:
        h = await jobs.spawn(user.id, "github_sync", "sync GitHub repositories", github.sync_job,
                             dedupe_key=f"ghsync:{user.id}", retries=1, timeout_s=900)
    except DuplicateJob as e:
        raise HTTPException(409, "A sync is already running") from e
    return {"request_id": h.id}


async def _repo(db: AsyncSession, user: User, repo_id: int) -> Repo:
    r = (await db.execute(select(Repo).where(Repo.id == repo_id, Repo.user_id == user.id))).scalar_one_or_none()
    if r is None:
        raise HTTPException(404, "Repository not found")
    return r


async def spawn_clone(user_id: int, repo: Repo) -> str:
    async def _job(ctx: JobContext):
        return await github.clone_job(ctx, repo.id)

    h = await jobs.spawn(user_id, "github_clone", f"clone {repo.full_name} + ClamAV scan", _job,
                         dedupe_key=f"clone:{user_id}:{repo.id}", retries=0, timeout_s=1800, meta={"repo_id": repo.id})
    return h.id


@router.post("/github/repos/{repo_id}/clone")
async def gh_clone(repo_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    repo = await _repo(db, user, repo_id)
    if repo.cloned_path and (user_root(user.id) / repo.cloned_path).exists():
        raise HTTPException(409, f"Already cloned at {repo.cloned_path} - delete it in the editor to re-clone")
    try:
        rid = await spawn_clone(user.id, repo)
    except DuplicateJob as e:
        raise HTTPException(409, "This repository is already being cloned") from e
    return {"request_id": rid}


@router.post("/github/repos/{repo_id}/scan")
async def gh_rescan(repo_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    repo = await _repo(db, user, repo_id)
    if not repo.cloned_path:
        raise HTTPException(400, "Clone it first")

    async def _job(ctx: JobContext):
        return await github.rescan_job(ctx, repo_id)

    try:
        h = await jobs.spawn(user.id, "github_scan", f"ClamAV rescan {repo.full_name}", _job,
                             dedupe_key=f"rescan:{user.id}:{repo_id}", retries=0, meta={"repo_id": repo_id})
    except DuplicateJob as e:
        raise HTTPException(409, "A scan is already running") from e
    return {"request_id": h.id}


@router.get("/github/repos/{repo_id}/changes")
async def gh_changes(repo_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    repo = await _repo(db, user, repo_id)
    return {"repo": repo.full_name, "path": repo.cloned_path, "files": await github.changed_files(user.id, repo)}


class PushIn(BaseModel):
    repo_id: int
    branch: str = Field(min_length=1, max_length=100)
    message: str = Field(default="", max_length=500)
    paths: list[str] = Field(min_length=1, max_length=200)


@router.post("/github/push")
async def gh_push(body: PushIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    repo = await _repo(db, user, body.repo_id)
    try:
        res = await github.push_new_branch(user.id, repo, body.branch, body.message, body.paths)
    except github.GitHubError as e:
        raise HTTPException(e.status if 400 <= e.status < 500 else 502, str(e)) from e
    repo.branches_pushed.append({"branch": res["branch"], "url": res["branch_url"], "compare": res["compare_url"],
                                 "files": len(res["files"]), "at": res.get("commit", "")[:12]})
    await db.commit()
    await emit_user(user.id, "repo:changed", {"action": "pushed", "id": repo.id, "branch": res["branch"]})
    return res
