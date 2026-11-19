"""Сетка «все окна»: полноэкранный слой поверх игрового монитора с живыми миниатюрами 3×2.

Клик и перетаскивание в миниатюре окна Chrome уходят в настоящее окно, клавиши — в выбранное окно.
Двойной клик — развернуть окно, Esc или клавиша сетки — закрыть.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QKeyEvent, QMouseEvent, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import QWidget

from .controller import DESKTOP_SLOT, SLOTS, Controller
from .geometry import Rect, grid_cells, to_page
from .keys import cdp_key, parse_hotkey
from .thumbs import ThumbHost

log = logging.getLogger(__name__)
LABEL_H = 30
BG = QColor("#0B1016")
CELL = QColor("#151E28")
INK = QColor("#DCE5EF")
DIM = QColor("#8C9BAD")
ACCENT = QColor("#7FA3FF")

_BUTTONS = {Qt.LeftButton: ("left", 1), Qt.RightButton: ("right", 2), Qt.MiddleButton: ("middle", 4)}


class GridOverlay(QWidget):
    def __init__(self, ctl: Controller):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.ctl = ctl
        self.setWindowTitle("Panel6 — все окна")
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(False)
        self.host = ThumbHost(self)
        self.cells: list[Rect] = []
        self.thumb_rects: dict[int, Rect] = {}
        self.selected: int | None = None
        self.drag_slot: int | None = None
        self.page_sizes: dict[int, tuple[float, float]] = {}

    # ------------------------------------------------------------ показ
    def toggle(self) -> None:
        if self.isVisible():
            self.close_grid()
        else:
            self.open_grid()

    def open_grid(self) -> None:
        self.setGeometry(self.ctl.game_rect())
        self.selected = self.ctl.history.current if self.ctl.history.current else None
        self.ctl.grid_focus_slot = self.selected
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self.relayout()

    def close_grid(self) -> None:
        self.ctl.grid_focus_slot = None
        self.host.clear()
        self.hide()

    def relayout(self) -> None:
        if not self.isVisible():
            return
        self.cells = grid_cells(self.width(), self.height() - 22, gap=14, margin=14)
        self.host.sync({s: self.ctl.slot_hwnd(s) for s in range(1, SLOTS + 1)})
        self.thumb_rects.clear()
        self.page_sizes.clear()
        for slot, cell in enumerate(self.cells, start=1):
            area = Rect(cell.x, cell.y + LABEL_H, cell.w, cell.h - LABEL_H)
            r = self.host.place(slot, area)
            if r:
                self.thumb_rects[slot] = r
        self.update()

    def resizeEvent(self, e):  # noqa: N802
        self.relayout()
        super().resizeEvent(e)

    # ------------------------------------------------------------ рисование
    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), BG)
        f = QFont(self.font())
        f.setPointSizeF(11)
        p.setFont(f)
        for slot, cell in enumerate(self.cells, start=1):
            p.fillRect(QRectF(cell.x, cell.y, cell.w, cell.h), CELL)
            name = self.ctl.slot_name(slot)
            hk = self.ctl.cfg["slot6"]["hotkey"] if slot == DESKTOP_SLOT else self.ctl.cfg["windows"][slot - 1]["hotkey"]
            p.setPen(INK)
            p.drawText(QRectF(cell.x + 10, cell.y, cell.w - 120, LABEL_H), Qt.AlignVCenter | Qt.AlignLeft, f"{slot}  {name}")
            p.setPen(DIM)
            p.drawText(QRectF(cell.x, cell.y, cell.w - 10, LABEL_H), Qt.AlignVCenter | Qt.AlignRight, hk)
            if slot not in self.thumb_rects:
                status = self.ctl.statuses.get(slot, "off")
                text = {
                    "off": "не запущено",
                    "starting": "запускается…",
                    "missing": "окно не найдено",
                }.get(status, "двойной клик — открыть")
                if slot == DESKTOP_SLOT and self.ctl.cfg["slot6"]["mode"] == "desktop":
                    text = "рабочий стол\nдвойной клик — открыть"
                p.drawText(QRectF(cell.x, cell.y + LABEL_H, cell.w, cell.h - LABEL_H), Qt.AlignCenter, text)
            if slot == self.selected:
                p.setPen(QPen(ACCENT, 3))
                p.drawRect(QRectF(cell.x + 1.5, cell.y + 1.5, cell.w - 3, cell.h - 3))
        p.setPen(DIM)
        p.drawText(QRectF(0, self.height() - 28, self.width(), 24), Qt.AlignCenter,
                   "клик — управлять окном   ·   двойной клик — развернуть   ·   Esc — закрыть")

    # ------------------------------------------------------------ мышь
    def _slot_at(self, x: float, y: float) -> int | None:
        for slot, cell in enumerate(self.cells, start=1):
            if cell.contains(x, y):
                return slot
        return None

    def _page_point(self, slot: int, x: float, y: float):
        """Точка сетки -> координаты страницы окна Chrome (или None)."""
        if slot == DESKTOP_SLOT or slot not in self.thumb_rects:
            return None
        win = self.ctl.windows[slot - 1]
        if slot not in self.page_sizes:
            try:
                self.page_sizes[slot] = win.page_size()
            except Exception as e:  # noqa: BLE001
                log.info("размер страницы окна %s: %s", slot, e)
                return None
        pw, ph = self.page_sizes[slot]
        return to_page(x, y, self.thumb_rects[slot], pw, ph)

    def _send_mouse(self, slot: int, kind: str, e: QMouseEvent, buttons_mask: int) -> None:
        pt = self._page_point(slot, e.position().x(), e.position().y())
        if pt is None:
            return
        button, _ = _BUTTONS.get(e.button(), ("none", 0))
        try:
            self.ctl.windows[slot - 1].mouse(kind, pt[0], pt[1], button=button, buttons=buttons_mask)
        except Exception as ex:  # noqa: BLE001
            log.info("клик в окно %s: %s", slot, ex)

    @staticmethod
    def _mask(buttons) -> int:
        return sum(bit for b, (_, bit) in _BUTTONS.items() if buttons & b)

    def mousePressEvent(self, e: QMouseEvent):  # noqa: N802
        slot = self._slot_at(e.position().x(), e.position().y())
        if slot is None:
            return
        if slot != self.selected:
            self.selected = slot
            self.ctl.grid_focus_slot = slot  # макрос в этом окне встанет на паузу, пока ты им управляешь
            self.update()
        self.drag_slot = slot
        self._send_mouse(slot, "mousePressed", e, self._mask(e.buttons()))

    def mouseMoveEvent(self, e: QMouseEvent):  # noqa: N802
        if self.drag_slot and e.buttons():
            pt = self._page_point(self.drag_slot, e.position().x(), e.position().y())
            if pt:
                try:
                    self.ctl.windows[self.drag_slot - 1].mouse("mouseMoved", pt[0], pt[1], button="none",
                                                               buttons=self._mask(e.buttons()))
                except Exception:  # noqa: BLE001
                    pass

    def mouseReleaseEvent(self, e: QMouseEvent):  # noqa: N802
        if self.drag_slot:
            self._send_mouse(self.drag_slot, "mouseReleased", e, self._mask(e.buttons()))
        self.drag_slot = None

    def mouseDoubleClickEvent(self, e: QMouseEvent):  # noqa: N802
        slot = self._slot_at(e.position().x(), e.position().y())
        if slot:
            self.close_grid()
            self.ctl.activate(slot)

    def wheelEvent(self, e: QWheelEvent):  # noqa: N802
        slot = self._slot_at(e.position().x(), e.position().y())
        if not slot:
            return
        pt = self._page_point(slot, e.position().x(), e.position().y())
        if pt:
            try:
                self.ctl.windows[slot - 1].wheel(pt[0], pt[1], -e.angleDelta().y())
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ клавиатура
    def _is_grid_key(self, e: QKeyEvent) -> bool:
        try:
            return parse_hotkey(self.ctl.cfg["hotkeys"]["grid"])[1] == e.nativeVirtualKey()
        except Exception:  # noqa: BLE001
            return False

    def keyPressEvent(self, e: QKeyEvent):  # noqa: N802
        if e.key() == Qt.Key_Escape or self._is_grid_key(e):
            self.close_grid()
            return
        self._send_key("keyDown", e)

    def keyReleaseEvent(self, e: QKeyEvent):  # noqa: N802
        if e.key() != Qt.Key_Escape:
            self._send_key("keyUp", e)

    def _send_key(self, kind: str, e: QKeyEvent) -> None:
        slot = self.selected
        if not slot or slot == DESKTOP_SLOT or e.isAutoRepeat() and kind == "keyUp":
            return
        params = cdp_key(e.nativeVirtualKey(), e.text())
        if params:
            try:
                self.ctl.windows[slot - 1].key(kind, params)
            except Exception as ex:  # noqa: BLE001
                log.info("клавиша в окно %s: %s", slot, ex)
