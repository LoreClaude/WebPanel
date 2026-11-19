"""Метка поверх окна игры: номер окна, его макрос и значки макросов всех окон.

Метка видна, только когда впереди окно игры. Она не принимает мышь и клавиатуру и не забирает фокус,
поэтому клики проходят в игру, а захват курсора не сбрасывается.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget

from . import win32
from .config import WINDOW_COUNT
from .macro_ui import status_text

CORNERS = {
    "top_left": "сверху слева", "top_center": "сверху по центру", "top_right": "сверху справа",
    "bottom_left": "снизу слева", "bottom_right": "снизу справа",
}
MARGIN = 16
PAD = 10
BG = QColor(11, 16, 22, 190)
BORDER = QColor("#3B4957")
INK = QColor("#E4EBF2")
DIM = QColor("#8C9BAD")
ACCENT = QColor("#7FA3FF")
STATE_ICON = {"running": ("▶", "#5CC48B"), "paused": ("❚❚", "#E5A84E"), "paused_user": ("❚❚", "#E5A84E"),
              "error": ("⚠", "#FF8B82"), "recording": ("●", "#FF8B82")}


def badge_content(ctl, slot: int) -> tuple[str, tuple[str, QColor], list[tuple[str, QColor, bool]]]:
    """Что показать: заголовок, строка макроса текущего окна и значки окон 1–5 (текст, цвет, текущее ли)."""
    title = f"{slot}  {ctl.slot_name(slot)}"
    if ctl.recording_slot == slot:
        line = (f"● запись: {ctl.recording_count} действий", QColor("#FF8B82"))
    else:
        st = ctl.macro_states.get(slot)
        active = st and st.get("state") in STATE_ICON
        line = (status_text(st) if active else "макрос не запущен", INK if active else DIM)
    marks = []
    for s in range(1, WINDOW_COUNT + 1):
        state = "recording" if ctl.recording_slot == s else (ctl.macro_states.get(s) or {}).get("state", "")
        icon, color = STATE_ICON.get(state, ("·", DIM.name()))
        marks.append((f"{s}{icon}", QColor(color), s == slot))
    return title, line, marks


class Badge(QWidget):
    def __init__(self, ctl, blocked: Callable[[], bool] = lambda: False):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle("Panel6 — метка")
        self.ctl = ctl
        self.blocked = blocked  # например, открыта сетка всех окон
        self.slot: int | None = None
        self._ticks = 0
        self.title_font = QFont(self.font())
        self.title_font.setPointSizeF(11)
        self.title_font.setBold(True)
        self.text_font = QFont(self.font())
        self.text_font.setPointSizeF(9.5)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check)
        self.timer.start(400)
        ctl.macro_status.connect(lambda *_: self._repaint())
        ctl.recording_changed.connect(lambda *_: self._repaint())

    def check(self) -> None:
        """Показать метку над окном игры, которое сейчас впереди, или спрятать."""
        cfg = self.ctl.cfg.get("badge", {})
        slot = self.ctl.foreground_slot() if cfg.get("enabled", True) and not self.blocked() else None
        if slot is None:
            self.slot = None
            if self.isVisible():
                self.hide()
            return
        self._ticks += 1
        if slot != self.slot or not self.isVisible():
            self.slot = slot
            self._place()
            self.show()
            win32.keep_on_top(int(self.winId()))
        elif self._ticks % 5 == 0:
            win32.keep_on_top(int(self.winId()))  # окно игры могло встать поверх при переключении
        self._repaint()

    def _repaint(self) -> None:
        if self.isVisible() and self.slot:
            self._place()
            self.update()

    def _size(self) -> tuple[int, int]:
        title, line, marks = badge_content(self.ctl, self.slot)
        tf, xf = QFontMetrics(self.title_font), QFontMetrics(self.text_font)
        marks_w = sum(xf.horizontalAdvance(t) for t, _c, _cur in marks) + 12 * (len(marks) - 1)
        text_w = max(tf.horizontalAdvance(title), xf.horizontalAdvance(line[0]), marks_w)
        w = text_w + 2 * PAD + 4  # запас на округление, иначе строка обрезается многоточием
        h = tf.height() + 2 * xf.height() + 2 * PAD + 8
        return min(w, 520), h

    def _place(self) -> None:
        w, h = self._size()
        area = self.ctl.game_rect()
        corner = self.ctl.cfg.get("badge", {}).get("corner", "top_right")
        if corner.endswith("left"):
            x = area.x() + MARGIN
        elif corner.endswith("center"):
            x = area.x() + (area.width() - w) // 2
        else:
            x = area.x() + area.width() - w - MARGIN
        y = area.y() + MARGIN if corner.startswith("top") else area.y() + area.height() - h - MARGIN
        if self.geometry() != QRect(x, y, w, h):
            self.setGeometry(x, y, w, h)

    def paintEvent(self, _e):  # noqa: N802
        if not self.slot:
            return
        title, line, marks = badge_content(self.ctl, self.slot)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(BORDER, 1))
        p.setBrush(BG)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        tf, xf = QFontMetrics(self.title_font), QFontMetrics(self.text_font)
        y = PAD + tf.ascent()
        p.setFont(self.title_font)
        p.setPen(ACCENT)
        p.drawText(PAD, y, title)
        p.setFont(self.text_font)
        y += tf.descent() + 4 + xf.ascent()
        p.setPen(line[1])
        p.drawText(PAD, y, xf.elidedText(line[0], Qt.ElideRight, self.width() - 2 * PAD))
        y += xf.descent() + 4 + xf.ascent()
        x = PAD
        for text, color, current in marks:
            p.setPen(color)
            p.drawText(x, y, text)
            w = xf.horizontalAdvance(text)
            if current:
                p.drawLine(x, y + 3, x + w, y + 3)
            x += w + 12
        p.end()
