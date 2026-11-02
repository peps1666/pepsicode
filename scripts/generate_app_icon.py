"""Export the supplied pixel-terminal identity (optional dependency: Pillow).

python scripts/generate_app_icon.py
Geometry and colors live in pepsicode/branding.py; the original design is
preserved in assets/brand-reference.png. Packaging uses committed exports.
"""

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pepsicode.branding import BUBBLES, INK, LIME, PAPER, PIXELS

ICON_SIZE = 512
BANNER_SIZE = (880, 280)
FRAMES = 40


def cells(*, light=False, cursor=True):
    """Return square vector primitives for the dark and light brand variants."""
    if light:
        # Light-surface variant from the lower-left panel of the reference.
        for y, row in enumerate(PIXELS):
            for x, pixel in enumerate(row):
                if pixel != " ":
                    yield x, y, INK
        for y in range(7, 14):
            for x in range(2, 12):
                pixel = PIXELS[y][x]
                yield x, y, INK if pixel == "#" or (pixel == "_" and cursor) else LIME
    else:
        for y, row in enumerate(PIXELS):
            for x, pixel in enumerate(row):
                if pixel == "#" or (pixel == "_" and cursor):
                    yield x, y, LIME


def motion(phase, index):
    return 0.65 * (1 - math.cos(2 * math.pi * phase)) / 2, 0.75 + 0.25 * math.cos(2 * math.pi * (phase + index / 3))


def mark_svg(*, light=False, animated=False):
    color = INK if light else LIME
    parts = ['<g shape-rendering="crispEdges">']
    for x, y, fill in cells(light=light):
        css = ' class="cursor"' if PIXELS[y][x] == "_" else ""
        parts.append(f'<rect{css} x="{x}" y="{y}" width="1" height="1" fill="{fill}"/>')
    parts.append("</g>")
    for i, (x, y, r) in enumerate(BUBBLES):
        parts.append(
            f'<circle class="bubble b{i}" cx="{x}" cy="{y}" r="{r}" fill="none" stroke="{color}" stroke-width=".28"/>'
        )
    return "\n".join(parts)


def svg(*, banner=False, light=False, animated=False):
    width, height = BANNER_SIZE if banner else (512, 512)
    style = (
        """<style>
.bubble { animation: fizz 3.2s ease-in-out infinite; }
.b1 { animation-delay: -.4s; } .b2 { animation-delay: -.8s; }
.cursor { animation: blink 1.6s steps(1) infinite; }
@keyframes fizz { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-.65px); } }
@keyframes blink { 0%,75% { opacity: 1; } 76%,100% { opacity: 0; } }
@media (prefers-reduced-motion: reduce) { .bubble,.cursor { animation: none; } }
</style>"""
        if animated
        else ""
    )
    if banner:
        body = f'<rect width="880" height="280" rx="24" fill="{INK}"/>'
        body += f'<g transform="translate(100 30) scale(12)">{mark_svg(animated=animated)}</g>'
        body += f'<text x="330" y="177" font-family="Consolas,DejaVu Sans Mono,monospace" font-weight="700" font-size="66" fill="{PAPER}">pepsi<tspan fill="{LIME}">code</tspan></text>'
    else:
        body = "" if light else f'<rect width="512" height="512" rx="104" fill="{INK}"/>'
        body += f'<g transform="translate(88 40) scale(24)">{mark_svg(light=light)}</g>'
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="logo-title">\n<title id="logo-title">PepsiCode pixel terminal</title>\n{style}\n{body}\n</svg>\n'


def draw_mark(image, origin, unit, *, light=False, phase=None):
    draw = ImageDraw.Draw(image)
    ox, oy = origin
    cursor = phase is None or (phase * 2) % 1 < 0.8
    for x, y, fill in cells(light=light, cursor=cursor):
        draw.rectangle((ox + x * unit, oy + y * unit, ox + (x + 1) * unit - 1, oy + (y + 1) * unit - 1), fill=fill)
    for i, (x, y, r) in enumerate(BUBBLES):
        shift, opacity = (0, 1) if phase is None else motion(phase, i)
        color = INK if light else LIME
        if phase is not None:
            color = tuple(
                round(int(color[n : n + 2], 16) * opacity + int(INK[n : n + 2], 16) * (1 - opacity)) for n in (1, 3, 5)
            )
        y -= shift
        draw.ellipse(
            (ox + (x - r) * unit, oy + (y - r) * unit, ox + (x + r) * unit, oy + (y + r) * unit),
            outline=color,
            width=max(1, round(unit * 0.28)),
        )


def wordmark_font():
    for path in (
        "C:/Windows/Fonts/consolab.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/System/Library/Fonts/Menlo.ttc",
    ):
        if Path(path).is_file():
            return ImageFont.truetype(path, 66 * 2)
    raise RuntimeError("Install Consolas or DejaVu Sans Mono Bold to export the README wordmark.")


def render(*, banner=False, phase=None):
    scale = 2
    width, height = BANNER_SIZE if banner else (ICON_SIZE, ICON_SIZE)
    image = Image.new("RGBA", (width * scale, height * scale))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (0, 0, width * scale - 1, height * scale - 1), radius=(24 if banner else 104) * scale, fill=INK
    )
    draw_mark(image, (200, 60) if banner else (176, 80), (12 if banner else 24) * scale, phase=phase)
    if banner:
        font = wordmark_font()
        x, y = 330 * scale, 177 * scale
        draw.text((x, y), "pepsi", font=font, fill=PAPER, anchor="ls")
        draw.text((x + draw.textlength("pepsi", font=font), y), "code", font=font, fill=LIME, anchor="ls")
    return image.resize((width, height), Image.Resampling.LANCZOS)


def main():
    public, desktop, assets = ROOT / "pepsicode-client/public", ROOT / "pepsicode-client/assets", ROOT / "assets"
    for directory in (public, desktop, assets):
        directory.mkdir(exist_ok=True)
    (public / "app-icon.svg").write_text(svg(), encoding="utf-8")
    (public / "brand-mark.svg").write_text(svg(light=True), encoding="utf-8")
    (assets / "logo.svg").write_text(svg(banner=True, animated=True), encoding="utf-8")
    icon = render()
    icon.save(public / "app-icon.png")
    icon.save(desktop / "app-icon.ico", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    render(banner=True).save(assets / "logo.png")
    frames = []
    for n in range(FRAMES):
        frame = render(banner=True, phase=n / FRAMES)
        # GIF has one-bit transparency: composite to the brand's dark surface.
        background = Image.new("RGB", frame.size, INK)
        background.paste(frame, mask=frame.getchannel("A"))
        frames.append(background)
    frames[0].save(assets / "logo.gif", save_all=True, append_images=frames[1:], duration=80, loop=0, disposal=2)


if __name__ == "__main__":
    main()
