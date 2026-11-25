"""Bug hunt + AI evaluation + Locust stress test orchestrator.

Writes data/locust_reports/latest.json, rendered publicly at /locust.

  python stress/run_stress.py --users 20 --duration 4m
  (start the server for stress runs with RATE_LIMIT_AUTH=100000/minute, because
   every simulated account registers from this one machine's IP)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET  # parsing our own pytest junit output (trusted)
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "data" / "locust_reports"
PY = sys.executable


def run(cmd: list[str], env: dict | None = None, timeout: int = 7200) -> int:
    print("\n$ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=ROOT, env={**os.environ, **(env or {})}, timeout=timeout).returncode


def pytest_results(base: str) -> dict:
    xml = REPORTS / "pytest.xml"
    run([PY, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}"], env={"DEMINIC_TEST_URL": base})
    if not xml.exists():
        return {"passed": 0, "total": 0, "failed": [], "isolation_cases": []}
    root = ET.parse(xml).getroot()
    cases, failed, iso = 0, [], []
    for tc in root.iter("testcase"):
        cases += 1
        bad = tc.find("failure") is not None or tc.find("error") is not None
        skipped = tc.find("skipped") is not None
        name = f"{tc.get('classname', '').split('.')[-1]}::{tc.get('name')}"
        if bad:
            failed.append(name)
        if not skipped and ("isolation" in tc.get("classname", "") or "csrf" in tc.get("name", "")
                            or "idempotency" in tc.get("name", "") or "double_submit" in tc.get("name", "")
                            or "clamav" in tc.get("name", "") or "ssrf" in tc.get("name", "")):
            iso.append({"name": tc.get("name").replace("_", " ").replace("test ", "", 1), "ok": not bad})
    return {"passed": cases - len(failed), "total": cases, "failed": failed, "isolation_cases": iso}


def ai_eval(base: str) -> dict:
    out = REPORTS / "ai_eval.json"
    run([PY, str(ROOT / "stress" / "ai_eval.py"), "--base", base, "--out", str(out)])
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))
    return {"accuracy_pct": 0, "passed": 0, "total": 0, "cases": []}


def locust(base: str, users: int, rate: float, duration: str) -> tuple[dict, list, dict]:
    prefix = REPORTS / "run"
    run([PY, "-m", "locust", "-f", str(ROOT / "stress" / "locustfile.py"), "--headless", "-u", str(users), "-r", str(rate),
         "-t", duration, "--host", base, "--csv", str(prefix), "--only-summary", "--stop-timeout", "120"],
        env={"DEMINIC_STRESS_OUT": str(REPORTS)})
    stats_f = Path(f"{prefix}_stats.csv")
    endpoints, totals = [], {}
    if stats_f.exists():
        with stats_f.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                item = {
                    "name": row["Name"] if row["Name"].startswith(row["Type"] + " ") or row["Type"] == "AI" else f"{row['Type']} {row['Name']}".strip(),
                    "requests": int(row["Request Count"]),
                    "failures": int(row["Failure Count"]), "p50_ms": int(float(row["50%"] or 0)),
                    "p95_ms": int(float(row["95%"] or 0)), "p99_ms": int(float(row["99%"] or 0)),
                    "max_ms": int(float(row["Max Response Time"] or 0)), "rps": float(row["Requests/s"] or 0),
                }
                if row["Name"] == "Aggregated":
                    totals = item
                else:
                    endpoints.append(item)
    state_f = REPORTS / "locust_state.json"
    state = json.loads(state_f.read_text(encoding="utf-8")) if state_f.exists() else {}
    return totals, sorted(endpoints, key=lambda e: -e["requests"]), state


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("DEMINIC_TEST_URL", "http://127.0.0.1:8700"))
    ap.add_argument("--users", type=int, default=20)
    ap.add_argument("--spawn-rate", type=float, default=2)
    ap.add_argument("--duration", default="4m")
    ap.add_argument("--skip-pytest", action="store_true")
    ap.add_argument("--skip-eval", action="store_true")
    ap.add_argument("--skip-locust", action="store_true")
    a = ap.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    try:
        health = httpx.get(a.base + "/healthz", timeout=10).json()
    except Exception as e:  # noqa: BLE001
        print(f"Server not reachable at {a.base}: {e}")
        return 2
    t0 = time.time()
    tests = {"passed": 0, "total": 0, "failed": [], "isolation_cases": []} if a.skip_pytest else pytest_results(a.base)
    ev = {"accuracy_pct": 0, "passed": 0, "total": 0, "cases": []} if a.skip_eval else ai_eval(a.base)
    totals, endpoints, state = ({}, [], {}) if a.skip_locust else locust(a.base, a.users, a.spawn_rate, a.duration)

    req = totals.get("requests", 0)
    fails = totals.get("failures", 0)
    leaks = len(state.get("leaks", []))
    iso_cases = tests["isolation_cases"] + [
        {"name": f"locust: {state.get('probes', 0)} cross-account probes by {state.get('accounts', 0)} accounts - {leaks} leaks",
         "ok": leaks == 0},
    ]
    notes = []
    if tests["failed"]:
        notes.append("Failing tests: " + ", ".join(tests["failed"][:8]))
    if state.get("ai_errors"):
        notes.append(f"{state['ai_errors']} AI replies did not finish within the load test window (gateway queueing).")
    notes.append(f"Health at start: {health}")
    fail_pct = round(100 * fails / req, 2) if req else 0.0
    verdict_ok = (leaks == 0 and (tests["total"] == 0 or tests["passed"] / tests["total"] >= 0.95)
                  and (ev["total"] == 0 or ev["accuracy_pct"] >= 80) and fail_pct < 5)
    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "verdict": "PASS" if verdict_ok else "FAIL",
        "config": {"users": a.users, "spawn_rate": a.spawn_rate, "duration": a.duration,
                   "ai": "local gateway (gemma) + docker fallback", "base": "local server"},
        "totals": {"requests": req, "failures": fails, "failure_pct": fail_pct, "p50_ms": totals.get("p50_ms", 0),
                   "p95_ms": totals.get("p95_ms", 0), "rps": totals.get("rps", 0.0)},
        "endpoints": endpoints,
        "ai_eval": {"accuracy_pct": ev["accuracy_pct"], "passed": ev["passed"], "total": ev["total"],
                    "cases": [{"name": c["name"], "ok": c["ok"], "note": c.get("note", "")} for c in ev["cases"]]},
        "isolation": {"leaks": leaks, "probes": state.get("probes", 0), "cases": iso_cases},
        "tests": {"passed": tests["passed"], "total": tests["total"]},
        "ai_load": {"replies": state.get("ai_replies", 0),
                    "p50_s": round((state.get("ai_p50_ms") or 0) / 1000, 1), "p95_s": round((state.get("ai_p95_ms") or 0) / 1000, 1)},
        "notes": notes,
        "duration_s": round(time.time() - t0),
    }
    (REPORTS / "latest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("\n================ STRESS / BUG-HUNT SUMMARY ================")
    print(f"verdict          : {report['verdict']}")
    print(f"functional tests : {tests['passed']}/{tests['total']}")
    print(f"AI accuracy      : {ev['accuracy_pct']}% ({ev['passed']}/{ev['total']})")
    print(f"requests         : {req}  failures {fails} ({fail_pct}%)  p50 {report['totals']['p50_ms']} ms  p95 {report['totals']['p95_ms']} ms")
    print(f"cross-account    : {leaks} leaks in {state.get('probes', 0)} probes")
    print(f"AI under load    : {report['ai_load']['replies']} replies, p50 {report['ai_load']['p50_s']} s")
    print("report           : data/locust_reports/latest.json  ->  /locust")
    return 0 if verdict_ok else 1


if __name__ == "__main__":
    sys.exit(main())
