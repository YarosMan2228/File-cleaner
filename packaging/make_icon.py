"""Иконка программы без сторонних библиотек: синий квадрат со скруглением и две белые «искры» чистоты.

Рисуется со сглаживанием (4×4 выборки на пиксель) и сохраняется в .ico с PNG внутри (16…256 px).
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

ACCENT = (0x2F, 0x6F, 0xDE)
SIZES = (16, 24, 32, 48, 64, 128, 256)
SAMPLES = 4


def _inside_square(x: float, y: float, radius: float = 0.22) -> bool:
    """Квадрат 0..1 со скруглёнными углами."""
    cx = min(max(x, radius), 1 - radius)
    cy = min(max(y, radius), 1 - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def _inside_star(x: float, y: float, cx: float, cy: float, r: float) -> bool:
    """Четырёхлучевая «искра» (астроида): |dx|^(2/3) + |dy|^(2/3) <= r^(2/3)."""
    dx, dy = abs(x - cx), abs(y - cy)
    return dx ** (2 / 3) + dy ** (2 / 3) <= r ** (2 / 3)


def render(size: int) -> bytes:
    rows = []
    for py in range(size):
        row = bytearray([0])  # тип фильтра PNG для строки
        for px in range(size):
            square = star = 0
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    x = (px + (sx + 0.5) / SAMPLES) / size
                    y = (py + (sy + 0.5) / SAMPLES) / size
                    if _inside_square(x, y):
                        square += 1
                        if _inside_star(x, y, 0.45, 0.56, 0.30) or _inside_star(x, y, 0.72, 0.29, 0.12):
                            star += 1
            n = SAMPLES * SAMPLES
            alpha = square / n
            white = star / square if square else 0.0
            color = [round(c * (1 - white) + 255 * white) for c in ACCENT]
            row += bytes(color + [round(alpha * 255)])
        rows.append(bytes(row))
    return _png(size, b"".join(rows))


def _png(size: int, raw: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8 бит, RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def make_ico(path: Path) -> None:
    images = [render(size) for size in SIZES]
    offset = 6 + 16 * len(images)
    directory = struct.pack("<HHH", 0, 1, len(images))
    for size, data in zip(SIZES, images):
        directory += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.write_bytes(directory + b"".join(images))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # консоль может быть не в UTF-8
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("icon.ico")
    make_ico(target)
    print(f"Иконка: {target}")
