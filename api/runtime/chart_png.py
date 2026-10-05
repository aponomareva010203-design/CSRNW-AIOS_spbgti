"""PNG-растеризация графиков chart_generate для Telegram (sendPhoto).

Инструмент chart_generate (api/runtime/tools.py) отдаёт SVG-строку собственного
производства — ровно эти элементы здесь и растеризуются: svg/rect/line/
polyline/circle/text(+tspan). Pillow берёт системный DejaVu (кириллица
покрыта). Если Pillow или шрифт недоступны — chart_png() вернёт None и бот
просто не приложит картинку: текстовый ответ не зависит от графика.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

_SVG_NS = "{http://www.w3.org/2000/svg}"

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
)
_FONT_BOLD_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
)


def _find_font(bold: bool):
    """truetype-шрифт или None (Telegram-график не критичен)."""
    try:
        from PIL import ImageFont
    except ImportError:
        return None
    paths = _FONT_BOLD_CANDIDATES if bold else _FONT_CANDIDATES
    for path in paths:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size=20)  # размер переопределяется
            except OSError:
                continue
    return None


def _color(value: str | None, default=(0, 0, 0)):
    """'#2563eb' → (37, 99, 235); именованные не нужны — генератор пишет hex."""
    if not value:
        return default
    value = value.strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", value)
    if not m:
        return default
    h = m.group(1)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _dash_segments(x1, y1, x2, y2, dash: str):
    """Пунктир: [{'6', '4'}] → [(отрезок), (пропуск), …] — Pillow не умеет dash."""
    parts = [float(p) for p in re.split(r"[ ,]+", dash.strip()) if p]
    if len(parts) < 2 or parts[0] <= 0:
        return [(x1, y1, x2, y2)]
    length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    if length == 0:
        return []
    dx, dy = (x2 - x1) / length, (y2 - y1) / length
    on, off, pos, segments = parts[0], parts[1 % len(parts)], 0.0, []
    drawing = True
    while pos < length:
        step = on if drawing else off
        nxt = min(pos + step, length)
        if drawing:
            segments.append((x1 + dx * pos, y1 + dy * pos, x1 + dx * nxt, y1 + dy * nxt))
        pos = nxt
        drawing = not drawing
    return segments


def chart_png(svg: str, scale: float = 2.0) -> bytes | None:
    """SVG-график chart_generate → PNG-байты (масштаб 2× для чёткости)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    try:
        root = ET.fromstring(svg)
    except ET.ParseError:
        return None

    viewbox = (root.get("viewBox") or "").replace(",", " ").split()
    if len(viewbox) != 4:
        return None
    _, _, vw, vh = (float(v) for v in viewbox)
    width, height = int(vw * scale), int(vh * scale)
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    fonts: dict[tuple[int, bool], object] = {}

    def font(size: float, bold: bool):
        key = (round(size * scale), bold)
        if key not in fonts:
            base = _find_font(bold)
            if base is None:
                return None
            fonts[key] = ImageFont.truetype(base.path, key[0])
        return fonts[key]

    def walk(node) -> None:
        tag = node.tag.replace(_SVG_NS, "") if node.tag.startswith(_SVG_NS) else node.tag
        if tag == "svg":
            for child in node:
                walk(child)
            return
        if tag == "rect":
            x, y = _float(node.get("x")), _float(node.get("y"))
            w, h = _float(node.get("width")), _float(node.get("height"))
            draw.rectangle(
                [x * scale, y * scale, (x + w) * scale, (y + h) * scale],
                fill=_color(node.get("fill")),
            )
        elif tag == "line":
            x1, y1 = _float(node.get("x1")), _float(node.get("y1"))
            x2, y2 = _float(node.get("x2")), _float(node.get("y2"))
            width_px = max(1, round(_float(node.get("stroke-width"), 1) * scale))
            color = _color(node.get("stroke"), (0, 0, 0))
            dash = node.get("stroke-dasharray")
            segments = _dash_segments(x1, y1, x2, y2, dash) if dash else [(x1, y1, x2, y2)]
            for sx1, sy1, sx2, sy2 in segments:
                draw.line([sx1 * scale, sy1 * scale, sx2 * scale, sy2 * scale],
                          fill=color, width=width_px)
        elif tag == "polyline":
            coords = re.findall(r"[-\d.]+", node.get("points") or "")
            pts = [
                (float(coords[i]) * scale, float(coords[i + 1]) * scale)
                for i in range(0, len(coords) - 1, 2)
            ]
            if len(pts) > 1:
                draw.line(pts, fill=_color(node.get("stroke")),
                          width=max(1, round(_float(node.get("stroke-width"), 1) * scale)),
                          joint="curve")
        elif tag == "circle":
            cx, cy, r = _float(node.get("cx")), _float(node.get("cy")), _float(node.get("r"))
            draw.ellipse(
                [(cx - r) * scale, (cy - r) * scale, (cx + r) * scale, (cy + r) * scale],
                fill=_color(node.get("fill")),
            )
        elif tag == "text":
            _text(node, 0.0)
        for child in node:
            walk(child)

    def _text(node, dy: float) -> None:
        x, y = _float(node.get("x")), _float(node.get("y")) + dy
        size = _float(node.get("font-size"), 12)
        bold = (node.get("font-weight") or "") in ("bold", "600", "700")
        f = font(size, bold)
        if f is None:
            return
        anchor_svg = node.get("text-anchor") or "start"
        anchor = {"middle": "ms", "end": "rs"}.get(anchor_svg, "ls")
        fill = _color(node.get("fill"))
        # собственный текст узла; tspan'ы рисуются отдельными строками
        # (dy — смещение строки по вертикали), иначе текст ляжет дважды
        if node.text and node.text.strip():
            draw.text((x * scale, y * scale), node.text, font=f, fill=fill, anchor=anchor)
        for tspan in node:
            if tspan.tag.replace(_SVG_NS, "") != "tspan":
                continue
            _text(tspan, _float(tspan.get("dy")))

    walk(root)
    import io

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
