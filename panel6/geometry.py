"""Чистая геометрия: раскладка сетки, вписывание миниатюр и перевод координат клика."""

from __future__ import annotations

from typing import NamedTuple


class Rect(NamedTuple):
    x: float
    y: float
    w: float
    h: float

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h


def grid_cells(width: float, height: float, cols: int = 3, rows: int = 2, gap: float = 12, margin: float = 12) -> list[Rect]:
    cw = (width - 2 * margin - gap * (cols - 1)) / cols
    ch = (height - 2 * margin - gap * (rows - 1)) / rows
    return [Rect(margin + c * (cw + gap), margin + r * (ch + gap), cw, ch) for r in range(rows) for c in range(cols)]


def fit(container: Rect, src_w: float, src_h: float) -> Rect:
    """Вписывает источник src_w×src_h в container с сохранением пропорций, по центру."""
    if src_w <= 0 or src_h <= 0 or container.w <= 0 or container.h <= 0:
        return container
    scale = min(container.w / src_w, container.h / src_h)
    w, h = src_w * scale, src_h * scale
    return Rect(container.x + (container.w - w) / 2, container.y + (container.h - h) / 2, w, h)


def to_page(px: float, py: float, thumb: Rect, page_w: float, page_h: float) -> tuple[float, float] | None:
    """Точка на миниатюре -> координаты страницы в окне Chrome (CSS-пиксели); None — мимо миниатюры."""
    if not thumb.contains(px, py) or thumb.w <= 0 or thumb.h <= 0:
        return None
    return (px - thumb.x) / thumb.w * page_w, (py - thumb.y) / thumb.h * page_h
