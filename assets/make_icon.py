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
    """Render the icon as RGBA rows: a waveform inside a rounded square."""
    pixels = bytearray(size * size * 4)
    radius = size * 0.22
    centre = size / 2.0
    #: Bars get thinner relative to the canvas as it grows, so the small sizes
    #: stay legible instead of turning into mush.
    bar_count = 5 if size <= 32 else 7
    bar_gap = size * (0.030 if size <= 32 else 0.022)
    span = size * 0.62
    bar_width = (span - bar_gap * (bar_count - 1)) / bar_count
    left = centre - span / 2

    # Relative bar heights, symmetric so it reads as a waveform.
    profile = [0.42, 0.72, 1.0, 0.58, 0.86, 0.34, 0.62][:bar_count]
    if bar_count == 5:
        profile = [0.45, 0.80, 1.0, 0.62, 0.38]

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

            # -- bars ---------------------------------------------------
            offset = x - left
            if offset < 0:
                continue
            slot = offset / (bar_width + bar_gap)
            bar = int(slot)
            if bar >= bar_count or (slot - bar) * (bar_width + bar_gap) > bar_width:
                continue

            height = span * profile[bar]
            if abs(y - centre) > height / 2:
                continue

            shade = 1.0 - 0.35 * (abs(y - centre) / max(1.0, height / 2))
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


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    images = []
    for size in SIZES:
        data = _png(size, _draw(size))
        images.append((size, data))
        if size == 256:
            (ASSETS / "icon.png").write_bytes(data)

    (ASSETS / "icon.ico").write_bytes(_ico(images))
    print(f"Wrote {ASSETS / 'icon.ico'} ({len(SIZES)} sizes) and icon.png")


if __name__ == "__main__":
    main()
