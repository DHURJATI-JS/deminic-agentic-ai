"""README video thumbnails: poster frame + dark vignette + glassy play button + title/duration pill."""
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
MEDIA = ROOT / "docs" / "media"
VIDEO = ROOT / "static" / "video"
CLIPS = {"chat": "Live AI chat", "business": "Business desk", "coding": "Coding studio",
         "status": "Live system status", "hero": "Immersive VR mode"}
W, H = 960, 600


def font(size, bold=True):
    for name in (("segoeuib.ttf" if bold else "segoeui.ttf"), "arialbd.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


for key, title in CLIPS.items():
    _, secs = imageio_ffmpeg.count_frames_and_secs(str(MEDIA / f"{key}.mp4"))
    im = Image.open(VIDEO / f"{key}.webp").convert("RGB").resize((W, H), Image.Resampling.LANCZOS)
    # vignette + bottom gradient so the overlay reads on any frame
    shade = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(shade)
    for y in range(H):
        d.line([(0, y), (W, y)], fill=int(40 + 150 * max(0, (y - H * 0.45) / (H * 0.55)) ** 1.6))
    im = Image.composite(Image.new("RGB", (W, H), (4, 8, 22)), im, shade)
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    o = ImageDraw.Draw(ov)
    cx, cy, r = W // 2, H // 2 - 10, 62
    # glow + glass disc + play triangle
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([cx - r - 26, cy - r - 26, cx + r + 26, cy + r + 26], fill=(80, 140, 255, 120))
    ov = Image.alpha_composite(ov, glow.filter(ImageFilter.GaussianBlur(22)))
    o = ImageDraw.Draw(ov)
    o.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 235))
    o.polygon([(cx - 18, cy - 30), (cx - 18, cy + 30), (cx + 34, cy)], fill=(10, 132, 255, 255))
    # title + duration pill
    ft, fd = font(34), font(24, bold=False)
    o.text((36, H - 92), title, font=ft, fill=(255, 255, 255, 255))
    dur = f"{int(secs // 60)}:{int(round(secs % 60)):02d}  \u00b7  real recording"
    o.text((38, H - 48), dur, font=fd, fill=(205, 215, 235, 255))
    out = Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB")
    out.save(MEDIA / f"{key}-play.webp", "WEBP", quality=86, method=6)
    print(key, f"{secs:.1f}s", (MEDIA / f"{key}-play.webp").stat().st_size // 1024, "KB")
