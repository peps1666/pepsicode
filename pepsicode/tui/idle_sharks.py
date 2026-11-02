"""One ASCII shark swimming across the empty terminal home screen.

Top-down shark, as in ascii-sharks: the classic outline, pectoral and pelvic
fins, forked tail, dorsal ridge, eye and gill slits are rasterised every frame
from the same profile maths, then the body wave and the fin bank are applied.
Nothing is pre-baked, so the shark keeps its shape at any terminal size and
every frame is a real pose. Half blocks give two vertical samples per cell, so
a 1:2 terminal cell does not squash the fish.

Shape proportions follow ascii-sharks (src/renderer.ts, src/world.ts),
Copyright (c) 2026 drewdi, MIT License:

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import math
from dataclasses import dataclass

from .chrome import RESET, string_display_width, strip_ansi
from .theme import CYAN

FRAME_INTERVAL = 0.1
MIN_WIDTH = 50
MIN_HEIGHT = 16
MIN_VISIBLE_ROWS = 8
MAX_CELLS = 120_000

# The renderer works in virtual pixels: one column wide, half a row tall, so
# the shark keeps its proportions in both axes while the cells stay 1:2.
ROW_UNITS = 2.0
_NODES = 24
_MAX_BEND = 0.11
_HISTORY_STEP = 0.05
_MAX_HISTORY = 640

_BLOCK_TOP = "▀"
_BLOCK_BOTTOM = "▄"
_BODY_TINT = (0.94, 0.98, 1.08)
_QUANT = 13
_MAX_LEVEL = 250

# Classic top-down furniture, as fractions of the body length.
_PECTORAL = (0.15, 0.43, 0.55, 0.26)
_PELVIC = (0.57, 0.65, 0.69, 0.085)
_CAUDAL = 0.085
_DORSAL = (0.33, 0.47)
_SECOND_DORSAL = (0.71, 0.76)
_EYE = (0.10, 0.14)
_GILLS = (0.19, 0.29)


def can_render(width: int, height: int) -> bool:
    return width >= MIN_WIDTH and height >= MIN_HEIGHT and width * height <= MAX_CELLS


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    t = _clamp((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _half_width(s: float) -> float:
    """Classic top-down half-width at s ∈ [0, 1] (snout → tail), in lengths."""
    if s < 0.13:
        return 0.09 * math.pow(math.sin((s / 0.13) * math.pi / 2), 0.95)
    if s < 0.3:
        return 0.09 + 0.016 * _smoothstep(0.13, 0.3, s)
    if s < 0.84:
        return 0.019 + 0.087 * math.pow(1 - (s - 0.3) / 0.54, 1.25)
    k = (s - 0.84) / 0.16
    return 0.019 * (1 - k) + 0.012 * math.sin(k * math.pi)


def _length(width: int, rows: int) -> float:
    """Body length in column units: big enough to read, small enough to turn."""
    return _clamp(min(width * 0.33, rows * 1.45), 16.0, 42.0)


def _swim_path(t: float, width: float, height: float) -> tuple[float, float]:
    """A slow oval patrol: long straights, wide turns at either end.

    Round and eight-shaped paths bend the body the whole time on a screen this
    short; an oval lets the shark glide straight across the water between turns,
    which is the pose the top-down outline reads best in.
    """
    cx = width * 0.5 + width * 0.04 * math.sin(t * 0.013 + 0.5)
    cy = height * 0.5 + height * 0.03 * math.sin(t * 0.017 - 1.1)
    ry = height * 0.24 * (1.0 + 0.08 * math.sin(t * 0.027 + 1.7))
    hx = width * 0.20 * (1.0 + 0.07 * math.sin(t * 0.023 + 0.9))
    straight = 2.0 * hx
    turn = math.pi * ry
    lap = 2.0 * (straight + turn)
    travelled = 6.2 * t + 2.4 * math.sin(t * 0.07)
    p = travelled % lap
    if p < straight:
        return cx - hx + p, cy - ry
    p -= straight
    if p < turn:
        angle = -math.pi / 2 + p / ry
        return cx + hx + ry * math.cos(angle), cy + ry * math.sin(angle)
    p -= turn
    if p < straight:
        return cx + hx - p, cy + ry
    p -= straight
    angle = math.pi / 2 + p / ry
    return cx - hx + ry * math.cos(angle), cy + ry * math.sin(angle)


def _body_nodes(t: float, width: float, height: float, length: float) -> list[tuple[float, float]]:
    """Spine, snout first: a stiff body trailing the snout through the water.

    Each joint takes the direction of the water one segment back but may only
    bend so far, so a tightening turn swings the tail wide instead of folding
    the shark into a banana.
    """
    seg = length / (_NODES - 1)
    px, py = _swim_path(t, width, height)
    when = t
    arcs = [0.0]
    angles = [0.0]
    total = 0.0
    need = seg * (_NODES - 1) * 1.05
    while total < need and len(arcs) < _MAX_HISTORY:
        when -= _HISTORY_STEP
        x, y = _swim_path(when, width, height)
        step = math.hypot(x - px, y - py)
        if step < 1e-6:
            continue
        total += step
        arcs.append(total)
        angles.append(math.atan2(y - py, x - px))
        px, py = x, y

    nodes: list[tuple[float, float]] = [_swim_path(t, width, height)]
    angle = angles[1] if len(angles) > 1 else 0.0
    index = 0
    for k in range(1, _NODES):
        target = k * seg
        while index + 1 < len(arcs) - 1 and arcs[index + 1] < target:
            index += 1
        wanted = angles[index]
        delta = math.atan2(math.sin(wanted - angle), math.cos(wanted - angle))
        angle += _clamp(delta, -_MAX_BEND, _MAX_BEND)
        nodes.append((nodes[-1][0] + math.cos(angle) * seg, nodes[-1][1] + math.sin(angle) * seg))
    return nodes


def _pose(t: float, width: float, height: float) -> tuple[list[tuple[float, float]], dict]:
    """Spine with the tail wave applied, plus the scalars the renderer needs."""
    length = _length(int(width), int(round(height / ROW_UNITS)))
    nodes = _body_nodes(t, width, height, length)

    x0, y0 = _swim_path(t, width, height)
    x1, y1 = _swim_path(t - 0.1, width, height)
    speed = math.hypot(x0 - x1, y0 - y1) / 0.1
    heading = math.atan2(y0 - y1, x0 - x1)
    turned = math.atan2(
        y1 - _swim_path(t - 0.45, width, height)[1],
        x1 - _swim_path(t - 0.45, width, height)[0],
    )
    turn = math.atan2(math.sin(heading - turned), math.cos(heading - turned)) / 0.35
    phase = t * (2.2 + speed * 0.10)
    swim = _clamp(speed / 4.5, 0.55, 1.3)

    waved: list[tuple[float, float]] = []
    count = len(nodes)
    for k, (px, py) in enumerate(nodes):
        s = k / (count - 1)
        a = max(0, k - 1)
        b = min(count - 1, k + 1)
        tx = nodes[a][0] - nodes[b][0]
        ty = nodes[a][1] - nodes[b][1]
        norm = math.hypot(tx, ty) or 1.0
        tx /= norm
        ty /= norm
        amplitude = length * 0.055 * (0.1 + 0.9 * math.pow(s, 1.8)) * swim
        offset = amplitude * math.sin(phase - s * 5.2)
        waved.append((px - ty * offset, py + tx * offset))

    bank = _clamp(turn * 0.45, -0.35, 0.35)
    flex = 1.0 + 0.06 * math.sin(phase * 0.5)
    return waved, {"length": length, "speed": speed, "phase": phase, "bank": bank, "flex": flex}


def _in_triangle(px, py, a, b, c) -> bool:
    ax, ay = a
    bx, by = b
    cx, cy = c
    d1 = (px - bx) * (ay - by) - (ax - bx) * (py - by)
    d2 = (px - cx) * (by - cy) - (bx - cx) * (py - cy)
    d3 = (px - ax) * (cy - ay) - (cx - ax) * (py - ay)
    neg = d1 < 0 or d2 < 0 or d3 < 0
    pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (neg and pos)


def _fill_triangle(marks, width: int, sub_rows: int, a, b, c, notch=None) -> None:
    xs = (a[0], b[0], c[0])
    ys = (a[1], b[1], c[1])
    c0 = max(0, int(math.floor(min(xs))))
    c1 = min(width - 1, int(math.ceil(max(xs))))
    r0 = max(0, int(math.floor(min(ys))))
    r1 = min(sub_rows - 1, int(math.ceil(max(ys))))
    for row in range(r0, r1 + 1):
        py = row + 0.5
        for col in range(c0, c1 + 1):
            if not _in_triangle(col + 0.5, py, a, b, c):
                continue
            if notch is not None and _in_triangle(col + 0.5, py, *notch):
                continue
            key = (row, col)
            if key not in marks:
                marks[key] = (1, 0.0, 0.0, 0.0, 0.0, 0.0)


def _rasterize(spine, width: int, sub_rows: int, info: dict) -> dict:
    """Sweep the body, then stamp the fins; every hit records its pose."""
    marks: dict[tuple[int, int], tuple] = {}
    length = info["length"]
    count = len(spine)

    centers = []
    normals = []
    widths = []
    for k, (px, py) in enumerate(spine):
        a = max(0, k - 1)
        b = min(count - 1, k + 1)
        tx = spine[b][0] - spine[a][0]
        ty = spine[b][1] - spine[a][1]
        norm = math.hypot(tx, ty) or 1.0
        s = k / (count - 1)
        centers.append((px, py, s))
        normals.append((-ty / norm, tx / norm))
        widths.append(_half_width(s) * length)

    for k in range(count - 1):
        ax, ay, sa = centers[k]
        bx, by, sb = centers[k + 1]
        wa = widths[k]
        wb = widths[k + 1]
        nax, nay = normals[k]
        nbx, nby = normals[k + 1]
        for j in range(3):
            f = j / 3.0
            px = ax + (bx - ax) * f
            py = ay + (by - ay) * f
            s = sa + (sb - sa) * f
            w = wa + (wb - wa) * f
            nx = nax + (nbx - nax) * f
            ny = nay + (nby - nay) * f
            norm = math.hypot(nx, ny) or 1.0
            nx /= norm
            ny /= norm
            if w < 0.3:
                w = 0.3
            v = -w
            while v <= w:
                gx = px + nx * v
                gy = py + ny * v
                if 0 <= gy < sub_rows and 0 <= gx < width:
                    key = (int(gy), int(gx))
                    margin = w - abs(v)
                    current = marks.get(key)
                    if current is None or current[0] < 2 or (current[0] == 2 and margin > current[1]):
                        marks[key] = (2, margin, s, v, nx, ny)
                v += 0.5

    # Pectoral fins: rigid wings on the shoulder, swept back and out along the
    # body normal, with the falcate notch the reference cuts from the trailing
    # edge.
    for side in (1.0, -1.0):
        s0, s1, _s2, reach = _PECTORAL
        tip_len = length * reach * info["flex"] * (1.0 - side * info["bank"])
        k0 = min(count - 2, int(round(s0 * (count - 1))))
        k1 = min(count - 2, int(round(s1 * (count - 1))))
        p0 = spine[k0]
        p1 = spine[k1]
        bx = p1[0] - p0[0]
        by = p1[1] - p0[1]
        blen = math.hypot(bx, by) or 1.0
        nx, ny = normals[k1]
        apex = (
            p1[0] + bx / blen * length * 0.12 + nx * tip_len * side,
            p1[1] + by / blen * length * 0.12 + ny * tip_len * side,
        )
        mid = (
            p1[0] + (apex[0] - p1[0]) * 0.45 - nx * tip_len * side * 0.16,
            p1[1] + (apex[1] - p1[1]) * 0.45 - ny * tip_len * side * 0.16,
        )
        _fill_triangle(marks, width, sub_rows, p0, p1, apex, (p1, mid, apex))

    # Pelvic fins and the caudal fork: small flat triangles near the tail.
    for side in (1.0, -1.0):
        s0, s1, _s2, reach = _PELVIC
        k0 = min(count - 2, int(round(s0 * (count - 1))))
        k1 = min(count - 2, int(round(s1 * (count - 1))))
        p0 = spine[k0]
        p1 = spine[k1]
        bx = p1[0] - p0[0]
        by = p1[1] - p0[1]
        blen = math.hypot(bx, by) or 1.0
        nx, ny = normals[k1]
        apex = (
            p1[0] + bx / blen * length * 0.02 + nx * length * reach * side,
            p1[1] + by / blen * length * 0.02 + ny * length * reach * side,
        )
        _fill_triangle(marks, width, sub_rows, p0, p1, apex)

        k3 = min(count - 2, int(round(0.86 * (count - 1))))
        k4 = min(count - 2, int(round(0.95 * (count - 1))))
        nx, ny = normals[count - 1]
        tail = (
            spine[count - 1][0] + nx * length * _CAUDAL * side,
            spine[count - 1][1] + ny * length * _CAUDAL * side,
        )
        _fill_triangle(marks, width, sub_rows, spine[k3], spine[k4], tail)
    return marks


def _body_level(s: float, v: float, w: float, nx: float, ny: float, margin: float) -> int:
    """Lit skin: bright along the back, falling off toward the flanks."""
    across = 1.0 if w <= 1e-6 else min(1.0, abs(v) / w)
    side = 1.0 if v >= 0.0 else -1.0
    facing = (nx * side * -0.6 + ny * side * -0.8) * across
    shade = _clamp(0.60 - across * across * 0.50 + facing * 0.30, 0.0, 1.0)

    if _EYE[0] <= s <= _EYE[1] and across > 0.5:
        return 14
    if _GILLS[0] <= s <= _GILLS[1] and across > 0.46:
        if int((s - _GILLS[0]) / (_GILLS[1] - _GILLS[0]) * 5) % 2 == 0:
            return 30
    if _DORSAL[0] <= s <= _DORSAL[1] and across < 0.2:
        return 158
    if _SECOND_DORSAL[0] <= s <= _SECOND_DORSAL[1] and across < 0.36:
        shade = max(shade, 0.84)

    coverage = _clamp(margin / 0.8, 0.0, 1.0)
    level = 16 + 140 * math.pow(shade, 0.9)
    return max(4, int(level * (0.32 + 0.68 * coverage)))


def _level_rgb(level: int) -> tuple[int, int, int]:
    return (
        min(255, int(level * _BODY_TINT[0])),
        min(255, int(level * _BODY_TINT[1])),
        min(255, int(level * _BODY_TINT[2])),
    )


_STYLE_CACHE: dict[tuple[int, int], str] = {}


def _colors_enabled() -> bool:
    # The palette in theme.py is empty when the terminal cannot do colour.
    return bool(CYAN)


def _ansi(top: int, bottom: int) -> str:
    key = (top, bottom)
    cached = _STYLE_CACHE.get(key)
    if cached is not None:
        return cached
    if not _colors_enabled():
        text = ""
    else:
        top_rgb = _level_rgb(top * _QUANT)
        text = f"{RESET}\x1b[38;2;{top_rgb[0]};{top_rgb[1]};{top_rgb[2]}m"
        if bottom < 0:
            text += "\x1b[49m"
        else:
            bottom_rgb = _level_rgb(bottom * _QUANT)
            text += f"\x1b[48;2;{bottom_rgb[0]};{bottom_rgb[1]};{bottom_rgb[2]}m"
    _STYLE_CACHE[key] = text
    return text


def _quantize(level: int) -> int:
    return max(0, min(_MAX_LEVEL // _QUANT, level // _QUANT))


def _cells(width: int, height: int, elapsed: float) -> list[list[tuple[str, str]]]:
    """Ocean layer for the whole viewport: half-block cells, (" ", "") for water."""
    if not can_render(width, height):
        return []
    sub_rows = height * 2
    spine, info = _pose(max(0.0, elapsed), float(width), height * ROW_UNITS)
    marks = _rasterize(spine, width, sub_rows, info)
    if not marks:
        return []

    length = info["length"]
    levels: dict[tuple[int, int], int] = {}
    for (row, col), hit in marks.items():
        if hit[0] == 1:
            levels[(row, col)] = 64
            continue
        _, margin, s, v, nx, ny = hit
        levels[(row, col)] = _body_level(s, v, _half_width(s) * length, nx, ny, margin)

    grid: list[list[tuple[str, str]]] = [[(" ", "")] * width for _ in range(height)]
    for row in range(height):
        top_row = row * 2
        for col in range(width):
            top = levels.get((top_row, col))
            bottom = levels.get((top_row + 1, col))
            if top is None and bottom is None:
                continue
            if top is not None and bottom is not None:
                grid[row][col] = _BLOCK_TOP, _ansi(_quantize(top), _quantize(bottom))
            elif top is not None:
                grid[row][col] = _BLOCK_TOP, _ansi(_quantize(top), -1)
            else:
                grid[row][col] = _BLOCK_BOTTOM, _ansi(_quantize(bottom), -1)
    return grid


def render_ocean(width: int, height: int, elapsed: float) -> list[str]:
    """Full viewport glyph layer, before foreground text and input are applied."""
    return ["".join(char for char, _ in row) for row in _cells(width, height, elapsed)]


def _paint(cells: list[tuple[str, str]]) -> str:
    parts = []
    previous = ""
    for char, style in cells:
        if style != previous:
            if style:
                parts.append(style)
            elif previous:
                parts.append(RESET)
            previous = style
        parts.append(char)
    if previous:
        parts.append(RESET)
    return "".join(parts)


def compose_ocean(
    foreground: list[str],
    width: int,
    height: int,
    elapsed: float,
    protected_rows: set[int],
) -> list[str]:
    """Text spans are opaque; composer/status rows are opaque across their width."""
    visible = min(protected_rows) if protected_rows else height
    visible = max(0, min(height, visible))
    if visible < MIN_VISIBLE_ROWS:
        return foreground
    grid = _cells(width, visible, elapsed)
    if not grid:
        return foreground
    result: list[str] = []
    for y in range(height):
        text = foreground[y] if y < len(foreground) else ""
        if y >= visible or y in protected_rows:
            result.append(text)
        elif strip_ansi(text).strip():
            # Keep foreground ANSI, spaces between words, cursor and wide glyphs
            # intact. The background never writes into any part of this span.
            end = min(width, string_display_width(text))
            result.append(text + RESET + _paint(grid[y][end:]))
        else:
            result.append(_paint(grid[y]))
    return result


@dataclass
class IdleSharks:
    """UI-only clock, polled by the existing input loop; no worker or timer."""

    started_at: float | None = None
    last_frame_at: float | None = None

    def due(self, now: float) -> bool:
        return self.last_frame_at is None or now - self.last_frame_at >= FRAME_INTERVAL

    def _elapsed(self, now: float) -> float:
        if self.started_at is None:
            self.started_at = now
        self.last_frame_at = now
        return now - self.started_at

    def render(self, width: int, height: int, now: float) -> list[str]:
        return render_ocean(width, height, self._elapsed(now))

    def compose(self, frame: list[str], width: int, height: int, now: float, protected_rows: set[int]) -> list[str]:
        return compose_ocean(frame, width, height, self._elapsed(now), protected_rows)
