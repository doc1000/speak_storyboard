"""
One-shot PWA icon generator. Run once during Docker build (see Dockerfile).
Requires Pillow >= 10.0.0 (already in requirements.txt).
"""
import os
from PIL import Image, ImageDraw, ImageFont

os.makedirs("static", exist_ok=True)


def make_icon(size: int, path: str) -> None:
    img = Image.new("RGB", (size, size), "#1a1510")
    draw = ImageDraw.Draw(img)
    text = "SP"
    font = ImageFont.load_default(size=size // 3)
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - w) / 2, (size - h) / 2), text, fill="#b89a6b", font=font)
    img.save(path)
    print(f"[create_icons] Generated {path}")


make_icon(192, "static/icon-192.png")
make_icon(512, "static/icon-512.png")
make_icon(180, "static/apple-touch-icon.png")
