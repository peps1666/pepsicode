r"""Generate vector/Windows icons from the terminal mascot /\_/\, ( o.o ), > ^ <.

Run with Pillow installed; generated assets are committed, so packaging needs no image tools.
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1] / "pepsicode-client"


def main():
    scale = 4
    image = Image.new("RGBA", (512 * scale, 512 * scale))
    draw = ImageDraw.Draw(image)
    svg = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">',
        '<rect width="512" height="512" rx="112" fill="#292925"/>',
    ]
    draw.rounded_rectangle((0, 0, 512 * scale - 1, 512 * scale - 1), radius=112 * scale, fill="#292925")

    def line(points, width=18):
        svg.append(
            '<polyline points="'
            + " ".join(f"{x},{y}" for x, y in points)
            + f'" fill="none" stroke="#ffd7af" stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"/>'
        )
        draw.line([(x * scale, y * scale) for x, y in points], fill="#ffd7af", width=width * scale, joint="curve")
        for x, y in points:
            r = width * scale / 2
            draw.ellipse((x * scale - r, y * scale - r, x * scale + r, y * scale + r), fill="#ffd7af")

    # The same pointed ears, round eyes and whiskers as the three-line terminal cat.
    line([(137, 208), (151, 114), (221, 176), (291, 176), (361, 114), (375, 208)])
    line([(117, 227), (100, 247), (95, 274), (100, 302), (117, 323)])
    line([(395, 227), (412, 247), (417, 274), (412, 302), (395, 323)])
    for x in (201, 311):
        svg.append(f'<circle cx="{x}" cy="264" r="17" fill="#ffd7af"/>')
        draw.ellipse(((x - 17) * scale, 247 * scale, (x + 17) * scale, 281 * scale), fill="#ffd7af")
    svg.append('<circle cx="256" cy="290" r="8" fill="#ffd7af"/>')
    draw.ellipse((248 * scale, 282 * scale, 264 * scale, 298 * scale), fill="#ffd7af")
    line([(165, 352), (191, 369), (165, 386)], 13)
    line([(347, 352), (321, 369), (347, 386)], 13)
    line([(240, 379), (256, 361), (272, 379)], 13)
    svg.append("</svg>")
    public = ROOT / "public"
    assets = ROOT / "assets"
    public.mkdir(exist_ok=True)
    assets.mkdir(exist_ok=True)
    (public / "app-icon.svg").write_text("\n".join(svg) + "\n", encoding="utf-8")
    image = image.resize((512, 512), Image.Resampling.LANCZOS)
    image.save(public / "app-icon.png")
    image.save(assets / "app-icon.ico", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])


if __name__ == "__main__":
    main()
