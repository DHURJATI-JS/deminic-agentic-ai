"""Re-caption one recording from its saved frames with the local Gemma vision model."""
import base64
import json
import sys
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from PIL import Image  # noqa: E402

from app.ai.failsafe import loopback_if_local  # noqa: E402
from app.config import settings  # noqa: E402

WORK = ROOT / "data" / "media_work"  # raw frames (git-ignored)
CAPS = ROOT / "docs" / "media" / "captions.json"
HINTS = {"hero": "the public home page in immersive mode: a starry night sky with a moon behind glass panels, then a switch "
                 "to light mode showing a daytime sky with clouds, hills and birds, then the VR mode section."}

key = sys.argv[1]
frames = sorted((WORK / key).glob("f*.jpg"))
pick = [frames[int(len(frames) * f)] for f in (0.3, 0.6, 0.92)]
content = [{"type": "text", "text": (
    "These are frames from a screen recording of Deminic, a private, self-hosted AI assistant web app. "
    f"What happens in the recording: {HINTS[key]} "
    "Write ONE vivid caption for a GitHub README (maximum 30 words) describing what happens. "
    "Describe the action directly; never mention screenshots, frames, images or recordings. "
    "Plain text only: no quotes, no hashtags, no emoji, no markdown.")}]
for p in pick:
    im = Image.open(p).convert("RGB")
    im.thumbnail((896, 896))
    buf = BytesIO()
    im.save(buf, "JPEG", quality=85)
    content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}})
url = loopback_if_local(settings.default_gateway_url).rstrip("/") + "/chat/completions"
r = httpx.post(url, headers={"Authorization": f"Bearer {settings.default_gateway_key}"}, timeout=180,
               json={"model": settings.default_local_model, "messages": [{"role": "user", "content": content}],
                     "max_tokens": 90, "temperature": 0.4})
r.raise_for_status()
text = r.json()["choices"][0]["message"]["content"].strip().strip('"').replace("\n", " ")
caps = json.loads(CAPS.read_text(encoding="utf-8"))
caps[key] = {"caption": text, "model": settings.default_local_model, "frames": len(pick)}
CAPS.write_text(json.dumps(caps, indent=2, ensure_ascii=False), encoding="utf-8")
print(key, "->", text)
