"""PNG-аватар эмблемы AIOS для Telegram (/setuserpic) — stdlib без зависимостей.

Эмблема — фирменный сигил из вордмарка сайта (css .wordmark .sigil):
диагональный градиент #7C8B6C → #5D6C50 (135°) и внутренняя рамка #E9DCBB.
Пропорции сигила (30×30, внутренний квадрат inset 8, рамка 2, радиус 4)
масштабируются на 512×512. Прямоугольник полный, без скругления: Telegram
обрезает аватар в круг.

Запуск: python scripts/aios_emblem_png.py [размер]
Создаёт frontend/assets/aios-emblem-{size}.png. В SVG-версии см.
frontend/assets/aios-emblem.svg.
"""

import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

GREEN = (124, 139, 108)      # #7C8B6C  пыльный зелёный
GREEN_DEEP = (93, 108, 80)   # #5D6C50  тёмный зелёный
GOLD = (233, 220, 187)       # #E9DCBB  золотая рамка

# геометрия сигила 30×30 → доля от размера
INSET = 8 / 30      # внешний край золотой рамки от края холста
BORDER = 2 / 30     # толщина рамки
CORNER = 4 / 30     # радиус скругления внутренней рамки


def lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient(color: tuple, x: float, y: float, size: float) -> tuple:
    """Диагональный градиент 135° (CSS): от левого верхнего к правому нижнему."""
    t = (x + y) / (2 * size)
    return lerp(GREEN, GREEN_DEEP, t)


def rounded_rect_sdf(px: float, py: float, half: float, radius: float) -> float:
    """SDF скруглённого квадрата с центром (0,0) и половиной стороны half:
    <0 внутри, >0 снаружи (расстояние до границы)."""
    qx = abs(px) - half + radius
    qy = abs(py) - half + radius
    outside = (max(qx, 0) ** 2 + max(qy, 0) ** 2) ** 0.5
    inside = min(max(qx, qy), 0)
    return outside + inside - radius


def render(size: int) -> bytes:
    """RGBA-пиксели эмблемы: градиент + золотая рамка-кольцо."""
    cx = size / 2
    outer_half = size * (0.5 - INSET)        # половина внешнего края рамки
    inner_half = outer_half - size * BORDER  # половина внутреннего края рамки
    outer_r = size * CORNER
    inner_r = max(outer_r - size * BORDER, 1.0)
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # фильтр строки PNG (None)
        for x in range(size):
            # рамка: снаружи от внутреннего контура и внутри внешнего
            d_out = rounded_rect_sdf(x - cx, y - cx, outer_half, outer_r)
            d_in = rounded_rect_sdf(x - cx, y - cx, inner_half, inner_r)
            base = gradient(GREEN, x, y, size)
            if d_out <= 0 <= d_in:
                base = GOLD
            rows.extend(base)
    return bytes(rows)


def write_png(path: Path, size: int, raw: bytes) -> None:
    """raw — IDAT-вход: по строке на ряд (байт фильтра + RGB-пиксели),
    как собрано в render()."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # RGB8
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def main() -> None:
    size = int(sys.argv[1]) if len(sys.argv) > 1 else 512
    raw = render(size)
    out = ROOT / "frontend" / "assets" / f"aios-emblem-{size}.png"
    write_png(out, size, raw)
    # самопроверка: углы — градиент, центр рамки — золото, центр поля — не золото
    row = size * 3 + 1

    def px(x, y):
        off = y * row + 1 + x * 3
        return tuple(raw[off : off + 3])

    assert px(0, 0) == GREEN, px(0, 0)
    assert px(size - 1, size - 1) == GREEN_DEEP, px(size - 1, size - 1)
    # середина золотой полосы (INSET + BORDER/2), а не её край — иначе
    # на малых размерах пиксель границы уходит в градиент
    band_mid = round(size * (INSET + BORDER / 2))
    assert px(size // 2, band_mid) == GOLD, px(size // 2, band_mid)
    assert px(size // 2, size // 2) != GOLD  # внутри рамки — градиент, не золото
    print(f"{out} ({size}×{size}, {out.stat().st_size} байт)")


if __name__ == "__main__":
    main()