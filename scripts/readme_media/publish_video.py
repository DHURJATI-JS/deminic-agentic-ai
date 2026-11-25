"""Copy the recorded MP4s + AI captions to static/video for the home page and make a poster frame for each."""
import shutil
import subprocess
from pathlib import Path

import imageio_ffmpeg
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
MEDIA = ROOT / "docs" / "media"
VIDEO = ROOT / "static" / "video"
VIDEO.mkdir(parents=True, exist_ok=True)
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
POSTER_AT = {"hero": 0.35, "chat": 0.9, "business": 0.55, "coding": 0.6, "status": 0.85}

for key, at in POSTER_AT.items():
    mp4 = MEDIA / f"{key}.mp4"
    if not mp4.exists():
        print("skip (not recorded):", key)
        continue
    shutil.copy2(mp4, VIDEO / f"{key}.mp4")
    _, secs = imageio_ffmpeg.count_frames_and_secs(str(mp4))
    png = VIDEO / f"_{key}.png"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{secs * at:.2f}", "-i", str(mp4), "-frames:v", "1", str(png)],
                   check=True)
    Image.open(png).convert("RGB").save(VIDEO / f"{key}.webp", "WEBP", quality=82, method=6)
    png.unlink()
    print(f"{key}: {secs:.1f}s, mp4 {(VIDEO / f'{key}.mp4').stat().st_size // 1024} KB, poster {(VIDEO / f'{key}.webp').stat().st_size // 1024} KB")
shutil.copy2(MEDIA / "captions.json", VIDEO / "captions.json")
print("captions copied")
