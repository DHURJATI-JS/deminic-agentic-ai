"""Render README.md from the template, filling in the locally generated AI captions."""
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TPL = ROOT / "docs" / "README.template.md"
caps = json.loads((ROOT / "docs" / "media" / "captions.json").read_text(encoding="utf-8"))


def sub(m):
    key = m.group(1)
    if key not in caps:
        raise SystemExit(f"missing caption: {key}")
    return html.escape(caps[key]["caption"].rstrip())


out = re.sub(r"\[\[caption:([a-z]+)\]\]", sub, TPL.read_text(encoding="utf-8"))
missing = [p for p in re.findall(r'(?:src|href)="((?:docs|static)/[^"]+)"', out) if not (ROOT / p).exists()]
if missing:
    raise SystemExit(f"missing media: {missing}")
(ROOT / "README.md").write_text(out, encoding="utf-8")
print("README.md written,", len(out), "chars; captions:", ", ".join(sorted(caps)))
