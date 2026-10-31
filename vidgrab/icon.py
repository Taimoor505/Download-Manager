"""The vidgrab icon, drawn in code so the tray, window, .exe and extension share one design."""

from pathlib import Path

ACCENT = (124, 58, 237, 255)
WHITE = (255, 255, 255, 255)


def draw(size: int = 256):
    """A violet rounded square with a white download arrow."""
    from PIL import Image, ImageDraw

    scale = 4  # draw large, then downsample for smooth edges
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.22), fill=ACCENT)
    stem_w = s * 0.13
    cx = s / 2
    d.rectangle((cx - stem_w / 2, s * 0.2, cx + stem_w / 2, s * 0.55), fill=WHITE)
    d.polygon([(s * 0.28, s * 0.5), (s * 0.72, s * 0.5), (cx, s * 0.74)], fill=WHITE)
    d.rounded_rectangle((s * 0.24, s * 0.79, s * 0.76, s * 0.86), radius=int(s * 0.03), fill=WHITE)
    return img.resize((size, size), Image.LANCZOS)


def write_assets(root: Path) -> None:
    """Writes the extension PNGs and the Windows .ico used by the desktop build."""
    for px in (16, 48, 128):
        out = root / "extension" / "icons" / f"{px}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        draw(px).save(out)
    draw(256).save(root / "packaging" / "vidgrab.ico", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])


if __name__ == "__main__":
    write_assets(Path(__file__).resolve().parents[1])
