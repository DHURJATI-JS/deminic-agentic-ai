"""Coding workspace: per-chat project folders, rename / copy protections,
.zip + file downloads and uploads (ClamAV + gate) - including isolation
between accounts. Each Client logs in exactly once (one login per account)."""
from __future__ import annotations

import io
import time
import zipfile


def _save(c, path, content):
    r = c.post("/api/coding/files/save", json={"path": path, "content": content, "force": True})
    assert r.status_code == 200, r.text
    return r.json()


def _project(c, section="coding"):
    no = c.new_chat(section)
    r = c.post("/api/coding/projects/ensure", json={"chat_no": no})
    return no, r


def _wait_job(c, rid, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = next((x for x in c.get("/api/jobs").json()["jobs"] if x["request_id"] == rid), None)
        if j and j["status"] not in ("running", "scheduled", "queued"):
            return j
        time.sleep(1)
    raise TimeoutError(rid)


# --------------------------------------------------------------------------- per-chat projects
def test_chat_project_created_and_isolated_between_accounts(alice, bob):
    no, r = _project(alice)
    assert r.status_code == 200, r.text
    assert r.json()["path"] == f"projects/chat-{no}"
    _save(alice, f"projects/chat-{no}/secret_alice.py", "TOKEN = 'alice-only'\n")
    assert any(p["chat_no"] == no for p in alice.get("/api/coding/projects").json()["projects"])
    tree = alice.get(f"/api/coding/files/list?path=projects/chat-{no}").json()["entries"]
    assert any(e["name"] == "secret_alice.py" for e in tree)

    # bob's chat may have the SAME number - but it is a different, private folder
    bno, br = _project(bob)
    assert br.status_code == 200
    assert bob.get(f"/api/coding/files/read?path=projects/chat-{no}/secret_alice.py").status_code == 404
    names = [e["name"] for e in bob.get(f"/api/coding/files/list?path=projects/chat-{bno}").json()["entries"]]
    assert "secret_alice.py" not in names
    assert bob.get(f"/api/coding/files/download?path=projects/chat-{no}/secret_alice.py").status_code == 404
    # bob cannot create a project for a chat number he does not own
    assert bob.post("/api/coding/projects/ensure", json={"chat_no": bno + 999}).status_code == 404


def test_personal_chat_has_no_project(alice):
    no, r = _project(alice, "personal")
    assert r.status_code == 400


def test_apply_from_chat_lands_in_that_chats_project(alice):
    no, _ = _project(alice)
    r = alice.post("/api/coding/apply", json={"chat_ref": f"chat#{no}.1", "files": [
        {"path": "src/util.py", "content": "def add(a, b):\n    return a + b\n"},
        {"path": "scratch/shared_note.md", "content": "# unified workspace still reachable\n"}]})
    assert r.status_code == 200, r.text
    assert r.json()["paths"] == [f"projects/chat-{no}/src/util.py", "scratch/shared_note.md"]
    assert _wait_job(alice, r.json()["request_id"])["status"] == "completed"
    assert "def add" in alice.get(f"/api/coding/files/read?path=projects/chat-{no}/src/util.py").json()["content"]
    assert alice.get("/api/coding/files/read?path=scratch/shared_note.md").status_code == 200


def test_apply_with_foreign_chat_ref_is_refused(alice, bob):
    no, _ = _project(alice)
    r = bob.post("/api/coding/apply", json={"chat_ref": f"chat#{no + 500}", "files": [{"path": "x.py", "content": "x = 1\n"}]})
    assert r.status_code == 404


# --------------------------------------------------------------------------- rename
def test_rename_protections(alice):
    _save(alice, "scratch/rn_one.py", "ONE = 1\n")
    _save(alice, "scratch/rn_two.py", "TWO = 2\n")
    no, _ = _project(alice)
    for bad in ("scratch/../../evil.py", "../evil.py", "scratch/..\\..\\evil.py", "other/evil.py", "scratch/.git",
                "scratch/con.py", "scratch/a:b.py", "scratch/bad?.py"):
        r = alice.post("/api/coding/files/rename", json={"path": "scratch/rn_one.py", "new_path": bad})
        assert r.status_code in (400, 403), (bad, r.status_code, r.text)
    for container in ("scratch", "repos", "projects", f"projects/chat-{no}"):
        r = alice.post("/api/coding/files/rename", json={"path": container, "new_path": "scratch/renamed_container"})
        assert r.status_code in (403, 404), (container, r.status_code, r.text)
    # moving a file onto a container path is refused too
    assert alice.post("/api/coding/files/rename", json={"path": "scratch/rn_one.py", "new_path": "projects/chat-77777"}).status_code == 403
    # no silent overwrite
    r = alice.post("/api/coding/files/rename", json={"path": "scratch/rn_one.py", "new_path": "scratch/rn_two.py"})
    assert r.status_code == 409 and r.json()["code"] == "exists"
    assert alice.get("/api/coding/files/read?path=scratch/rn_one.py").status_code == 200
    # explicit overwrite works and is undoable via the version history
    r = alice.post("/api/coding/files/rename", json={"path": "scratch/rn_one.py", "new_path": "scratch/rn_two.py", "overwrite": True})
    assert r.status_code == 200, r.text
    assert alice.get("/api/coding/files/read?path=scratch/rn_one.py").status_code == 404
    assert "ONE = 1" in alice.get("/api/coding/files/read?path=scratch/rn_two.py").json()["content"]
    versions = alice.get("/api/coding/files/history?path=scratch/rn_two.py").json()["versions"]
    assert any(v["reason"] == "overwritten" for v in versions)
    assert any(v["reason"] == "rename_from" for v in alice.get("/api/coding/files/history?path=scratch/rn_one.py").json()["versions"])
    # rename back (undo)
    r = alice.post("/api/coding/files/rename", json={"path": "scratch/rn_two.py", "new_path": "scratch/rn_back.py"})
    assert r.status_code == 200


def test_rename_to_manifest_runs_the_gate(alice):
    _save(alice, "scratch/deps_notes.txt", "jinja2==2.10\n")
    r = alice.post("/api/coding/files/rename", json={"path": "scratch/deps_notes.txt", "new_path": "scratch/requirements.txt"})
    assert r.status_code == 422, r.text
    assert "jinja2" in r.text.lower()
    assert alice.get("/api/coding/files/read?path=scratch/deps_notes.txt").status_code == 200  # untouched
    assert alice.get("/api/coding/files/read?path=scratch/requirements.txt").status_code == 404


def test_rename_folder_and_isolation(alice, bob):
    _save(alice, "scratch/rn_dir/inner.py", "x = 1\n")
    r = alice.post("/api/coding/files/rename", json={"path": "scratch/rn_dir", "new_path": "scratch/rn_dir_new"})
    assert r.status_code == 200 and r.json()["type"] == "dir"
    assert alice.get("/api/coding/files/read?path=scratch/rn_dir_new/inner.py").status_code == 200
    # bob renaming alice's path only ever touches his own workspace -> not found
    assert bob.post("/api/coding/files/rename", json={"path": "scratch/rn_dir_new", "new_path": "scratch/stolen"}).status_code == 404


# --------------------------------------------------------------------------- copy
def test_copy_file_and_folder(alice):
    _save(alice, "scratch/cp_src/main.py", "print('main')\n")
    _save(alice, "scratch/cp_src/lib/util.py", "def f():\n    return 1\n")
    r = alice.post("/api/coding/files/copy", json={"path": "scratch/cp_src/main.py", "new_path": "scratch/cp_src/main copy.py"})
    assert r.status_code == 200, r.text
    assert alice.get("/api/coding/files/read?path=scratch/cp_src/main copy.py").json()["content"] == "print('main')\n"
    assert alice.post("/api/coding/files/copy", json={"path": "scratch/cp_src/main.py", "new_path": "scratch/cp_src/main copy.py"}).status_code == 409
    assert alice.post("/api/coding/files/copy", json={"path": "scratch/cp_src/main.py", "new_path": "scratch/cp_src/main copy.py", "overwrite": True}).status_code == 200
    r = alice.post("/api/coding/files/copy", json={"path": "scratch/cp_src", "new_path": "scratch/cp_dst"})
    assert r.status_code == 200 and r.json()["type"] == "dir" and r.json()["files"] >= 3
    assert alice.get("/api/coding/files/read?path=scratch/cp_dst/lib/util.py").status_code == 200
    assert alice.post("/api/coding/files/copy", json={"path": "scratch/cp_src", "new_path": "scratch/cp_src/inside"}).status_code == 400
    assert alice.post("/api/coding/files/copy", json={"path": "scratch/cp_src", "new_path": "scratch/cp_dst"}).status_code == 409
    assert alice.post("/api/coding/files/copy", json={"path": "scratch/cp_src/main.py", "new_path": "../../outside.py"}).status_code == 400


def test_copy_into_manifest_is_gated(alice):
    _save(alice, "scratch/old_deps.txt", "jinja2==2.10\n")
    r = alice.post("/api/coding/files/copy", json={"path": "scratch/old_deps.txt", "new_path": "scratch/gated/requirements.txt"})
    assert r.status_code == 422
    assert alice.get("/api/coding/files/read?path=scratch/gated/requirements.txt").status_code == 404


# --------------------------------------------------------------------------- download
def test_zip_and_file_download(alice, bob):
    _save(alice, "scratch/zipme/a.py", "A = 1\n")
    _save(alice, "scratch/zipme/sub/b.txt", "bee\n")
    r = alice.get("/api/coding/files/download?path=scratch/zipme")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/zip")
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-content-type-options"] == "nosniff"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "zipme/a.py" in names and "zipme/sub/b.txt" in names
    f = alice.get("/api/coding/files/download?path=scratch/zipme/a.py")
    assert f.status_code == 200 and f.content == b"A = 1\n"
    assert f.headers["content-disposition"].startswith("attachment") and f.headers["x-content-type-options"] == "nosniff"
    assert bob.get("/api/coding/files/download?path=scratch/zipme").status_code == 404
    assert alice.get("/api/coding/files/download?path=../../etc").status_code == 400


# --------------------------------------------------------------------------- upload (existing endpoint)
def test_upload_into_chat_project_with_per_file_results(alice):
    no, _ = _project(alice)
    files = [("files", ("notes.txt", b"hello workspace\n", "text/plain")),
             ("files", ("con.txt", b"reserved name\n", "text/plain")),
             ("files", ("pic.svg", b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>', "image/svg+xml"))]
    r = alice.post("/api/coding/files/upload", data={"dir": f"projects/chat-{no}"}, files=files)
    assert r.status_code == 200, r.text
    res = {x["name"]: x for x in r.json()["results"]}
    assert res["notes.txt"]["ok"] and res["notes.txt"]["path"] == f"projects/chat-{no}/notes.txt"
    assert not res["con.txt"]["ok"]
    assert res["pic.svg"]["ok"]
    assert alice.get(f"/api/coding/files/read?path=projects/chat-{no}/notes.txt").json()["content"] == "hello workspace\n"
