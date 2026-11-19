"""Общие элементы интерфейса."""

from __future__ import annotations

import datetime
import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QKeyEvent
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QVBoxLayout,
    QWidget,
)

from .keys import HotkeyError, format_hotkey, parse_hotkey

log = logging.getLogger(__name__)


def _kv(k) -> int:
    return int(k.value) if hasattr(k, "value") else int(k)


_QT_SPECIAL = {
    Qt.Key.Key_Space: 0x20, Qt.Key.Key_Return: 0x0D, Qt.Key.Key_Enter: 0x0D, Qt.Key.Key_Escape: 0x1B,
    Qt.Key.Key_Tab: 0x09, Qt.Key.Key_Backspace: 0x08, Qt.Key.Key_Shift: 0x10, Qt.Key.Key_Control: 0x11,
    Qt.Key.Key_Alt: 0x12, Qt.Key.Key_Left: 0x25, Qt.Key.Key_Up: 0x26, Qt.Key.Key_Right: 0x27,
    Qt.Key.Key_Down: 0x28, Qt.Key.Key_Delete: 0x2E, Qt.Key.Key_Insert: 0x2D, Qt.Key.Key_Home: 0x24,
    Qt.Key.Key_End: 0x23, Qt.Key.Key_PageUp: 0x21, Qt.Key.Key_PageDown: 0x22,
    Qt.Key.Key_Minus: 0xBD, Qt.Key.Key_Underscore: 0xBD, Qt.Key.Key_Equal: 0xBB, Qt.Key.Key_Plus: 0xBB,
    Qt.Key.Key_BracketLeft: 0xDB, Qt.Key.Key_BracketRight: 0xDD, Qt.Key.Key_Semicolon: 0xBA,
    Qt.Key.Key_Apostrophe: 0xDE, Qt.Key.Key_Comma: 0xBC, Qt.Key.Key_Period: 0xBE, Qt.Key.Key_Slash: 0xBF,
    Qt.Key.Key_Backslash: 0xDC, Qt.Key.Key_QuoteLeft: 0xC0,
}

_QT_KEYPAD = {
    Qt.Key.Key_Asterisk: 0x6A, Qt.Key.Key_Plus: 0x6B, Qt.Key.Key_Minus: 0x6D, Qt.Key.Key_Period: 0x6E,
    Qt.Key.Key_Slash: 0x6F,
}


def event_vk(e: QKeyEvent) -> int:
    """Виртуальный код клавиши Windows для события Qt."""
    if sys.platform == "win32" and e.nativeVirtualKey():
        return e.nativeVirtualKey()
    k = _kv(e.key())
    if e.modifiers() & Qt.KeypadModifier:
        if _kv(Qt.Key.Key_0) <= k <= _kv(Qt.Key.Key_9):
            return 0x60 + k - _kv(Qt.Key.Key_0)
        keypad = next((vk for qk, vk in _QT_KEYPAD.items() if _kv(qk) == k), 0)
        if keypad:
            return keypad
    if _kv(Qt.Key.Key_A) <= k <= _kv(Qt.Key.Key_Z) or _kv(Qt.Key.Key_0) <= k <= _kv(Qt.Key.Key_9):
        return k
    if _kv(Qt.Key.Key_F1) <= k <= _kv(Qt.Key.Key_F24):
        return 0x70 + k - _kv(Qt.Key.Key_F1)
    return next((vk for qk, vk in _QT_SPECIAL.items() if _kv(qk) == k), 0)


class HotkeyEdit(QLineEdit):
    """Поле, в которое нажимают нужное сочетание. Backspace/Delete — очистить."""

    def __init__(self, text: str):
        super().__init__(text)
        self.setPlaceholderText("нажми клавишу…")
        self.setToolTip("Щёлкни и нажми сочетание. Цифры цифрового блока — при включённом NumLock.\n"
                        "Клавиша без Ctrl/Alt (например, - или =) занята панелью во всех программах:\n"
                        "пока панель запущена, она не печатается в чате игры и других окнах.")

    def keyPressEvent(self, e: QKeyEvent):  # noqa: N802
        if e.key() in (Qt.Key_Backspace, Qt.Key_Delete):
            self.clear()
            return
        if e.key() in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta, Qt.Key_Tab):
            if e.key() == Qt.Key_Tab:
                super().keyPressEvent(e)
            return
        vk = event_vk(e)
        mods = 0
        m = e.modifiers()
        if m & Qt.ControlModifier:
            mods |= 0x2
        if m & Qt.AltModifier:
            mods |= 0x1
        if m & Qt.ShiftModifier:
            mods |= 0x4
        text = format_hotkey(mods, vk)
        try:
            parse_hotkey(text)
        except HotkeyError:
            return
        self.setText(text)


class ElidedLabel(QLabel):
    """Надпись в одну строку, которая не растягивает окно: длинный текст обрезается «…», целиком он в подсказке.

    Обычная QLabel требует ширину под весь текст, и одна длинная строка раздвигала панель на два монитора.
    """

    def __init__(self, text: str = ""):
        super().__init__()
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full = text or ""
        self._elide()

    def text(self) -> str:
        return self._full

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._elide()

    def _elide(self) -> None:
        shown = self.fontMetrics().elidedText(self._full, Qt.ElideRight, max(self.width(), 0))
        super().setText(shown)
        self.setToolTip(self._full if shown != self._full else "")


class EventLog(QWidget):
    """Журнал событий панели: время и текст, новые снизу. Свёрнутый журнал показывает последнюю строку."""

    WARN = QColor("#FF8B82")

    def __init__(self, limit: int = 500, lines: int = 5):
        super().__init__()
        self.limit = limit
        self.collapsed = False
        self.list = QListWidget()
        self.list.setWordWrap(True)  # длинная строка переносится, а не раздвигает окно
        self.list.setResizeMode(QListView.Adjust)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setSelectionMode(QListWidget.NoSelection)
        self.list.setFocusPolicy(Qt.NoFocus)
        row_h = self.list.fontMetrics().height() + 4
        self.list.setFixedHeight(row_h * lines + 8)
        self.title = QLabel("Журнал")
        self.title.setObjectName("dim")
        self.last = ElidedLabel()
        self.last.setObjectName("dim")
        self.toggle = QPushButton("Свернуть")
        self.toggle.clicked.connect(lambda: self.set_collapsed(not self.collapsed))
        clear = QPushButton("Очистить")
        clear.clicked.connect(self.list.clear)
        head = QHBoxLayout()
        head.addWidget(self.title)
        head.addWidget(self.last, 1)
        head.addWidget(clear)
        head.addWidget(self.toggle)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addLayout(head)
        lay.addWidget(self.list)
        self.set_collapsed(False)

    def add(self, text: str, warn: bool = False) -> None:
        warn = warn or text.startswith("⚠")
        log.info("журнал: %s", text)
        line = f"{datetime.datetime.now():%H:%M:%S}  {text}"
        item = QListWidgetItem(line)
        if warn:
            item.setForeground(self.WARN)
        self.list.addItem(item)
        while self.list.count() > self.limit:
            self.list.takeItem(0)
        self.list.scrollToBottom()
        self.last.setText(line if self.collapsed else "")
        self.last.setStyleSheet(f"color:{self.WARN.name()}" if warn else "")

    def lines(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self.list.setVisible(not collapsed)
        self.toggle.setText("Развернуть" if collapsed else "Свернуть")
        n = self.list.count()
        self.last.setText(self.list.item(n - 1).text() if collapsed and n else "")
