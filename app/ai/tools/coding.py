"""Coding-section agent tools (selected semantically via pgvector examples)."""
from __future__ import annotations

import re

from sqlalchemy import select

from app.ai.tools import ToolCtx, ToolResult, tool
from app.core.jobs import DuplicateJob
from app.core.utils import clip
from app.models import Repo, VulnScan
from app.services import editor, github, osv
from app.services.workspace import PathError

CODING = ("coding",)


@tool("scan_dependencies",
      "Check packages / requirements / package.json for known security vulnerabilities with OSV.dev and recommend safe "
      "versions or alternative packages",
      args={"packages": "packages with optional versions, e.g. 'requests==2.19.0, flask, lodash@4.17.15'",
            "ecosystem": "PyPI or npm (leave empty to guess)",
            "path": "optional workspace manifest to scan, e.g. scratch/requirements.txt"},
      sections=CODING,
      examples=["is requests 2.19.0 safe to use?", "check my requirements.txt for vulnerabilities",
                "does lodash 4.17.15 have any known CVEs", "scan these dependencies for security issues: flask==0.12, jinja2==2.10",
                "are there vulnerabilities in express 4.16", "audit the packages in package.json",
                "which version of urllib3 fixes the security bug", "is this npm package vulnerable"])
async def scan_dependencies(ctx: ToolCtx, args: dict) -> ToolResult:
    deps: list[osv.Dep] = []
    target = "chat"
    path = str(args.get("path") or "").strip()
    if path:
        try:
            rel, p = editor.safe_path(ctx.uid, path)
            text, _ = editor.read_text(p)
            if text is not None:
                deps = osv.deps_from_text(text, rel)
                target = rel
        except PathError:
            pass
    if not deps:
        raw = str(args.get("packages") or "")
        eco = str(args.get("ecosystem") or "")
        deps = osv.parse_package_list(raw, "npm" if eco.lower() == "npm" else "PyPI" if eco.lower() == "pypi" else "")
    if not deps:
        m = re.findall(r"([A-Za-z][\w.\-]*)\s*(?:==|@|\s)\s*v?(\d+\.\d+(?:\.\d+)?)", ctx.text)
        deps = osv.parse_package_list(", ".join(f"{n}=={v}" for n, v in m)) if m else []
    if not deps:
        return ToolResult(ok=False, text="No package names were found - ask which packages (and versions) to check.")
    await ctx.log(f"OSV.dev check of {len(deps)} package(s)")
    try:
        report = await osv.scan_deps(deps[:200], verify=True)
    except osv.ScannerUnavailable as e:
        return ToolResult(ok=False, text=f"The vulnerability database is unreachable right now ({e}).")
    report["target"] = target
    report["message"] = (f"{report['vulnerable']} vulnerable package(s)" if report["findings"]
                         else f"no known vulnerabilities in {len(report['checked'])} package(s)")
    row = VulnScan(user_id=ctx.uid, target=f"{target} ({ctx.ref})"[:500], kind="manual", verdict=report["verdict"],
                   result={**report, "source": {"chat_ref": ctx.ref}})
    ctx.db.add(row)
    await ctx.db.flush()
    lines = []
    for f in report["findings"]:
        top = f["vulns"][0]
        lines.append(f"- {f['name']} {f['version']} ({f['ecosystem']}): {f['count']} advisories, worst {f['max_severity']} "
                     f"(e.g. {top['id']}: {clip(top['summary'], 90)}). Recommendation: {f['recommendation']}")
    for c in report["checked"]:
        if not any(f["name"] == c["name"] for f in report["findings"]):
            lines.append(f"- {c['name']} {c['version']} ({c['ecosystem']}): no known vulnerabilities")
    for u in report["unknown"]:
        lines.append(f"- {u['name']}: NOT FOUND on {u['ecosystem']} (typo or hallucinated package?)")
    return ToolResult(
        text=f"OSV.dev scan #{row.id} ({report['message']}):\n" + "\n".join(lines),
        cards=[{"type": "vuln_report", "scan_id": row.id, "target": target, "verdict": report["verdict"],
                "findings": report["findings"][:10], "checked": len(report["checked"]),
                "unknown": [u["name"] for u in report["unknown"]], "ref": ctx.ref}])


@tool("read_workspace_file",
      "Open a specific EXISTING file by its path - in this coding chat's project folder (e.g. app.py, src/main.js) or "
      "the unified workspace (scratch/..., repos/<name>/...) - to review or change that file",
      args={"path": "file path relative to this chat's project (e.g. src/app.py) or a workspace path like scratch/app.py"},
      sections=CODING,
      examples=["look at scratch/app.py and fix the bug", "review my file repos/api/server.js",
                "explain the code in scratch/welcome.py", "open src/utils.py and add type hints",
                "what does repos/site/index.html do", "refactor calc.py in this project",
                "what's wrong in main.py"])
async def read_workspace_file(ctx: ToolCtx, args: dict) -> ToolResult:
    path = str(args.get("path") or "").strip()
    if not path:
        m = re.search(r"((?:scratch|repos|projects)/[\w./-]+|[\w./-]+\.[A-Za-z0-9]{1,6}\b)", ctx.text)
        path = m.group(1) if m else ""
    if not path:
        return ToolResult(ok=False, text="")  # nothing named - silently ignored, the model just answers
    project = editor.project_rel(ctx.chat_no)
    try:
        candidates = [editor.in_chat_project(path, ctx.chat_no)]
        if path.replace("\\", "/").strip().removeprefix("./").split("/")[0] not in editor.ROOTS:
            candidates.append(editor.in_chat_project(path, None))  # fall back to scratch/<path>
        resolved = [editor.safe_path(ctx.uid, c) for c in candidates]
    except PathError as e:
        return ToolResult(ok=False, text=f"Invalid workspace path: {e}")
    rel, p = next(((r, q) for r, q in resolved if q.is_file()), resolved[0])
    text, _ = editor.read_text(p)
    shown_path = rel[len(project) + 1:] if rel.startswith(project + "/") else rel
    if text is None:
        if not p.exists():
            return ToolResult(ok=False, text=f"{rel} does not exist yet (this chat's project is {project}/). If the user "
                                             f"wants it created, output the complete file in one fenced block whose info "
                                             f"string ends with path={shown_path}.")
        return ToolResult(ok=False, text=f"{rel} is not a text file (binary or larger than 2 MB).")
    await ctx.log(f"read workspace file {rel} ({len(text)} chars)")
    shown = text if len(text) <= 9000 else text[:9000] + "\n... [truncated]"
    lang = p.suffix.lstrip(".") or "text"
    return ToolResult(
        text=f"Contents of {rel}:\n```{lang} path={shown_path}\n{shown}\n```",
        system_addendum=f"When you change {rel}, output the COMPLETE updated file in one fenced block whose info string is "
                        f"'{lang} path={shown_path}' so the user can apply it through the security gate. Paths without "
                        f"scratch/, repos/ or projects/ are saved inside this chat's project ({project}/).",
        cards=[{"type": "file_open", "path": rel, "size": len(text), "lines": text.count("\n") + 1}])


@tool("start_stress_test",
      "Prepare a Locust load / stress test against a website or API the user owns (the user confirms and starts it)",
      args={"url": "target base URL", "users": "concurrent users (default 20, max 200)",
            "duration": "seconds (default 60, max 600)", "paths": "comma-separated paths, default /"},
      sections=CODING,
      examples=["stress test my website https://mysite.dev", "run a load test with 50 users on http://localhost:3000",
                "how many requests per second can my api handle", "benchmark my server under load for 2 minutes",
                "start a locust test against staging.example.com", "simulate 100 concurrent users hitting my homepage"])
async def start_stress_test(ctx: ToolCtx, args: dict) -> ToolResult:
    url = str(args.get("url") or "").strip()
    if not url:
        m = re.search(r"https?://[^\s'\"<>]+", ctx.text)
        url = m.group(0) if m else ""
    if url and "://" not in url:
        url = "https://" + url

    def _int(v, d, lo, hi):
        try:
            return max(lo, min(hi, int(float(str(v)))))
        except (TypeError, ValueError):
            return d

    users = _int(args.get("users"), 20, 1, 200)
    duration = _int(args.get("duration"), 60, 5, 600)
    paths = [p.strip() for p in str(args.get("paths") or "/").split(",") if p.strip().startswith("/")] or ["/"]
    await ctx.log(f"stress test prepared for {url or '(no url yet)'} - waiting for the user's authorisation")
    target = f"**{url}**" if url else "your site (add the URL in the Stress test tab)"
    return ToolResult(
        text=f"Prepared a Locust stress test (target {url or 'not given'}, {users} users, {duration}s, paths {paths}) - NOT started.",
        reply=(f"I've prepared a Locust stress test for {target}: **{users}** concurrent users for **{duration} s** on "
               f"`{', '.join(paths)}`.\n\nIt has **not started yet** - load-testing is only allowed against systems you own "
               "or are authorised to test. Press **Review & start** below, tick the authorisation box and start it; you "
               "can stop it at any time from Background tasks."),
        cards=[{"type": "locust_confirm", "url": url, "users": users, "duration": duration, "paths": paths}])


@tool("github_repos",
      "List the user's GitHub repositories with stars, forks, watchers, clones/views and local clone status",
      args={}, sections=CODING,
      examples=["show my github repos", "how many stars do my repositories have", "list my repositories",
                "which of my repos are cloned", "what are my most popular projects on github", "github repo stats"])
async def github_repos(ctx: ToolCtx, args: dict) -> ToolResult:
    if not ctx.settings.github_token_enc:
        return ToolResult(ok=False, text="GitHub is not linked.",
                          reply="Your GitHub account isn't linked yet. Open the **GitHub** tab (or Settings → GitHub), paste a "
                                "personal access token and I'll list all your repositories with stars, forks, watchers, clones "
                                "and views. I can only read, clone and push **new** branches - never delete or modify anything.",
                          cards=[{"type": "github_repos", "linked": False, "repos": []}])
    rows = (await ctx.db.execute(select(Repo).where(Repo.user_id == ctx.uid).order_by(Repo.stars.desc()).limit(30))).scalars().all()
    repos = [github.repo_view(r) for r in rows]
    lines = [f"- {r['full_name']}: ★{r['stars']} forks {r['forks']} watchers {r['watchers']} issues {r['open_issues']}"
             f"{' clones ' + str(r['clones']) if r['clones'] is not None else ''} [{r['clone_status']}]" for r in repos]
    return ToolResult(text=f"{len(repos)} repositories (last sync shown in the GitHub tab):\n" + "\n".join(lines[:30]),
                      cards=[{"type": "github_repos", "linked": True, "login": ctx.settings.github_login, "repos": repos[:12]}])


async def _find_repo(ctx: ToolCtx, name: str) -> Repo | None:
    name = (name or "").strip().lower().removeprefix("https://github.com/").strip("/")
    rows = (await ctx.db.execute(select(Repo).where(Repo.user_id == ctx.uid))).scalars().all()
    if not rows:
        return None
    for r in rows:
        if name and (r.full_name.lower() == name or r.name.lower() == name):
            return r
    cands = [r for r in rows if name and (name in r.full_name.lower() or r.name.lower() in name)]
    return cands[0] if len(cands) == 1 else None


@tool("clone_repo",
      "Clone one of the user's GitHub repositories into the coding workspace (every file is then scanned by ClamAV)",
      args={"repo": "repository name or owner/name"},
      sections=CODING,
      examples=["clone my repo deminic", "download my project from github into the editor", "clone owner/website so I can edit it",
                "get a local copy of my api repository", "pull my portfolio repo into the workspace"])
async def clone_repo(ctx: ToolCtx, args: dict) -> ToolResult:
    if not ctx.settings.github_token_enc:
        return ToolResult(ok=False, text="GitHub is not linked yet - link it in the GitHub tab first.")
    repo = await _find_repo(ctx, str(args.get("repo") or ""))
    if repo is None:
        return ToolResult(ok=False, text="I couldn't match that to exactly one of the user's synced repositories - ask which one "
                                         "(the GitHub tab lists them).")
    if repo.cloned_path:
        return ToolResult(text=f"{repo.full_name} is already cloned at {repo.cloned_path}.",
                          cards=[{"type": "repo_clone", "repo": repo.full_name, "repo_id": repo.id, "path": repo.cloned_path,
                                  "status": repo.clone_status}])
    from app.routers.coding import spawn_clone

    try:
        rid = await spawn_clone(ctx.uid, repo)
    except DuplicateJob:
        return ToolResult(text=f"{repo.full_name} is already being cloned.")
    await ctx.log(f"clone job {rid[:8]} started for {repo.full_name}")
    return ToolResult(text=f"Started cloning {repo.full_name} (background task {rid[:8]}); every file will be scanned by ClamAV.",
                      cards=[{"type": "repo_clone", "repo": repo.full_name, "repo_id": repo.id, "request_id": rid,
                              "status": "cloning"}])


@tool("push_new_branch",
      "Push changed files of a cloned repository to a NEW GitHub branch (never modifies or deletes existing branches); "
      "the user confirms which files to push",
      args={"repo": "repository name", "branch": "new branch name, e.g. fix/login-bug", "message": "commit message"},
      sections=CODING,
      examples=["push my changes to a new branch called fix-login", "create a branch feature/dark-mode with my edits",
                "commit these changes to github on a new branch", "open a new branch on my repo with the updated files",
                "push the fixes to github as branch hotfix-1"])
async def push_new_branch(ctx: ToolCtx, args: dict) -> ToolResult:
    if not ctx.settings.github_token_enc:
        return ToolResult(ok=False, text="GitHub is not linked - link it first.")
    repo = await _find_repo(ctx, str(args.get("repo") or ""))
    if repo is None:
        cloned = (await ctx.db.execute(select(Repo).where(Repo.user_id == ctx.uid, Repo.cloned_path != ""))).scalars().all()
        repo = cloned[0] if len(cloned) == 1 else None
    if repo is None or not repo.cloned_path:
        return ToolResult(ok=False, text="Which cloned repository should I push from? (Clone it first in the GitHub tab.)")
    files = await github.changed_files(ctx.uid, repo)
    branch = re.sub(r"[^A-Za-z0-9._/-]", "-", str(args.get("branch") or "deminic/changes")).strip("-/")[:100] or "deminic/changes"
    message = clip(str(args.get("message") or f"Changes from {ctx.user.ai_name}"), 300)
    await ctx.log(f"push proposal for {repo.full_name}: {len(files)} changed file(s) -> new branch {branch}")
    return ToolResult(
        text=f"{len(files)} changed file(s) in {repo.cloned_path}. A confirmation card lets the user pick files and push them "
             f"to the NEW branch '{branch}' (nothing is pushed until they click Push).",
        cards=[{"type": "push_branch", "repo": repo.full_name, "repo_id": repo.id, "branch": branch, "message": message,
                "files": files[:100]}])
