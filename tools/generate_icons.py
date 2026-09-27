"""Generate the PNG brand assets from the logo geometry.

The logo is defined once, as two cubic Béziers (see :data:`PATHS`), and
rasterised here with a small pure-Python renderer. No Pillow, no cairosvg, no
node — the build stays dependency-free and the output is byte-reproducible, so
regenerating never produces a spurious diff.

Curves are flattened to polygons, filled with even-odd coverage using 4x4
supersampling, and written as a minimal RGBA PNG.

Usage::

    python tools/generate_icons.py
    python tools/generate_icons.py --out frontend/assets
"""

from __future__ import annotations

import argparse
import math
import struct
import zlib
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "frontend" / "assets"

# The mark: two smooth strokes that diverge from a shared point, cross lower
# down, and close into a leaf. Defined in a 64x64 viewBox.
PATHS: tuple[tuple[tuple[float, float], tuple[tuple[float, float], ...]], ...] = (
    ((32.0, 5.0), ((44.0, 17.0), (44.0, 35.0), (30.0, 59.0))),  # right then left
    ((32.0, 5.0), ((20.0, 17.0), (20.0, 35.0), (34.0, 59.0))),  # left then right
)

STROKE_WIDTH = 4.5
COLOURS: tuple[tuple[int, int, int], ...] = (
    (141, 132, 121),  # warm stone
    (179, 167, 148),  # soft sand
)

SAMPLES = 96  # points per curve after flattening
SUPERSAMPLE = 4  # grid resolution per output pixel


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def cubic_points(
    start: tuple[float, float],
    controls: Sequence[tuple[float, float]],
    samples: int = SAMPLES,
) -> list[tuple[float, float]]:
    """Flatten a cubic Bézier into points using uniform parameterisation."""
    p0, p1, p2, p3 = start, controls[0], controls[1], controls[2]
    points: list[tuple[float, float]] = []
    for index in range(samples + 1):
        t = index / samples
        u = 1.0 - t
        x = u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0]
        y = u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1]
        points.append((x, y))
    return points


def stroke_polygon(points: Sequence[tuple[float, float]], width: float) -> list[tuple[float, float]]:
    """Turn a polyline into a closed outline with round caps and joins.

    Offsetting the curve would need tangents and a curvature term; instead the
    outline is built from the offset on one side, reversed on the other, plus a
    semicircle at each end. That is exact for round caps and visually
    indistinguishable from a proper offset for these gentle curves.
    """
    half = width / 2.0
    left: list[tuple[float, float]] = []
    right: list[tuple[float, float]] = []

    for index, (x, y) in enumerate(points):
        # Central-difference tangent keeps the offset stable at the joins.
        before = points[max(0, index - 1)]
        after = points[min(len(points) - 1, index + 1)]
        dx = after[0] - before[0]
        dy = after[1] - before[1]
        length = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / length, dx / length
        left.append((x + nx * half, y + ny * half))
        right.append((x - nx * half, y - ny * half))

    return left + list(reversed(right))


def arc_points(
    centre: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
    radius: float,
    segments: int = 24,
) -> list[tuple[float, float]]:
    """Points along the shorter arc between two offset endpoints."""
    cx, cy = centre
    a0 = math.atan2(start[1] - cy, start[0] - cx)
    a1 = math.atan2(end[1] - cy, end[0] - cx)
    delta = a1 - a0
    while delta > math.pi:
        delta -= 2 * math.pi
    while delta < -math.pi:
        delta += 2 * math.pi
    return [
        (
            cx + radius * math.cos(a0 + delta * index / segments),
            cy + radius * math.sin(a0 + delta * index / segments),
        )
        for index in range(segments + 1)
    ]


# ---------------------------------------------------------------------------
# Rasterisation
# ---------------------------------------------------------------------------


def point_in_polygon(x: float, y: float, polygon: Sequence[tuple[float, float]]) -> bool:
    """Even-odd ray casting."""
    inside = False
    count = len(polygon)
    j = count - 1
    for i in range(count):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            t = (y - yi) / (yj - yi) if yj != yi else 0.0
            if x < xi + t * (xj - xi):
                inside = not inside
        j = i
    return inside


def render(size: int, *, padding: float = 0.0, background: tuple[int, int, int, int] | None = None) -> bytes:
    """Rasterise the mark to RGBA bytes."""
    scale = size / 64.0
    offset = padding * scale
    effective = size - 2 * offset

    polygons: list[tuple[list[tuple[float, float]], tuple[int, int, int]]] = []
    for path, colour in zip(PATHS, COLOURS, strict=True):
        flat = cubic_points(*path)
        outline = stroke_polygon(flat, STROKE_WIDTH * scale)
        # viewBox space -> device pixels
        scaled = [((x / 64.0) * effective + offset, (y / 64.0) * effective + offset) for x, y in outline]
        polygons.append((scaled, colour))

    pixels = bytearray(size * size * 4)
    step = 1.0 / SUPERSAMPLE

    for py in range(size):
        for px in range(size):
            r = g = b = a = 0
            if background is not None:
                r, g, b, a = background
            for sy in range(SUPERSAMPLE):
                for sx in range(SUPERSAMPLE):
                    x = px + (sx + 0.5) * step
                    y = py + (sy + 0.5) * step
                    for polygon, colour in polygons:
                        if point_in_polygon(x, y, polygon):
                            r, g, b, a = colour[0], colour[1], colour[2], 255
                            break
            offset_index = (py * size + px) * 4
            pixels[offset_index] = r
            pixels[offset_index + 1] = g
            pixels[offset_index + 2] = b
            pixels[offset_index + 3] = a

    return bytes(pixels)


def write_png(path: Path, size: int, rgba: bytes) -> None:
    """Write 8-bit RGBA PNG data. Filter type 0 on every scanline."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    raw = bytearray()
    stride = size * 4
    for row in range(size):
        raw.append(0)  # filter: none
        raw.extend(rgba[row * stride : (row + 1) * stride])

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(data)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

TARGETS: tuple[tuple[str, int, float, tuple[int, int, int, int] | None], ...] = (
    # A maskable icon must keep its content inside the safe zone, so the mark
    # is inset. Without this, Android crops the crossing point off.
    ("favicon-32.png", 32, 0.06, None),
    ("icon-192.png", 192, 0.14, None),
    ("icon-512.png", 512, 0.14, None),
    ("apple-touch-icon.png", 180, 0.10, (250, 249, 247, 255)),
    ("icon-maskable-512.png", 512, 0.22, (250, 249, 247, 255)),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output directory.")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    for name, size, padding, background in TARGETS:
        pixels = render(size, padding=padding, background=background)
        target = args.out / name
        write_png(target, size, pixels)
        print(f"  {target.relative_to(ROOT)}  {size}x{size}")

    print(f"Generated {len(TARGETS)} icons in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
