"""GitHub integration with a deliberately restricted client.

SafeGitHub only ever issues:
  * GET   (anything read-only)
  * POST  /repos/{o}/{r}/git/blobs | git/trees | git/commits | git/refs
          - refs are create-only: the branch must not exist yet
It refuses PATCH / PUT / DELETE and every other POST, so the AI (or a bug)
can never delete or modify a repo, an existing branch or repo settings.

Clones use ``git clone --depth 1`` with the token injected through
GIT_CONFIG_* environment variables (never written to .git/config or a URL),
then every file is scanned by ClamAV; infected files are quarantined.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select

from app.config import settings
from app.core.jobs import JobContext
from app.core.logs import ulog
from app.core.sockets import emit_user
from app.core.state import http
from app.db import SessionLocal
from app.models import Repo, User, UserSettings
from app.security import clamav
from app.security.crypto import decrypt, encrypt
from app.services.editor import force_rmtree
from app.services.workspace import user_root

API = "https://api.github.com"
_ALLOWED_POST = re.compile(r"^/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/git/(blobs|trees|commits|refs)$")
_BRANCH_RE = re.compile(r"^(?!/)(?!.*//)(?!.*\.\.)(?!.*@\{)(?!.*\.lock$)(?!.*/$)(?!.*\.$)[A-Za-z0-9._/-]{1,100}$")


class GitHubError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class ForbiddenOperation(GitHubError):
    def __init__(self, what: str):
        super().__init__(f"blocked by SafeGitHub: {what} (only reads and new branches are allowed)", 403)


class SafeGitHub:
    def __init__(self, token: str | None):
        self.token = token or ""

    def _headers(self) -> dict:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
             "User-Agent": "DeminicAI/1.0"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None):
        method = method.upper()
        if not path.startswith("/") or "://" in path or ".." in path:
            raise ForbiddenOperation(f"invalid path {path[:60]}")
        if method == "POST":
            if not _ALLOWED_POST.match(path):
                raise ForbiddenOperation(f"POST {path}")
        elif method != "GET":
            raise ForbiddenOperation(f"{method} {path}")
        r = await http().request(method, API + path, headers=self._headers(), params=params, json=json, timeout=30,
                                 follow_redirects=method == "GET")
        return r

    async def get(self, path: str, params: dict | None = None):
        return await self.request("GET", path, params=params)

    async def get_json(self, path: str, params: dict | None = None):
        r = await self.get(path, params)
        if r.status_code == 401:
            raise GitHubError("GitHub rejected the token (401) - link GitHub again", 401)
        if r.status_code >= 400:
            raise GitHubError(f"GitHub {path}: HTTP {r.status_code} {r.text[:160]}", r.status_code)
        return r.json()

    async def paginate(self, path: str, params: dict | None = None, limit: int = 1000) -> list:
        out, page = [], 1
        while len(out) < limit:
            data = await self.get_json(path, {**(params or {}), "per_page": 100, "page": page})
            if not isinstance(data, list) or not data:
                break
            out += data
            if len(data) < 100:
                break
            page += 1
        return out[:limit]

    async def post_json(self, path: str, body: dict):
        r = await self.request("POST", path, json=body)
        if r.status_code >= 400:
            raise GitHubError(f"GitHub POST {path}: HTTP {r.status_code} {r.text[:200]}", r.status_code)
        return r.json()

    async def ref_exists(self, owner: str, repo: str, branch: str) -> bool:
        r = await self.get(f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
        if r.status_code == 200:
            return True
        if r.status_code == 404:
            return False
        raise GitHubError(f"could not check branch {branch}: HTTP {r.status_code}", r.status_code)

    async def create_branch_with_files(self, owner: str, repo: str, branch: str, message: str,
                                       files: list[tuple[str, bytes]], base: str | None = None) -> dict:
        if not _BRANCH_RE.match(branch) or branch.startswith("-"):
            raise GitHubError("invalid branch name")
        info = await self.get_json(f"/repos/{owner}/{repo}")
        base = base or info.get("default_branch") or "main"
        if await self.ref_exists(owner, repo, branch):
            raise GitHubError(f"branch '{branch}' already exists - existing branches are never modified; pick a new name", 409)
        ref = await self.get_json(f"/repos/{owner}/{repo}/git/ref/heads/{base}")
        base_sha = ref["object"]["sha"]
        commit = await self.get_json(f"/repos/{owner}/{repo}/git/commits/{base_sha}")
        tree_items = []
        for path, data in files:
            blob = await self.post_json(f"/repos/{owner}/{repo}/git/blobs",
                                        {"content": base64.b64encode(data).decode(), "encoding": "base64"})
            tree_items.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = await self.post_json(f"/repos/{owner}/{repo}/git/trees", {"base_tree": commit["tree"]["sha"], "tree": tree_items})
        new_commit = await self.post_json(f"/repos/{owner}/{repo}/git/commits",
                                          {"message": message, "tree": tree["sha"], "parents": [base_sha]})
        if await self.ref_exists(owner, repo, branch):  # re-check right before creating the ref
            raise GitHubError(f"branch '{branch}' appeared meanwhile - not touching it", 409)
        await self.post_json(f"/repos/{owner}/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": new_commit["sha"]})
        return {"branch": branch, "base": base, "commit": new_commit["sha"],
                "branch_url": f"https://github.com/{owner}/{repo}/tree/{branch}",
                "compare_url": f"https://github.com/{owner}/{repo}/compare/{base}...{branch}?expand=1"}


# --------------------------------------------------------------------------- account link
async def token_for(user_id: int) -> str:
    async with SessionLocal() as db:
        s = (await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))).scalar_one_or_none()
        return decrypt(s.github_token_enc) if s else ""


async def link(db, user: User, token: str) -> str:
    token = (token or "").strip()
    if not token or len(token) > 255 or any(c.isspace() for c in token):
        raise GitHubError("Paste a GitHub personal access token")
    gh = SafeGitHub(token)
    r = await gh.get("/user")
    if r.status_code != 200:
        raise GitHubError("GitHub rejected that token" if r.status_code in (401, 403) else f"GitHub HTTP {r.status_code}", 400)
    login = r.json().get("login", "")
    user.settings.github_token_enc = encrypt(token)
    user.settings.github_login = login[:100]
    await db.commit()
    await ulog(user.id, f"GitHub linked as {login} (token encrypted at rest)", src="github")
    return login


async def unlink(db, user: User) -> None:
    user.settings.github_token_enc = ""
    user.settings.github_login = ""
    await db.execute(delete(Repo).where(Repo.user_id == user.id, Repo.cloned_path == ""))
    await db.commit()
    await ulog(user.id, "GitHub unlinked - token deleted; cloned repos stay in the workspace", src="github")


def _dt(v: str | None):
    if not v:
        return None
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    return None


def repo_view(r: Repo) -> dict:
    return {
        "id": r.id, "full_name": r.full_name, "name": r.name, "owner": r.owner, "html_url": r.html_url,
        "description": r.description, "private": r.private, "fork": r.fork, "default_branch": r.default_branch,
        "language": r.language, "stars": r.stars, "forks": r.forks, "watchers": r.watchers, "open_issues": r.open_issues,
        "size_kb": r.size_kb, "clones": r.clones_count, "clones_uniques": r.clones_uniques, "views": r.views_count,
        "views_uniques": r.views_uniques, "pushed_at": r.pushed_at.isoformat() if r.pushed_at else None,
        "synced_at": r.synced_at.isoformat() if r.synced_at else None, "cloned_path": r.cloned_path,
        "clone_status": r.clone_status, "scan_report": dict(r.scan_report or {}), "branches_pushed": list(r.branches_pushed or []),
    }


# --------------------------------------------------------------------------- sync
async def sync_job(ctx: JobContext) -> dict:
    uid = ctx.user_id
    token = await token_for(uid)
    if not token:
        raise GitHubError("GitHub is not linked")
    gh = SafeGitHub(token)
    await ctx.progress(0.05, "listing repositories")
    items = await gh.paginate("/user/repos", {"affiliation": "owner,collaborator,organization_member", "sort": "updated"})
    await ctx.log(f"GitHub: {len(items)} repositories visible")
    sem = asyncio.Semaphore(6)
    traffic: dict[int, dict] = {}

    async def _traffic(item):
        if not (item.get("permissions") or {}).get("push"):
            return
        async with sem:
            out = {}
            for kind in ("clones", "views"):
                r = await gh.get(f"/repos/{item['full_name']}/traffic/{kind}")
                if r.status_code == 200:
                    j = r.json()
                    out[kind] = (j.get("count"), j.get("uniques"))
            traffic[item["id"]] = out

    done = 0
    for chunk_start in range(0, len(items), 12):
        chunk = items[chunk_start : chunk_start + 12]
        await asyncio.gather(*(_traffic(i) for i in chunk), return_exceptions=True)
        done += len(chunk)
        await ctx.progress(0.1 + 0.8 * done / max(len(items), 1), f"traffic stats {done}/{len(items)}")
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        existing = {r.gh_id: r for r in (await db.execute(select(Repo).where(Repo.user_id == uid))).scalars().all()}
        seen = set()
        for it in items:
            gid = int(it["id"])
            seen.add(gid)
            r = existing.get(gid)
            if r is None:
                r = Repo(user_id=uid, gh_id=gid, full_name=it["full_name"], name=it["name"], owner=it["owner"]["login"])
                db.add(r)
            r.full_name, r.name, r.owner = it["full_name"], it["name"], it["owner"]["login"]
            r.html_url = it.get("html_url") or ""
            r.description = (it.get("description") or "")[:2000]
            r.private, r.fork = bool(it.get("private")), bool(it.get("fork"))
            r.default_branch = it.get("default_branch") or "main"
            r.language = it.get("language") or ""
            r.stars, r.forks = int(it.get("stargazers_count") or 0), int(it.get("forks_count") or 0)
            r.watchers = int(it.get("subscribers_count") or it.get("watchers_count") or 0)
            r.open_issues, r.size_kb = int(it.get("open_issues_count") or 0), int(it.get("size") or 0)
            r.pushed_at = _dt(it.get("pushed_at"))
            t = traffic.get(gid) or {}
            if "clones" in t:
                r.clones_count, r.clones_uniques = t["clones"]
            if "views" in t:
                r.views_count, r.views_uniques = t["views"]
            r.synced_at = now
        for gid, r in existing.items():
            if gid not in seen and not r.cloned_path:
                await db.delete(r)
        await db.commit()
    await emit_user(uid, "repo:changed", {"action": "synced", "count": len(items)})
    await ctx.progress(1.0, f"{len(items)} repositories synced")
    return {"repos": len(items)}


# --------------------------------------------------------------------------- clone + scan
def clone_dest(user_id: int, repo: Repo, taken: set[str]) -> tuple[str, Path]:
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", repo.name)[:80] or f"repo{repo.id}"
    rel = f"repos/{safe}"
    if rel in taken:
        rel = f"repos/{re.sub(r'[^A-Za-z0-9._-]', '-', repo.owner)[:40]}__{safe}"
    return rel, user_root(user_id) / rel


def _git_env(token: str) -> dict:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "GCM_INTERACTIVE": "never", "GIT_CONFIG_NOSYSTEM": "1"}
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"})
    return env


async def _run_git(args: list[str], *, env: dict, cwd: str | None = None, ctx: JobContext | None = None,
                   timeout: float = 600) -> tuple[int, str]:
    hooks = settings.data_dir / "empty_git_hooks"  # no hooks can ever run from a clone
    hooks.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec("git", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={hooks}",
                                                "-c", "credential.helper=", "-c", "protocol.file.allow=never", *args,
                                                cwd=cwd, env=env,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out_lines: list[str] = []

    async def pump():
        assert proc.stdout is not None
        buf = b""
        while True:
            chunk = await proc.stdout.read(512)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for p in parts:
                line = p.decode("utf-8", "replace").rstrip()  # keep leading spaces (porcelain flags)
                if not line.strip():
                    continue
                out_lines.append(line)
                m = re.search(r"(Receiving objects|Resolving deltas):\s+(\d+)%", line)
                if m and ctx:
                    base = 0.05 if m.group(1).startswith("Receiving") else 0.4
                    await ctx.progress(base + 0.35 * int(m.group(2)) / 100, line[:120])

    try:
        await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout=timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5)
        raise
    return proc.returncode or 0, "\n".join(out_lines[-20:])


def _redact(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


async def scan_clone(ctx: JobContext, uid: int, dest: Path, rel: str) -> dict:
    if not await clamav.ping():
        for _ in range(5):
            await asyncio.sleep(3)
            if await clamav.ping():
                break
        else:
            return {"status": "error", "scanned": 0, "infected": [], "errors": [{"path": "*", "error": "ClamAV unavailable"}],
                    "note": "ClamAV unavailable - clone kept but NOT scanned; use Rescan"}

    async def prog(frac, detail):
        await ctx.progress(0.8 + 0.18 * frac, detail)

    res = await clamav.scan_tree(dest, prog)
    quarantined = []
    if res.infected:
        qroot = settings.data_dir / "quarantine" / f"u{uid}" / rel.split("/", 1)[-1]
        for item in res.infected:
            src = dest / item["path"]
            target = qroot / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                shutil.move(str(src), str(target))
                quarantined.append(item["path"])
        await ulog(uid, f"ClamAV: {len(res.infected)} infected file(s) quarantined from {rel}", level="warn", src="github")
    status = "infected" if res.infected else ("error" if res.errors and not res.scanned else "clean")
    return {"status": status, "scanned": res.scanned, "infected": res.infected, "quarantined": quarantined,
            "errors": res.errors[:20], "skipped": res.skipped}


async def clone_job(ctx: JobContext, repo_id: int) -> dict:
    uid = ctx.user_id
    async with SessionLocal() as db:
        repo = (await db.execute(select(Repo).where(Repo.id == repo_id, Repo.user_id == uid))).scalar_one_or_none()
        if repo is None:
            raise GitHubError("repository not found", 404)
        taken = {r for r in (await db.execute(select(Repo.cloned_path).where(Repo.user_id == uid, Repo.id != repo_id))).scalars().all() if r}
        rel, dest = clone_dest(uid, repo, taken)
        if dest.exists():
            raise GitHubError(f"{rel} already exists - delete it in the editor first to re-clone", 409)
        full_name = repo.full_name
        repo.clone_status = "cloning"
        await db.commit()
    await emit_user(uid, "repo:changed", {"action": "cloning", "id": repo_id})
    token = await token_for(uid)
    (user_root(uid) / "repos").mkdir(parents=True, exist_ok=True)
    status, report = "error", {}
    try:
        await ctx.log(f"cloning {full_name} into {rel} (depth 1, token via env header - never stored)")
        code, out = await _run_git(["clone", "--depth", "1", "--single-branch", "--no-tags", "--progress",
                                    f"https://github.com/{full_name}.git", str(dest)], env=_git_env(token), ctx=ctx)
        if code != 0:
            raise GitHubError(f"git clone failed: {_redact(out, token)[-300:]}")
        await ctx.progress(0.8, "scanning every file with ClamAV")
        report = await scan_clone(ctx, uid, dest, rel)
        status = report["status"]
    except asyncio.CancelledError:
        force_rmtree(dest)
        status, report = "none", {"note": "clone cancelled"}
        raise
    except Exception as e:
        force_rmtree(dest)
        status, report = "error", {"error": str(e)[:400]}
        raise
    finally:
        async with SessionLocal() as db:
            repo = (await db.execute(select(Repo).where(Repo.id == repo_id, Repo.user_id == uid))).scalar_one_or_none()
            if repo is not None:
                repo.clone_status = status
                repo.scan_report = report
                repo.cloned_path = rel if dest.exists() else ""
                await db.commit()
        await emit_user(uid, "repo:changed", {"action": "cloned", "id": repo_id, "status": status, "path": rel})
    await ulog(uid, f"{full_name} cloned to {rel} - ClamAV: {status} ({report.get('scanned', 0)} files scanned)", src="github")
    return {"path": rel, "status": status, "scanned": report.get("scanned", 0)}


async def rescan_job(ctx: JobContext, repo_id: int) -> dict:
    uid = ctx.user_id
    async with SessionLocal() as db:
        repo = (await db.execute(select(Repo).where(Repo.id == repo_id, Repo.user_id == uid))).scalar_one_or_none()
        if repo is None or not repo.cloned_path:
            raise GitHubError("repository is not cloned", 404)
        rel = repo.cloned_path
    dest = user_root(uid) / rel
    report = await scan_clone(ctx, uid, dest, rel)
    async with SessionLocal() as db:
        repo = (await db.execute(select(Repo).where(Repo.id == repo_id, Repo.user_id == uid))).scalar_one_or_none()
        if repo is not None:
            repo.clone_status = report["status"]
            repo.scan_report = report
            await db.commit()
    await emit_user(uid, "repo:changed", {"action": "scanned", "id": repo_id, "status": report["status"]})
    return {"status": report["status"], "scanned": report.get("scanned", 0)}


async def changed_files(uid: int, repo: Repo) -> list[dict]:
    if not repo.cloned_path:
        return []
    dest = user_root(uid) / repo.cloned_path
    if not (dest / ".git").exists():
        return []
    code, out = await _run_git(["status", "--porcelain", "--untracked-files=all"], env=_git_env(""), cwd=str(dest), timeout=60)
    files = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        flag, path = line[:2], line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if "D" in flag:
            continue  # deletions are never pushed (we never delete anything)
        p = dest / path
        if p.is_file():
            files.append({"path": path.replace("\\", "/"), "status": "new" if flag.strip() in ("??", "A") else "modified",
                          "size": p.stat().st_size})
    return files[:500]


async def push_new_branch(uid: int, repo: Repo, branch: str, message: str, paths: list[str]) -> dict:
    token = await token_for(uid)
    if not token:
        raise GitHubError("Link GitHub first (Settings -> GitHub)")
    if not repo.cloned_path:
        raise GitHubError("Clone the repository first")
    dest = (user_root(uid) / repo.cloned_path).resolve()
    if not paths:
        raise GitHubError("Select at least one file")
    files: list[tuple[str, bytes]] = []
    for p in paths[:200]:
        p = str(p).replace("\\", "/").strip().lstrip("/")
        if not p or ".." in p.split("/") or p.split("/")[0] == ".git":
            raise GitHubError(f"invalid path {p[:60]}")
        f = (dest / p).resolve()
        if dest not in f.parents or not f.is_file():
            raise GitHubError(f"{p} is not a file in the clone")
        data = f.read_bytes()
        if len(data) > 20 * 1024 * 1024:
            raise GitHubError(f"{p} is larger than 20 MB")
        scan = await clamav.scan_bytes(data)
        if scan.signature:
            raise GitHubError(f"{p} is infected ({scan.signature}) - not pushed")
        files.append((p, data))
    message = (message or "").strip()[:500] or f"Changes from Deminic ({len(files)} file(s))"
    owner, name = repo.full_name.split("/", 1)
    res = await SafeGitHub(token).create_branch_with_files(owner, name, branch.strip(), message, files,
                                                           base=repo.default_branch or None)
    await ulog(uid, f"pushed {len(files)} file(s) to NEW branch {res['branch']} on {repo.full_name}", src="github")
    return {**res, "files": [p for p, _ in files], "repo": repo.full_name}


async def git_available() -> bool:
    exe = shutil.which("git")
    return bool(exe) or sys.platform == "win32" and Path(r"C:\Program Files\Git\cmd\git.exe").exists()
