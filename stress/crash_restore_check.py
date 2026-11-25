"""Process-level restore-point check (run by hand, see the two phases below).

Phase "start": creates a user with memories + a task, starts a NON-cancellable
memory wipe (server must run with DEMINIC_TEST_SLOW_WIPE=8 so it takes ~25 s)
and saves the state to a JSON file. Then you hard-kill the server mid-wipe.
Phase "verify": after restarting the server normally, checks that startup
recovery rolled everything back to the restore point.

  python stress/crash_restore_check.py start  state.json
  (kill the server while the wipe is running, restart it)
  python stress/crash_restore_check.py verify state.json
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
from helpers import Client  # noqa: E402


def start(state_file: str) -> None:
    c = Client().register("crash")
    for i in range(3):
        c.post("/api/memory", json={"section": "personal", "category": "general", "key": f"fact {i}", "value": f"value {i}"})
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    task = c.post("/api/personal/tasks", json={"title": "Water the plants", "due_at": due}).json()["task"]
    r = c.post("/api/memory/wipe", json={"section": "personal"}).json()
    time.sleep(10)  # step 1-2 done (memories soft-deleted, task marked stale), step 3 pending
    mid_mem = len(c.get("/api/memory?section=personal").json()["memories"])
    mid_stale = next(t for t in c.get("/api/personal/tasks?status=all").json()["tasks"] if t["task_no"] == task["task_no"])["stale"]
    Path(state_file).write_text(json.dumps({"cookies": dict(c.http.cookies), "username": c.username,
                                            "request_id": r["request_id"], "task_no": task["task_no"],
                                            "mid_memories": mid_mem, "mid_stale": mid_stale}), encoding="utf-8")
    print(f"wipe {r['request_id'][:8]} running; mid-way memories={mid_mem} task stale={mid_stale} -> KILL THE SERVER NOW")


def verify(state_file: str) -> int:
    st = json.loads(Path(state_file).read_text(encoding="utf-8"))
    c = Client()
    for k, v in st["cookies"].items():
        c.http.cookies.set(k, v)
    mem = c.get("/api/memory?section=personal").json()["memories"]
    task = next(t for t in c.get("/api/personal/tasks?status=all").json()["tasks"] if t["task_no"] == st["task_no"])
    job = next((j for j in c.get("/api/jobs").json()["jobs"] if j["request_id"] == st["request_id"]), {})
    rps = c.get("/api/restore-points").json()["restore_points"]
    ok = len(mem) == 3 and task["stale"] is False and job.get("status") == "reverted" and any(p["status"] == "restored" for p in rps)
    print(json.dumps({"memories_after_restart": len(mem), "task_stale_after_restart": task["stale"],
                      "job_status": job.get("status"), "job_detail": job.get("detail"),
                      "restore_points": [(p["label"], p["status"], p["changes"]) for p in rps],
                      "mid_way": {"memories": st["mid_memories"], "task_stale": st["mid_stale"]},
                      "PASS": ok}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    mode, path = sys.argv[1], sys.argv[2]
    sys.exit(start(path) if mode == "start" else verify(path))
