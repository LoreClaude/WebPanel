"""Живые миниатюры окон внутри наших окон Qt (через DWM: без нагрузки на процессор)."""

from __future__ import annotations

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QWidget

from . import win32
from .geometry import Rect, fit


class ThumbHost:
    """Держит DWM-миниатюры для одного окна верхнего уровня (приёмника)."""

    def __init__(self, top: QWidget):
        self.top = top
        self.thumbs: dict[int, win32.Thumbnail] = {}

    def _dest(self) -> int:
        return int(self.top.winId())

    def sync(self, sources: dict[int, int | None]) -> None:
        """sources: {слот: hwnd источника или None}. Пересоздаёт миниатюры при смене окна."""
        for slot, hwnd in sources.items():
            cur = self.thumbs.get(slot)
            if cur and (cur.src != hwnd or not win32.is_window(hwnd)):
                cur.close()
                del self.thumbs[slot]
                cur = None
            if cur is None and hwnd and win32.is_window(hwnd):
                t = win32.Thumbnail(self._dest(), hwnd)
                if t.ok:
                    self.thumbs[slot] = t

    def place(self, slot: int, area: QWidget | Rect, visible: bool = True) -> Rect | None:
        """Вписывает миниатюру слота в виджет area (или в прямоугольник в координатах окна).

        Возвращает фактический прямоугольник миниатюры в логических координатах окна.
        """
        t = self.thumbs.get(slot)
        if t is None:
            return None
        if isinstance(area, QWidget):
            pos = area.mapTo(self.top, QPoint(0, 0))
            box = Rect(pos.x(), pos.y(), area.width(), area.height())
        else:
            box = area
        sw, sh = t.source_size()
        r = fit(box, sw, sh)
        dpr = self.top.devicePixelRatioF()
        t.update(round(r.x * dpr), round(r.y * dpr), round(r.w * dpr), round(r.h * dpr), visible and r.w > 2)
        return r

    def has(self, slot: int) -> bool:
        return slot in self.thumbs

    def clear(self) -> None:
        for t in self.thumbs.values():
            t.close()
        self.thumbs.clear()
