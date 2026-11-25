"""Create .env from .env.example, generating strong random secrets.

Standard library only so it can run before dependencies are installed.
Existing values in .env are preserved (re-running is safe).
Usage: python scripts/gen_env.py [--docker]
"""
from __future__ import annotations

import base64
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"
TARGET = ROOT / ".env"


def _parse(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main() -> None:
    existing = _parse(TARGET)
    generators = {
        "SECRET_KEY": lambda: secrets.token_hex(32),
        "ENCRYPTION_KEY": lambda: base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "DB_PASSWORD": lambda: secrets.token_urlsafe(24).replace("-", "a").replace("_", "b"),
        "REDIS_PASSWORD": lambda: secrets.token_urlsafe(24).replace("-", "c").replace("_", "d"),
    }
    values: dict[str, str] = {}
    lines_out: list[str] = []
    for raw in EXAMPLE.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            lines_out.append(raw)
            continue
        key, default = s.split("=", 1)
        key = key.strip()
        if key in existing and existing[key] and existing[key] != "__GENERATE__":
            val = existing[key]
        elif default.strip() == "__GENERATE__":
            val = generators[key]()
        else:
            val = default.strip()
        values[key] = val
        lines_out.append(f"{key}={val}")

    # Expand ${VAR} references so every consumer sees literal values.
    def expand(v: str) -> str:
        for k, kv in values.items():
            v = v.replace("${" + k + "}", kv)
        return v

    final = []
    for line in lines_out:
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            final.append(f"{k}={expand(v)}")
        else:
            final.append(line)
    TARGET.write_text("\n".join(final) + "\n", encoding="utf-8")
    print(f"[gen_env] wrote {TARGET}")


if __name__ == "__main__":
    sys.exit(main())
