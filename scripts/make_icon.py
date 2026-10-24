"""Write the placeholder app icon, unraid/periodicool.png (256x256), with the standard
library only: a charcoal tile with an amber cassette label and two tape reels, in the
theme's colors. Replace the PNG with the real icon under the same name.

    python scripts/make_icon.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZE = 256
CHARCOAL = (31, 33, 31)
AMBER = (232, 129, 42)
LABEL_DARK = (58, 40, 22)
REEL = (31, 33, 31)
REEL_RING = (245, 236, 220)
OUT = Path(__file__).resolve().parent.parent / "unraid" / "periodicool.png"


def _inside_rounded(x: float, y: float, left, top, right, bottom, radius) -> bool:
    if not (left <= x <= right and top <= y <= bottom):
        return False
    cx = min(max(x, left + radius), right - radius)
    cy = min(max(y, top + radius), bottom - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius**2


def pixel(x: int, y: int) -> tuple[int, int, int, int]:
    px, py = x + 0.5, y + 0.5
    if not _inside_rounded(px, py, 0, 0, SIZE, SIZE, 40):
        return (0, 0, 0, 0)  # transparent corners
    color = CHARCOAL
    if _inside_rounded(px, py, 28, 64, 228, 192, 18):
        color = AMBER
        if _inside_rounded(px, py, 64, 104, 192, 152, 20):
            color = LABEL_DARK  # the tape window
        for cx in (92, 164):
            d2 = (px - cx) ** 2 + (py - 128) ** 2
            if d2 <= 18**2:
                color = REEL_RING
            if d2 <= 9**2:
                color = REEL
        if 76 <= py <= 84 and 48 <= px <= 208:
            color = LABEL_DARK  # a label line
    return (*color, 255)


def png(width: int, height: int, rows: list[bytes]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + row for row in rows)  # filter type 0 per row
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    rows = [b"".join(bytes(pixel(x, y)) for x in range(SIZE)) for y in range(SIZE)]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(png(SIZE, SIZE, rows))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
