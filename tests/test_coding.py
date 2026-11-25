"""Coding section bug hunt: the OSV/ClamAV gate, editor sandbox, SVG viewing,
multi-file apply rollback, tenant isolation of workspaces."""
from __future__ import annotations

import time


def test_gate_blocks_vulnerable_dependency_and_suggests_upgrade(alice):
    r = alice.post("/api/coding/files/save", json={"path": "scratch/requirements.txt", "content": "jinja2==2.10\n"})
    assert r.status_code == 422, r.text
    body = r.text.lower()
    assert "jinja2" in body and ("upgrade" in body or "fixed" in body)
    assert alice.get("/api/coding/files/read?path=scratch/requirements.txt").status_code == 404  # never written


def test_gate_allows_safe_code_and_snapshots_versions(alice):
    r = alice.post("/api/coding/files/save", json={"path": "scratch/hello.py", "content": "print('hi')\n"})
    assert r.status_code == 200 and r.json()["verdict"] == "go", r.text
    sha = r.json()["sha"]
    r2 = alice.post("/api/coding/files/save", json={"path": "scratch/hello.py", "content": "print('hello')\n", "base_sha": sha})
    assert r2.status_code == 200, r2.text
    stale = alice.post("/api/coding/files/save", json={"path": "scratch/hello.py", "content": "x\n", "base_sha": sha})
    assert stale.status_code == 409  # optimistic concurrency
    hist = alice.get("/api/coding/files/history?path=scratch/hello.py").json()
    assert len(hist.get("versions", hist if isinstance(hist, list) else [])) >= 1


def test_path_traversal_refused(alice):
    for bad in ("../../etc/passwd", "..\\..\\windows\\win.ini", "C:/Windows/win.ini"):
        r = alice.get(f"/api/coding/files/read?path={bad}")
        assert r.status_code in (400, 403, 404), (bad, r.status_code)
    r = alice.post("/api/coding/files/save", json={"path": "../escape.py", "content": "x=1\n"})
    assert r.status_code in (400, 403), r.text


def test_svg_served_sandboxed(alice):
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><script>alert(1)</script><rect width="10" height="10"/></svg>'
    r = alice.post("/api/coding/files/new", json={"path": "scratch/pic.svg", "kind": "file", "content": svg})
    assert r.status_code in (200, 422), r.text
    if r.status_code == 200:
        raw = alice.get("/api/coding/files/raw?path=scratch/pic.svg")
        assert raw.headers["content-type"].startswith("image/svg+xml")
        assert "sandbox" in raw.headers["content-security-policy"] and raw.headers["x-content-type-options"] == "nosniff"


def test_workspace_isolation(alice, bob):
    alice.post("/api/coding/files/save", json={"path": "scratch/secret.py", "content": "TOKEN = 'alice-only'\n"})
    assert bob.get("/api/coding/files/read?path=scratch/secret.py").status_code == 404
    listing = bob.get("/api/coding/files/list?path=scratch").text
    assert "secret.py" not in listing


def test_multi_file_apply_rolls_back_when_one_is_blocked(alice):
    r = alice.post("/api/coding/apply", json={"files": [
        {"path": "scratch/ok_module.py", "content": "def f():\n    return 1\n"},
        {"path": "scratch/requirements-bad.txt", "content": "jinja2==2.10\n"},
    ]})
    if r.status_code == 200 and r.json().get("request_id"):
        rid = r.json()["request_id"]
        job = alice.wait_job(rid, timeout=180)
        assert job["status"] in ("reverted", "failed", "cancelled"), job
    else:
        assert r.status_code in (409, 422), r.text
    time.sleep(0.5)
    assert alice.get("/api/coding/files/read?path=scratch/ok_module.py").status_code == 404


def test_upload_scanned_by_clamav(alice):
    eicar = ("X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + "EICAR-STANDARD-ANTIVIRUS-" + "TEST-FILE!$H+H*").encode()
    r = alice.post("/api/coding/files/upload", data={"dir": "scratch"}, files={"files": ("readme.txt", eicar, "text/plain")})
    res = r.json()["results"][0]
    assert r.status_code == 200 and res["ok"] is False and "clamav" in res["error"].lower(), r.text
    assert alice.get("/api/coding/files/read?path=scratch/readme.txt").status_code == 404  # never written
