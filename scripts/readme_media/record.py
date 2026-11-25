"""Capture README media from the LIVE app (real AI answers, nothing mocked):
  * screen recordings via Chrome's screencast (CDP) -> MP4 (H.264) + GIF preview
  * screenshots -> WebP
  * captions written by the local Gemma vision model from frames of each recording
Pipeline (run from the repo root while the app is running; needs `uv pip install imageio-ffmpeg`):
  1. .venv/Scripts/python scripts/readme_media/record.py [hero chat business coding status extra]
  2. .venv/Scripts/python scripts/readme_media/recaption.py hero      (optional: re-caption one clip)
  3. .venv/Scripts/python scripts/readme_media/publish_video.py      (home-page copies + poster frames)
  4. .venv/Scripts/python scripts/readme_media/play_thumbs.py        (README play-button thumbnails)
  5. .venv/Scripts/python scripts/readme_media/render_readme.py      (docs/README.template.md -> README.md)
"""
import asyncio
import base64
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import imageio_ffmpeg  # noqa: E402
from helpers import Client  # noqa: E402
from PIL import Image  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from app.ai.failsafe import loopback_if_local  # noqa: E402
from app.config import settings  # noqa: E402

MEDIA = ROOT / "docs" / "media"
WORK = ROOT / "data" / "media_work"  # raw frames (git-ignored)
BASE = "http://127.0.0.1:8700"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
VIEW = {"width": 1280, "height": 800}
MEDIA.mkdir(parents=True, exist_ok=True)
WORK.mkdir(parents=True, exist_ok=True)
CAPTIONS_FILE = MEDIA / "captions.json"
captions = json.loads(CAPTIONS_FILE.read_text(encoding="utf-8")) if CAPTIONS_FILE.exists() else {}


# ---------------------------------------------------------------- recording
class Recorder:
    def __init__(self, page, name):
        self.page, self.name = page, name
        self.dir = WORK / name
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True)
        self.frames: list[tuple[Path, float]] = []

    async def start(self):
        self.cdp = await self.page.context.new_cdp_session(self.page)
        self.cdp.on("Page.screencastFrame", self._frame)
        await self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 90, "maxWidth": VIEW["width"],
                                                     "maxHeight": VIEW["height"], "everyNthFrame": 1})

    def _frame(self, params):
        ts = params.get("metadata", {}).get("timestamp") or time.time()
        p = self.dir / f"f{len(self.frames):05d}.jpg"
        p.write_bytes(base64.b64decode(params["data"]))
        self.frames.append((p, ts))
        asyncio.ensure_future(self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]}))

    async def stop(self, hold=1.4):
        await self.page.wait_for_timeout(300)
        await self.cdp.send("Page.stopScreencast")
        await self.page.wait_for_timeout(200)
        if len(self.frames) < 3:
            raise RuntimeError(f"{self.name}: only {len(self.frames)} frames captured")
        lines = []
        for i, (p, ts) in enumerate(self.frames):
            nxt = self.frames[i + 1][1] if i + 1 < len(self.frames) else ts + hold
            dur = min(max(nxt - ts, 0.01), 2.0)
            lines += [f"file '{p.as_posix()}'", f"duration {dur:.4f}"]
        lines.append(f"file '{self.frames[-1][0].as_posix()}'")  # concat quirk: repeat the last frame
        lst = self.dir / "list.txt"
        lst.write_text("\n".join(lines), encoding="utf-8")
        mp4 = MEDIA / f"{self.name}.mp4"
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-vf", "fps=30,scale=1280:-2:flags=lanczos,format=yuv420p", "-c:v", "libx264", "-preset", "slow",
                        "-crf", "21", "-movflags", "+faststart", str(mp4)], check=True)
        gif = MEDIA / f"{self.name}.gif"
        for width, fps in ((860, 12), (760, 10), (640, 9)):
            subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(mp4), "-vf",
                            f"fps={fps},scale={width}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=200:stats_mode=diff[p];"
                            "[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle", "-loop", "0", str(gif)], check=True)
            if gif.stat().st_size < 7.5 * 1024 * 1024:
                break
        dur = self.frames[-1][1] - self.frames[0][1]
        print(f"  {self.name}: {len(self.frames)} frames, {dur:.1f}s -> mp4 {mp4.stat().st_size // 1024} KB, gif {gif.stat().st_size // 1024} KB")
        n = len(self.frames)
        return [self.frames[int(n * f)][0] for f in (0.3, 0.6, 0.92)]


def webp(png: bytes, name: str, size=None, quality=86):
    from io import BytesIO
    im = Image.open(BytesIO(png)).convert("RGB")
    if size:
        im = im.resize(size, Image.Resampling.LANCZOS)
    im.save(MEDIA / name, "WEBP", quality=quality, method=6)
    print(f"  screenshot {name}: {(MEDIA / name).stat().st_size // 1024} KB")


# ---------------------------------------------------------------- AI captions (local Gemma vision)
def caption(key: str, frames: list[Path], hint: str) -> None:
    content = [{"type": "text", "text": (
        "These are frames from a screen recording of Deminic, a private, self-hosted AI assistant web app. "
        f"What happens in the recording: {hint} "
        "Write ONE vivid caption for a GitHub README (maximum 30 words) describing what happens. "
        "Describe the action directly; never mention screenshots, frames, images or recordings. "
        "Plain text only: no quotes, no hashtags, no emoji, no markdown.")}]
    for p in frames:
        im = Image.open(p).convert("RGB")
        im.thumbnail((896, 896))
        from io import BytesIO
        buf = BytesIO()
        im.save(buf, "JPEG", quality=85)
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}})
    url = loopback_if_local(settings.default_gateway_url).rstrip("/") + "/chat/completions"
    r = httpx.post(url, headers={"Authorization": f"Bearer {settings.default_gateway_key}"}, timeout=180,
                   json={"model": settings.default_local_model, "messages": [{"role": "user", "content": content}],
                         "max_tokens": 90, "temperature": 0.4})
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"].strip().strip('"').replace("\n", " ")
    captions[key] = {"caption": text, "model": settings.default_local_model, "frames": len(frames)}
    CAPTIONS_FILE.write_text(json.dumps(captions, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  caption[{key}]: {text}")


# ---------------------------------------------------------------- helpers
async def send_and_wait(page, text, typing_delay=28):
    await page.click("#c-input")
    await page.type("#c-input", text, delay=typing_delay)
    await page.keyboard.press("Enter")
    await page.wait_for_selector("#c-stop:not(.hidden)", timeout=15000)
    # a hidden element is never "visible": wait for it to be attached with the class instead
    await page.wait_for_selector("#c-stop.hidden", state="attached", timeout=180000)
    await page.wait_for_timeout(1300)


async def clean_toasts(page):
    await page.evaluate("() => { const t = document.getElementById('toasts'); if (t) t.innerHTML = ''; }")


async def demo_account():
    # gateway on localhost so no LAN address ever appears in the published media
    c = Client().register("demo", name="Alex Rivera", ai_name="Nova", business_name="Rivera Studio",
                          gateway_url="http://localhost:8000/v1", gateway_key=settings.default_gateway_key)
    return c, [{"name": k, "value": v, "url": BASE} for k, v in c.http.cookies.items()]


# ---------------------------------------------------------------- clips
async def clip_hero(b):
    ctx = await b.new_context(color_scheme="dark", viewport=VIEW)
    page = await ctx.new_page()
    await page.goto(f"{BASE}/", wait_until="networkidle")
    await page.evaluate("import('/static/js/immersive.js').then(m => m.setImmersive(true))")
    await page.mouse.move(640, 400)
    await page.wait_for_timeout(1500)
    rec = Recorder(page, "hero")
    await rec.start()
    for i in range(36):  # slow pointer drift -> parallax
        await page.mouse.move(640 + 260 * (i / 36), 400 - 120 * (i / 36))
        await page.wait_for_timeout(55)
    await page.wait_for_timeout(1200)
    await page.click("header [data-theme-toggle]")  # night -> day: clouds, hills, birds
    await page.wait_for_timeout(3200)
    await page.evaluate("document.getElementById('immersive').scrollIntoView({behavior: 'smooth'})")
    await page.wait_for_timeout(2600)
    frames = await rec.stop()
    caption("hero", frames, "the public home page in immersive mode: a starry night sky with a moon behind glass panels, "
            "then a switch to light mode showing a daytime sky with clouds, hills and birds, then the VR mode section.")
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(900)
    webp(await page.screenshot(), "home-day.webp")
    await ctx.close()


async def clip_chat(b, cookies):
    ctx = await b.new_context(color_scheme="dark", viewport=VIEW)
    await ctx.add_cookies(cookies)
    page = await ctx.new_page()
    await page.goto(f"{BASE}/app", wait_until="networkidle")
    await page.wait_for_timeout(1200)
    await clean_toasts(page)
    rec = Recorder(page, "chat")
    await rec.start()
    await page.click("#new-chat")
    await page.wait_for_timeout(700)
    await send_and_wait(page, "Plan my Saturday: gym at 9am, lunch with mum at 1pm, and remind me to call the bank at 4pm.")
    await page.wait_for_timeout(1200)
    frames = await rec.stop()
    caption("chat", frames, "a user types a request to plan their Saturday; the local AI streams its answer and creates "
            "calendar events and a task with a reminder, shown as interactive cards.")
    webp(await page.screenshot(), "chat.webp")
    await ctx.close()


async def clip_business(b, cookies):
    ctx = await b.new_context(color_scheme="light", viewport=VIEW)
    await ctx.add_cookies(cookies)
    page = await ctx.new_page()
    await page.goto(f"{BASE}/app", wait_until="networkidle")
    await page.click('#section-switch [data-section="business"]')
    await page.wait_for_timeout(1200)
    await clean_toasts(page)
    rec = Recorder(page, "business")
    await rec.start()
    await page.click("#new-chat")
    await page.wait_for_timeout(600)
    await send_and_wait(page, "Add client Acme Corp - they owe us 2,000 by Friday for the website redesign.")
    await page.wait_for_timeout(800)
    await page.click('#tabs [data-tab="clients"]')
    await page.wait_for_timeout(2200)
    await page.click('#tabs [data-tab="reminders"]')
    await page.wait_for_timeout(2200)
    frames = await rec.stop()
    caption("business", frames, "in business mode the user asks the AI to add a client who owes money; the AI creates the client "
            "and a payment reminder, then the Clients and Payment reminders tabs show them.")
    await page.click('#tabs [data-tab="clients"]')
    await page.wait_for_timeout(1500)
    webp(await page.screenshot(), "business-clients.webp")
    await ctx.close()


async def clip_coding(b, cookies):
    ctx = await b.new_context(color_scheme="dark", viewport=VIEW)
    await ctx.add_cookies(cookies)
    page = await ctx.new_page()
    await page.goto(f"{BASE}/app", wait_until="networkidle")
    await page.click('#section-switch [data-section="coding"]')
    await page.wait_for_timeout(1000)
    await page.click('#tabs [data-tab="scanner"]')
    await page.wait_for_timeout(1200)
    await clean_toasts(page)
    rec = Recorder(page, "coding")
    await rec.start()
    ta = page.locator("#view textarea").first
    await ta.fill("")
    await ta.type("requests==2.19.0\nflask==0.12\njinja2==2.10", delay=35)
    await page.wait_for_timeout(400)
    await page.locator("#view button:has-text('Scan')").first.click()
    await page.wait_for_timeout(9000)
    await page.click('#tabs [data-tab="editor"]')
    await page.wait_for_timeout(2500)
    frames = await rec.stop()
    caption("coding", frames, "the coding studio's vulnerability scanner checks pinned Python packages against OSV.dev and lists "
            "known vulnerabilities with fixed versions, then the built-in code editor opens.")
    webp(await page.screenshot(), "coding-editor.webp")
    await page.click('#tabs [data-tab="scanner"]')
    await page.wait_for_timeout(1500)
    webp(await page.screenshot(), "coding-scanner.webp")
    await ctx.close()


async def clip_status(b, cookies):
    ctx = await b.new_context(color_scheme="dark", viewport=VIEW)
    await ctx.add_cookies(cookies)
    page = await ctx.new_page()
    await page.goto(f"{BASE}/app", wait_until="networkidle")
    await page.wait_for_timeout(1200)
    await clean_toasts(page)
    rec = Recorder(page, "status")
    await rec.start()
    await page.click("header [data-status-open]")
    await page.wait_for_function("() => document.querySelectorAll('[data-status-tests] .status-test-row').length === 2 && "
                                 "!document.querySelector('[data-status-tests] .status-spinner')", timeout=120000)
    await page.wait_for_timeout(6500)
    frames = await rec.stop()
    caption("status", frames, "the System status dialog pings every component live every two seconds with latency graphs, "
            "and a real AI response test gets the answer pong from the local models.")
    webp(await page.screenshot(), "status.webp")
    await ctx.close()


async def shots_extra(b, cookies):
    # phone views: immersive home, light chat, dark immersive chat
    phones = []
    for path, scheme, immersive in (("/", "dark", True), ("/app", "light", False), ("/app", "dark", True)):
        ctx = await b.new_context(color_scheme=scheme, viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                  is_mobile=True, has_touch=True)
        if path == "/app":
            await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        await page.goto(f"{BASE}{path}", wait_until="networkidle")
        await page.wait_for_timeout(1000)
        if immersive:
            await page.evaluate("import('/static/js/immersive.js').then(m => m.setImmersive(true))")
            await page.wait_for_timeout(1800)
        await clean_toasts(page)
        phones.append(await page.screenshot())
        await ctx.close()
    from io import BytesIO
    ims = [Image.open(BytesIO(x)).convert("RGB").resize((390, 844), Image.Resampling.LANCZOS) for x in phones]
    gap = 28
    sheet = Image.new("RGB", (390 * 3 + gap * 4, 844 + gap * 2), (12, 16, 32))
    for i, im in enumerate(ims):
        sheet.paste(im, (gap + i * (390 + gap), gap))
    sheet.save(MEDIA / "phones.webp", "WEBP", quality=86, method=6)
    print(f"  screenshot phones.webp: {(MEDIA / 'phones.webp').stat().st_size // 1024} KB")


async def main(which):
    async with async_playwright() as p:
        b = await p.chromium.launch()
        _, cookies = await demo_account()
        steps = {"hero": lambda: clip_hero(b), "chat": lambda: clip_chat(b, cookies),
                 "business": lambda: clip_business(b, cookies), "coding": lambda: clip_coding(b, cookies),
                 "status": lambda: clip_status(b, cookies), "extra": lambda: shots_extra(b, cookies)}
        for name in which or list(steps):
            print(f"[{name}]")
            t0 = time.time()
            await steps[name]()
            print(f"  done in {time.time() - t0:.0f}s")
        await b.close()


asyncio.run(main(sys.argv[1:]))
