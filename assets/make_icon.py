"""Generate the application icon.

Kept as a script rather than a committed binary blob so the icon is
reviewable, tweakable, and reproducible. Writes a multi-resolution .ico
(Windows picks the size it needs per context) plus a PNG for other uses.

Run with:  python assets/make_icon.py

Deliberately dependency-free: PNG and ICO are both simple enough to emit by
hand, and adding Pillow just to draw a waveform would be a poor trade.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ASSETS = Path(__file__).resolve().parent

#: Matches the accent colour in ui/theme.py.
ACCENT = (0x5B, 0x8D, 0xEE)
ACCENT_LIGHT = (0x8F, 0xB4, 0xF5)
BACKGROUND = (0x18, 0x1B, 0x23)

#: Sizes Windows actually asks for.
SIZES = (16, 32, 48, 64, 128, 256)


def _draw(size: int) -> bytearray:
    """Render the icon as RGBA rows: headphones inside a rounded square."""
    pixels = bytearray(size * size * 4)
    radius = size * 0.22
    centre = size / 2.0

    #: The headband and cups are sized up a bit at small resolutions, so the
    #: shape stays legible instead of thinning into a blur at 16-32px.
    small = size <= 32
    band_r = size * 0.32
    band_thickness = size * (0.17 if small else 0.11)
    band_cy = size * 0.44
    cup_cy = band_cy + size * (0.12 if small else 0.16)
    cup_rx = size * (0.15 if small else 0.13)
    cup_ry = size * (0.19 if small else 0.17)
    cup_offset = band_r

    for y in range(size):
        for x in range(size):
            index = (y * size + x) * 4

            # -- rounded-square background ------------------------------
            dx = max(radius - x, x - (size - radius), 0)
            dy = max(radius - y, y - (size - radius), 0)
            inside = math.hypot(dx, dy) <= radius
            if not inside:
                continue

            # A soft vertical gradient stops it looking flat.
            t = y / max(1, size - 1)
            r = int(BACKGROUND[0] + 14 * (1 - t))
            g = int(BACKGROUND[1] + 16 * (1 - t))
            b = int(BACKGROUND[2] + 22 * (1 - t))
            pixels[index:index + 4] = bytes((r, g, b, 255))

            # -- headband: the top arc of a ring -------------------------
            band_dist = math.hypot(x - centre, y - band_cy)
            on_band = (
                band_r - band_thickness <= band_dist <= band_r
                and y <= band_cy + band_thickness * 0.3
            )

            # -- ear cups: rounded ovals hanging off the band's ends -----
            left_dx = (x - (centre - cup_offset)) / cup_rx
            left_dy = (y - cup_cy) / cup_ry
            left_in = left_dx * left_dx + left_dy * left_dy <= 1.0
            right_dx = (x - (centre + cup_offset)) / cup_rx
            right_dy = (y - cup_cy) / cup_ry
            right_in = right_dx * right_dx + right_dy * right_dy <= 1.0
            on_cup = left_in or right_in

            if not (on_band or on_cup):
                continue

            # Shade darker toward the outer edge of each shape, lighter
            # toward its centreline -- the same highlight trick as before.
            if on_cup:
                edge_dx, edge_dy = (left_dx, left_dy) if left_in else (right_dx, right_dy)
                shade = 1.0 - 0.35 * math.hypot(edge_dx, edge_dy)
            else:
                shade = 1.0 - 0.35 * ((band_r - band_dist) / band_thickness)

            colour = (
                int(ACCENT_LIGHT[0] * shade + ACCENT[0] * (1 - shade)),
                int(ACCENT_LIGHT[1] * shade + ACCENT[1] * (1 - shade)),
                int(ACCENT_LIGHT[2] * shade + ACCENT[2] * (1 - shade)),
            )
            pixels[index:index + 4] = bytes((*colour, 255))

    return pixels


def _png(size: int, pixels: bytearray) -> bytes:
    """Encode RGBA pixels as a PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter type: none
        raw += pixels[y * size * 4:(y + 1) * size * 4]

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def _ico(images: list[tuple[int, bytes]]) -> bytes:
    """Pack PNG images into a multi-resolution .ico.

    Windows has accepted PNG-compressed icon entries since Vista, which keeps
    this far simpler than emitting BMP+mask for every size.
    """
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = bytearray()
    payload = bytearray()
    offset = len(header) + 16 * len(images)

    for size, data in images:
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,   # 0 means 256
            0 if size >= 256 else size,
            0,      # palette count
            0,      # reserved
            1,      # colour planes
            32,     # bits per pixel
            len(data),
            offset,
        )
        payload += data
        offset += len(data)

    return header + bytes(entries) + bytes(payload)


#: The hard in/out pixel tests in `_draw` have no anti-aliasing of their own,
#: which turns fine curves (the headphone band, the cup edges) into jagged
#: mush at 16-32px. Rendering at this many times the target resolution and
#: box-downsampling gives smooth edges without needing an imaging library.
SUPERSAMPLE = 4


def _downsample(size: int, factor: int, pixels: bytearray) -> bytearray:
    """Box-downsample an RGBA buffer by `factor`, blending in premultiplied
    alpha so a half-covered edge pixel fades toward transparent rather than
    toward black."""
    src_size = size * factor
    samples = factor * factor
    out = bytearray(size * size * 4)
    for oy in range(size):
        for ox in range(size):
            r_sum = g_sum = b_sum = a_sum = 0
            for sy in range(factor):
                row = ((oy * factor + sy) * src_size + ox * factor) * 4
                for sx in range(factor):
                    idx = row + sx * 4
                    a = pixels[idx + 3]
                    r_sum += pixels[idx] * a
                    g_sum += pixels[idx + 1] * a
                    b_sum += pixels[idx + 2] * a
                    a_sum += a
            out_a = a_sum // samples
            if out_a:
                out_r, out_g, out_b = (r_sum // a_sum, g_sum // a_sum, b_sum // a_sum)
            else:
                out_r = out_g = out_b = 0
            oi = (oy * size + ox) * 4
            out[oi:oi + 4] = bytes((out_r, out_g, out_b, out_a))
    return out


def _render(size: int) -> bytearray:
    return _downsample(size, SUPERSAMPLE, _draw(size * SUPERSAMPLE))


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    images = []
    for size in SIZES:
        data = _png(size, _render(size))
        images.append((size, data))
        if size == 256:
            (ASSETS / "icon.png").write_bytes(data)

    (ASSETS / "icon.ico").write_bytes(_ico(images))
    print(f"Wrote {ASSETS / 'icon.ico'} ({len(SIZES)} sizes) and icon.png")


if __name__ == "__main__":
    main()
