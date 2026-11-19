"""Окно «Макросы»: библиотека набора, свойства макроса, шаги, запись и проверка."""

from __future__ import annotations

import copy
import json
import logging
from typing import Callable

from PySide6.QtCore import QItemSelection, QItemSelectionModel, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QKeyEvent, QKeySequence, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QToolButton,
    QVBoxLayout, QWidget,
)

from .keys import cdp_key
from .widgets import ElidedLabel, HotkeyEdit, event_vk
from .macros import (
    BUTTONS, MAX_REPEAT, RANDOM_KINDS, STEP_TYPES, USER_INPUT_MODES, check_blocks, clone_macro, delay_text, describe,
    drop_short_pauses, jitter_pauses, key_title, merge_looks, new_macro, normalize_step, scale_pauses,
)

log = logging.getLogger(__name__)


class KeyCaptureEdit(QLineEdit):
    """Поле, в котором нажимают нужную клавишу игры."""

    changed = Signal()

    def __init__(self, step: dict | None = None):
        super().__init__()
        self.params = {k: step[k] for k in ("code", "key", "vk")} if step and step.get("code") else {}
        self.setText(key_title(self.params) if self.params else "")
        self.setPlaceholderText("щёлкни и нажми клавишу")

    def keyPressEvent(self, e: QKeyEvent):  # noqa: N802
        vk = event_vk(e)
        params = cdp_key(vk, e.text())
        if not params:
            return
        self.params = {"code": params["code"], "key": params["key"], "vk": vk}
        self.setText(key_title(self.params))
        self.changed.emit()

    def keyReleaseEvent(self, e):  # noqa: N802
        e.accept()


class KeyListEdit(QLineEdit):
    """Поле со списком клавиш: каждое нажатие добавляет клавишу, Backspace убирает последнюю."""

    changed = Signal()

    def __init__(self, keys: list[dict] | None = None):
        super().__init__()
        self.keys = [dict(k) for k in keys or []]
        self.setPlaceholderText("щёлкни и нажимай клавиши; Backspace — убрать последнюю")
        self._show()

    def _show(self) -> None:
        self.setText("  ".join(key_title(k) for k in self.keys))

    def keyPressEvent(self, e: QKeyEvent):  # noqa: N802
        if e.key() == Qt.Key_Backspace:
            if self.keys:
                self.keys.pop()
                self._show()
                self.changed.emit()
            return
        vk = event_vk(e)
        params = cdp_key(vk, e.text())
        if not params:
            return
        spec = {"code": params["code"], "key": params["key"], "vk": vk}
        if all((k["code"] or k["key"]) != (spec["code"] or spec["key"]) for k in self.keys):
            self.keys.append(spec)
            self._show()
            self.changed.emit()

    def keyReleaseEvent(self, e):  # noqa: N802
        e.accept()

    def clear_keys(self) -> None:
        self.keys = []
        self._show()
        self.changed.emit()


# ------------------------------------------------------------------ выбор точки или области на снимке окна

class ShotView(QWidget):
    picked = Signal(float, float)
    region_picked = Signal(float, float, float, float)

    def __init__(self, pixmap: QPixmap, mark: tuple[float, float] | None = None, mode: str = "point",
                 region: tuple[float, float, float, float] | None = None):
        super().__init__()
        self.pm = pixmap
        self.mark = mark
        self.mode = mode
        self.region = region
        self._drag_from: tuple[float, float] | None = None
        self.hover: QPointF | None = None
        self.setMouseTracking(True)
        self.setMinimumSize(480, 270)
        self.setCursor(Qt.CrossCursor)

    def image_rect(self) -> QRectF:
        w, h = self.width(), self.height()
        pw, ph = max(self.pm.width(), 1), max(self.pm.height(), 1)
        s = min(w / pw, h / ph)
        return QRectF((w - pw * s) / 2, (h - ph * s) / 2, pw * s, ph * s)

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#080C10"))
        r = self.image_rect()
        p.drawPixmap(r, self.pm, QRectF(self.pm.rect()))
        if self.hover and r.contains(self.hover):
            p.setPen(QPen(QColor(255, 255, 255, 120), 1))
            p.drawLine(QPointF(r.left(), self.hover.y()), QPointF(r.right(), self.hover.y()))
            p.drawLine(QPointF(self.hover.x(), r.top()), QPointF(self.hover.x(), r.bottom()))
        if self.mode == "point" and self.mark:
            c = QPointF(r.x() + self.mark[0] * r.width(), r.y() + self.mark[1] * r.height())
            p.setPen(QPen(QColor("#FF5A5A"), 2))
            p.drawEllipse(c, 9, 9)
            p.drawLine(c + QPointF(-14, 0), c + QPointF(14, 0))
            p.drawLine(c + QPointF(0, -14), c + QPointF(0, 14))
        if self.mode == "region" and self.region:
            x1, y1, x2, y2 = self.region
            box = QRectF(r.x() + x1 * r.width(), r.y() + y1 * r.height(), (x2 - x1) * r.width(), (y2 - y1) * r.height())
            p.fillRect(box, QColor(255, 90, 90, 50))
            p.setPen(QPen(QColor("#FF5A5A"), 2))
            p.drawRect(box)

    def _norm(self, pos: QPointF) -> tuple[float, float]:
        r = self.image_rect()
        return (min(max((pos.x() - r.x()) / r.width(), 0.0), 1.0), min(max((pos.y() - r.y()) / r.height(), 0.0), 1.0))

    def mouseMoveEvent(self, e):  # noqa: N802
        self.hover = e.position()
        if self._drag_from is not None:
            (ax, ay), (bx, by) = self._drag_from, self._norm(e.position())
            self.region = (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))
        self.update()

    def mousePressEvent(self, e):  # noqa: N802
        r = self.image_rect()
        pos = e.position()
        if not r.contains(pos):
            return
        if self.mode == "region":
            self._drag_from = self._norm(pos)
            self.region = self._drag_from * 2
        else:
            self.mark = self._norm(pos)
            self.picked.emit(*self.mark)
        self.update()

    def mouseReleaseEvent(self, e):  # noqa: N802
        if self._drag_from is None:
            return
        self._drag_from = None
        x1, y1, x2, y2 = self.region
        if x2 - x1 > 0.002 and y2 - y1 > 0.002:
            self.region_picked.emit(*self.region)
        else:
            self.region = None
        self.update()


class ShotPicker(QDialog):
    """Снимок окна: щёлкнуть точку (mode="point") или обвести область (mode="region")."""

    def __init__(self, png: bytes, mark: tuple[float, float] | None = None, parent=None, mode: str = "point",
                 region: tuple[float, float, float, float] | None = None):
        super().__init__(parent)
        self.mode = mode
        self.setWindowTitle("Укажи точку на снимке окна" if mode == "point" else "Обведи область на снимке окна")
        pm = QPixmap()
        pm.loadFromData(png)
        self.view = ShotView(pm, mark, mode, region)
        self.point = mark
        self.region = region
        self.info = QLabel()
        self.view.picked.connect(self._picked)
        self.view.region_picked.connect(self._region)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Выбрать")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Щёлкни по нужному месту. Координаты сохранятся в долях окна." if mode == "point" else
                             "Зажми левую кнопку и обведи область. Координаты сохранятся в долях окна."))
        lay.addWidget(self.view, 1)
        row = QHBoxLayout()
        row.addWidget(self.info, 1)
        row.addWidget(buttons)
        lay.addLayout(row)
        if mode == "point":
            self._picked(*(mark or (None, None)))
        else:
            self._region(*(region or (None, None, None, None)))
        self.resize(1100, 700)

    def _region(self, x1, y1, x2, y2):
        if x1 is None:
            self.info.setText("Область не выбрана")
            return
        self.region = (x1, y1, x2, y2)
        self.info.setText(f"X {x1 * 100:.1f}–{x2 * 100:.1f}%   Y {y1 * 100:.1f}–{y2 * 100:.1f}%")

    def _picked(self, x, y):
        if x is None:
            self.info.setText("Точка не выбрана")
            return
        self.point = (x, y)
        self.info.setText(f"X {x * 100:.2f}%   Y {y * 100:.2f}%")


# ------------------------------------------------------------------ редактор одного шага

def _ms_spin(value: int, maximum: int = 3_600_000) -> QSpinBox:
    s = QSpinBox()
    s.setRange(0, maximum)
    s.setSingleStep(50)
    s.setSuffix(" мс")
    s.setValue(int(value))
    return s


def _pct_spin(value: float) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(0, 100)
    s.setDecimals(2)
    s.setSuffix(" %")
    s.setValue(value * 100)
    return s


class StepDialog(QDialog):
    """Изменение одного шага. shot() возвращает PNG-снимок окна для выбора точки или None."""

    def __init__(self, step: dict, shot: Callable[[], bytes | None], parent=None):
        super().__init__(parent)
        self.step = dict(step)
        self.shot = shot
        t = step["t"]
        self.setWindowTitle(f"Шаг: {STEP_TYPES[t]}")
        form = QFormLayout()
        self.delay = _ms_spin(step.get("delay", 0))
        self.delay_rand = QCheckBox("случайно до")
        self.delay_rand.setToolTip("Каждый раз перед шагом пауза выбирается случайно в этом интервале")
        self.delay_max = _ms_spin(step.get("delay_max", step.get("delay", 0)))
        self.delay_rand.setChecked("delay_max" in step)
        self.delay_max.setEnabled(self.delay_rand.isChecked())
        self.delay_rand.toggled.connect(self._delay_rand_toggled)
        delay_row = QHBoxLayout()
        for w in (self.delay, self.delay_rand, self.delay_max):
            delay_row.addWidget(w)
        delay_row.addStretch(1)
        form.addRow("Пауза перед шагом", delay_row)

        self.x = self.y = self.in_place = None
        if t in ("click", "down", "up", "move", "wheel"):
            has_xy = step.get("x") is not None
            self.x = _pct_spin(step["x"] if has_xy else 0.5)
            self.y = _pct_spin(step["y"] if has_xy else 0.5)
            pick = QPushButton("Указать на снимке окна…")
            pick.clicked.connect(self._pick)
            xy = QHBoxLayout()
            xy.addWidget(QLabel("X"))
            xy.addWidget(self.x)
            xy.addWidget(QLabel("Y"))
            xy.addWidget(self.y)
            xy.addWidget(pick)
            form.addRow("Точка", xy)
            if t != "move":
                self.in_place = QCheckBox("на текущем месте курсора (для игры с захватом курсора)")
                self.in_place.setChecked(not has_xy)
                self.in_place.toggled.connect(self._sync_place)
                form.addRow("", self.in_place)
                self._sync_place()
        if t in ("click", "down", "up"):
            self.button = QComboBox()
            for k, v in BUTTONS.items():
                self.button.addItem(v, k)
            self.button.setCurrentIndex(list(BUTTONS).index(step.get("button", "left")))
            form.addRow("Кнопка", self.button)
        if t == "click":
            self.count = QComboBox()
            self.count.addItems(["одинарный", "двойной"])
            self.count.setCurrentIndex(1 if step.get("count") == 2 else 0)
            form.addRow("Клик", self.count)
        if t == "look":
            self.dx, self.dy = QDoubleSpinBox(), QDoubleSpinBox()
            for s, v in ((self.dx, step.get("dx", 0)), (self.dy, step.get("dy", 0))):
                s.setRange(-100_000, 100_000)
                s.setDecimals(1)
                s.setValue(v)
            form.addRow("По горизонтали (вправо +)", self.dx)
            form.addRow("По вертикали (вниз +)", self.dy)
            self.ms = _ms_spin(step.get("ms", 200), 600_000)
            form.addRow("За время", self.ms)
        if t == "wheel":
            self.wheel = QDoubleSpinBox()
            self.wheel.setRange(-100_000, 100_000)
            self.wheel.setDecimals(0)
            self.wheel.setValue(step.get("dy", 120))
            form.addRow("Прокрутка (120 — один щелчок вниз, −120 — вверх)", self.wheel)
        if t == "key":
            self.key = KeyCaptureEdit(step)
            self.action = QComboBox()
            for k, v in (("press", "нажать и отпустить"), ("down", "зажать"), ("up", "отпустить")):
                self.action.addItem(v, k)
            self.action.setCurrentIndex(["press", "down", "up"].index(step.get("action", "press")))
            self.ms = _ms_spin(step.get("ms", 50), 600_000)
            self.action.currentIndexChanged.connect(lambda _: self.ms.setEnabled(self.action.currentData() == "press"))
            self.ms.setEnabled(self.action.currentData() == "press")
            form.addRow("Клавиша", self.key)
            form.addRow("Действие", self.action)
            form.addRow("Держать", self.ms)
        if t == "text":
            self.text = QLineEdit(step.get("text", ""))
            form.addRow("Текст", self.text)
        if t == "wait":
            self.ms = _ms_spin(step.get("ms", 1000), 86_400_000)
            form.addRow("Длительность", self.ms)
        if t == "loop":
            self.times = QSpinBox()
            self.times.setRange(1, MAX_REPEAT)
            self.times.setValue(step.get("times", 2))
            form.addRow("Сколько раз выполнить блок", self.times)
            form.addRow("", QLabel("Блок — шаги между этим шагом и шагом «Конец блока»."))
        if t == "random":
            self._build_random(form, normalize_step(dict(step, t="random")))

        self.error = QLabel()
        self.error.setStyleSheet("color:#FF8B82")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Готово")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.error)
        lay.addWidget(buttons)

    def _delay_rand_toggled(self, on: bool) -> None:
        self.delay_max.setEnabled(on)
        if on and self.delay_max.value() <= self.delay.value():
            lo = self.delay.value()
            self.delay_max.setValue(max(lo + 100, lo * 2))  # заметный интервал для начала

    def _sync_place(self):
        on = not self.in_place.isChecked()
        self.x.setEnabled(on)
        self.y.setEnabled(on)

    # ------------------------------------------------------------ случайный ввод
    def _build_random(self, form: QFormLayout, step: dict) -> None:
        def pair(a: QWidget, b: QWidget, first: str = "от", second: str = "до") -> QHBoxLayout:
            row = QHBoxLayout()
            for w in (QLabel(first), a, QLabel(second), b):
                row.addWidget(w)
            row.addStretch(1)
            return row

        self.r_ms = _ms_spin(step["ms"], 86_400_000)
        form.addRow("Сколько длится", self.r_ms)
        self.r_min, self.r_max = _ms_spin(step["min_ms"], 600_000), _ms_spin(step["max_ms"], 600_000)
        for sp in (self.r_min, self.r_max):
            sp.setMinimum(1)
            sp.setSingleStep(10)
        form.addRow("Действие каждые", pair(self.r_min, self.r_max))

        kinds = QGridLayout()
        self.r_kinds = {}
        for i, (k, title) in enumerate(RANDOM_KINDS.items()):
            cb = QCheckBox(title)
            cb.setChecked(k in step["kinds"])
            cb.toggled.connect(self._sync_random)
            kinds.addWidget(cb, i // 3, i % 3)
            self.r_kinds[k] = cb
        form.addRow("Случайно выбирать из", kinds)

        self.r_where = QComboBox()
        self.r_where.addItem("во всём окне", "window")
        self.r_where.addItem("в выбранной области", "area")
        self.r_where.addItem("на месте курсора (игра с захватом курсора)", "cursor")
        area = step["area"]
        self.r_where.setCurrentIndex(2 if area is None else (0 if area == [0.0, 0.0, 1.0, 1.0] else 1))
        self.r_where.currentIndexChanged.connect(self._sync_random)
        form.addRow("Где действует мышь", self.r_where)
        self.r_area = [_pct_spin(v) for v in (area or [0.0, 0.0, 1.0, 1.0])]
        area_row = QHBoxLayout()
        for label, sp in zip(("X от", "до", "Y от", "до"), (self.r_area[0], self.r_area[2], self.r_area[1], self.r_area[3])):
            area_row.addWidget(QLabel(label))
            area_row.addWidget(sp)
        self.r_area_pick = QPushButton("Обвести на снимке окна…")
        self.r_area_pick.clicked.connect(self._pick_region)
        area_row.addWidget(self.r_area_pick)
        form.addRow("Область", area_row)

        buttons = QHBoxLayout()
        self.r_buttons = {}
        for b, title in BUTTONS.items():
            cb = QCheckBox(title)
            cb.setChecked(b in step["buttons"])
            buttons.addWidget(cb)
            self.r_buttons[b] = cb
        buttons.addStretch(1)
        form.addRow("Кнопки мыши", buttons)

        self.r_keys = KeyListEdit(step["keys"])
        clear = QPushButton("Очистить")
        clear.clicked.connect(self.r_keys.clear_keys)
        keys_row = QHBoxLayout()
        keys_row.addWidget(self.r_keys, 1)
        keys_row.addWidget(clear)
        form.addRow("Клавиши", keys_row)
        self.r_hold_min, self.r_hold_max = _ms_spin(step["hold_min"], 600_000), _ms_spin(step["hold_max"], 600_000)
        for sp in (self.r_hold_min, self.r_hold_max):
            sp.setSingleStep(10)
        form.addRow("Держать клавишу", pair(self.r_hold_min, self.r_hold_max))

        self.r_dx, self.r_dy = QDoubleSpinBox(), QDoubleSpinBox()
        for sp, v in ((self.r_dx, step["look_dx"]), (self.r_dy, step["look_dy"])):
            sp.setRange(0, 100_000)
            sp.setDecimals(0)
            sp.setValue(v)
        form.addRow("Поворот / сдвиг мыши до", pair(self.r_dx, self.r_dy, "±X", "±Y"))
        self.r_wheel = QDoubleSpinBox()
        self.r_wheel.setRange(1, 100_000)
        self.r_wheel.setDecimals(0)
        self.r_wheel.setValue(step["wheel"])
        form.addRow("Колесо до ±", self.r_wheel)
        hint = QLabel("Каждое действие выбирается случайно из отмеченных. Всё, что случайный ввод зажал, "
                      "он сам отпускает в конце.")
        hint.setWordWrap(True)
        hint.setObjectName("dim")
        form.addRow(hint)
        self._sync_random()

    def _checked_kinds(self) -> list[str]:
        return [k for k, cb in self.r_kinds.items() if cb.isChecked()]

    def _sync_random(self, *_):
        kinds = set(self._checked_kinds())
        where = self.r_where.currentData()
        pointer = bool(kinds & {"click", "down", "up", "move", "wheel"})
        self.r_where.setEnabled(pointer)
        for sp in self.r_area:
            sp.setEnabled(pointer and where == "area")
        self.r_area_pick.setEnabled(pointer and where == "area")
        for cb in self.r_buttons.values():
            cb.setEnabled(bool(kinds & {"click", "down", "up"}))
        for w in (self.r_keys, self.r_hold_min, self.r_hold_max):
            w.setEnabled("key" in kinds)
        for w in (self.r_dx, self.r_dy):
            w.setEnabled("look" in kinds or ("move" in kinds and where == "cursor"))
        self.r_wheel.setEnabled("wheel" in kinds)

    def _pick_region(self):
        png = None
        try:
            png = self.shot()
        except Exception as e:  # noqa: BLE001
            log.info("снимок для выбора области: %s", e)
        if not png:
            self.error.setText("Не удалось сделать снимок: выбранное окно не запущено.")
            return
        a = [sp.value() / 100 for sp in self.r_area]
        dlg = ShotPicker(png, None, self, mode="region", region=tuple(a))
        if dlg.exec() == QDialog.Accepted and dlg.region:
            for sp, v in zip(self.r_area, dlg.region):
                sp.setValue(v * 100)

    def _random_values(self) -> dict | None:
        kinds = self._checked_kinds()
        if not kinds:
            self.error.setText("Отметь хотя бы одно действие.")
            return None
        if "key" in kinds and not self.r_keys.keys:
            self.error.setText("Добавь клавиши в поле «Клавиши» или сними отметку «клавиша».")
            return None
        if kinds and set(kinds) & {"click", "down", "up"} and not any(cb.isChecked() for cb in self.r_buttons.values()):
            self.error.setText("Отметь хотя бы одну кнопку мыши.")
            return None
        where = self.r_where.currentData()
        area = [sp.value() / 100 for sp in self.r_area]
        if where == "area" and (abs(area[2] - area[0]) < 0.001 or abs(area[3] - area[1]) < 0.001):
            self.error.setText("Область пустая: задай её или обведи на снимке окна.")
            return None
        return {
            "ms": self.r_ms.value(), "min_ms": self.r_min.value(), "max_ms": self.r_max.value(),
            "kinds": kinds,
            "area": None if where == "cursor" else ([0.0, 0.0, 1.0, 1.0] if where == "window" else area),
            "buttons": [b for b, cb in self.r_buttons.items() if cb.isChecked()],
            "keys": self.r_keys.keys, "hold_min": self.r_hold_min.value(), "hold_max": self.r_hold_max.value(),
            "look_dx": self.r_dx.value(), "look_dy": self.r_dy.value(), "wheel": self.r_wheel.value(),
        }

    def _pick(self):
        png = None
        try:
            png = self.shot()
        except Exception as e:  # noqa: BLE001
            log.info("снимок для выбора точки: %s", e)
        if not png:
            self.error.setText("Не удалось сделать снимок: выбранное окно не запущено.")
            return
        mark = (self.x.value() / 100, self.y.value() / 100)
        dlg = ShotPicker(png, mark, self)
        if dlg.exec() == QDialog.Accepted and dlg.point:
            self.x.setValue(dlg.point[0] * 100)
            self.y.setValue(dlg.point[1] * 100)
            if self.in_place:
                self.in_place.setChecked(False)

    def _accept(self):
        t = self.step["t"]
        s = {"t": t, "delay": self.delay.value()}
        if self.delay_rand.isChecked():
            s["delay_max"] = self.delay_max.value()  # равные или перепутанные границы выправит normalize_step
        if self.x is not None:
            in_place = self.in_place is not None and self.in_place.isChecked()
            s["x"] = None if in_place else self.x.value() / 100
            s["y"] = None if in_place else self.y.value() / 100
        if t in ("click", "down", "up"):
            s["button"] = self.button.currentData()
        if t == "click":
            s["count"] = 2 if self.count.currentIndex() == 1 else 1
        if t == "look":
            s.update(dx=self.dx.value(), dy=self.dy.value(), ms=self.ms.value())
        if t == "wheel":
            s["dy"] = self.wheel.value()
        if t == "key":
            if not self.key.params:
                self.error.setText("Нажми нужную клавишу в поле «Клавиша».")
                return
            s.update(self.key.params, action=self.action.currentData(), ms=self.ms.value())
        if t == "text":
            s["text"] = self.text.text()
        if t == "wait":
            s["ms"] = self.ms.value()
        if t == "loop":
            s["times"] = self.times.value()
        if t == "random":
            values = self._random_values()
            if values is None:
                return
            s.update(values)
        try:
            self.step = normalize_step(s)
        except ValueError as e:
            self.error.setText(str(e))
            return
        self.accept()


# ------------------------------------------------------------------ окно «Макросы»

_NEW_STEP = {
    "click": {"t": "click", "x": 0.5, "y": 0.5},
    "down": {"t": "down", "x": None, "y": None},
    "up": {"t": "up", "x": None, "y": None},
    "move": {"t": "move", "x": 0.5, "y": 0.5},
    "look": {"t": "look", "dx": 100, "dy": 0, "ms": 200},
    "wheel": {"t": "wheel", "x": None, "y": None, "dy": 120},
    "key": {"t": "key", "action": "press", "ms": 50},
    "text": {"t": "text", "text": ""},
    "wait": {"t": "wait", "ms": 1000},
    "loop": {"t": "loop", "times": 2},
    "endloop": {"t": "endloop"},
    "random": {"t": "random", "ms": 3000, "min_ms": 50, "max_ms": 50, "kinds": ["click"], "area": [0.0, 0.0, 1.0, 1.0]},
}

STATE_TEXT = {
    "running": "идёт", "paused": "на паузе", "paused_user": "ждёт, пока ты закончишь",
    "done": "выполнен", "stopped": "остановлен", "error": "ошибка",
}


def status_text(st: dict | None) -> str:
    """Короткая строка состояния макроса для карточки окна и редактора."""
    if not st:
        return ""
    state = st.get("state", "")
    name = st.get("macro", "")
    if state == "running":
        count = "∞" if st.get("count") == 0 else st.get("count")
        return f"▶ «{name}» · шаг {st.get('step')}/{st.get('total')} · повтор {st.get('iteration')}/{count}"
    icon = {"paused": "❚❚", "paused_user": "❚❚", "done": "✓", "stopped": "■", "error": "⚠"}.get(state, "")
    text = f"{icon} «{name}» {STATE_TEXT.get(state, state)}"
    if state == "error" and st.get("reason"):
        text += f": {st['reason']}"
    return text


UNDO_LIMIT = 100
RUN_BG = QColor("#1E4D33")  # шаг, который сейчас выполняется


def steps_to_clipboard(steps: list[dict]) -> None:
    """Шаги в буфер обмена текстом JSON: их можно вставить в другой макрос или в другой набор.

    Только setText: QMimeData, созданный из Python, буфер удаляет при выходе уже после Python — программа падала.
    """
    QApplication.clipboard().setText(json.dumps({"panel6_steps": steps}, ensure_ascii=False))


def steps_from_clipboard() -> list[dict]:
    try:
        obj = json.loads(QApplication.clipboard().text())
    except ValueError:
        return []
    out = []
    for step in obj.get("panel6_steps", []) if isinstance(obj, dict) else []:
        try:
            out.append(normalize_step(step))
        except ValueError:
            pass
    return out


class StepsTable(QTableWidget):
    """Таблица шагов: клавиши редактора отдаются в on_key; если он их не взял — обычное поведение таблицы."""

    def __init__(self, on_key: Callable[[QKeyEvent], bool]):
        super().__init__(0, 3)
        self.on_key = on_key

    def keyPressEvent(self, e: QKeyEvent):  # noqa: N802
        if not self.on_key(e):
            super().keyPressEvent(e)


class MacrosDialog(QDialog):
    def __init__(self, ctl, parent=None):
        super().__init__(parent, Qt.Window)
        self.ctl = ctl
        self.current: dict | None = None
        self._loading = False
        self._self_edit = False
        self._hotkeys_dirty = False
        self._history: dict[str, tuple[list, list]] = {}  # id макроса -> (отмена, повтор)
        self._running_row = -1
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(400)
        self.save_timer.timeout.connect(self._save)

        # --- список макросов
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._select)
        b_new, b_copy, b_del = QPushButton("Новый"), QPushButton("Копия"), QPushButton("Удалить")
        b_new.clicked.connect(self._new)
        b_copy.clicked.connect(self._copy)
        b_del.clicked.connect(self._delete)
        self.preset_label = QLabel()
        self.preset_label.setObjectName("dim")
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.preset_label)
        ll.addWidget(self.list, 1)
        row = QHBoxLayout()
        for b in (b_new, b_copy, b_del):
            row.addWidget(b)
        ll.addLayout(row)

        # --- вкладка «Параметры»
        self.name = QLineEdit()
        self.name.textEdited.connect(self._edited_name)
        self.hotkey = HotkeyEdit("")
        self.hotkey.textChanged.connect(self._edited_props)
        self.target = QComboBox()
        self.target.addItem("в текущем окне", "current")
        self.target.addItem("во всех отмеченных окнах", "all")
        self.target.currentIndexChanged.connect(self._edited_props)
        self.count = QSpinBox()
        self.count.setRange(0, MAX_REPEAT)
        self.count.setSpecialValueText("до остановки")
        self.count.valueChanged.connect(self._edited_props)
        self.gap = _ms_spin(0, 86_400_000)
        self.gap.valueChanged.connect(self._edited_props)
        self.user_input = QComboBox()
        self.user_input.addItem("как в настройках", "default")
        for k, v in USER_INPUT_MODES.items():
            self.user_input.addItem(v, k)
        self.user_input.currentIndexChanged.connect(self._edited_props)
        props = QGridLayout()
        props.addWidget(QLabel("Название"), 0, 0)
        props.addWidget(self.name, 0, 1, 1, 3)
        props.addWidget(QLabel("Клавиша запуска"), 1, 0)
        props.addWidget(self.hotkey, 1, 1)
        props.addWidget(self.target, 1, 2, 1, 2)
        props.addWidget(QLabel("Повторов"), 2, 0)
        props.addWidget(self.count, 2, 1)
        props.addWidget(QLabel("Пауза между повторами"), 2, 2)
        props.addWidget(self.gap, 2, 3)
        props.addWidget(QLabel("Когда ты действуешь в окне"), 3, 0)
        props.addWidget(self.user_input, 3, 1, 1, 3)

        win_box = QGroupBox("Окна")
        wg = QGridLayout(win_box)
        wg.addWidget(QLabel("Запускать по кнопке «во всех окнах»"), 0, 1)
        wg.addWidget(QLabel("Повторов в этом окне"), 0, 2)
        wg.addWidget(QLabel("Пауза между повторами"), 0, 3)
        self.win_rows = []
        for i, wcfg in enumerate(ctl.cfg["windows"], start=1):
            on = QCheckBox(f"{i}. {wcfg['name']}" + ("" if wcfg.get("enabled", True) else " (выключено)"))
            cnt = QSpinBox()
            cnt.setRange(-1, MAX_REPEAT)
            cnt.setSpecialValueText("как у макроса")
            gap = QSpinBox()
            gap.setRange(-1, 86_400_000)
            gap.setSingleStep(100)
            gap.setSpecialValueText("как у макроса")
            for w in (on, cnt, gap):
                (w.toggled if w is on else w.valueChanged).connect(self._edited_props)
            wg.addWidget(on, i, 0, 1, 2)
            wg.addWidget(cnt, i, 2)
            wg.addWidget(gap, i, 3)
            self.win_rows.append((on, cnt, gap))
        params = QWidget()
        pl = QVBoxLayout(params)
        pl.addLayout(props)
        pl.addWidget(win_box)
        pl.addStretch(1)

        # --- вкладка «Шаги»
        self.table = StepsTable(self._table_key)
        self.table.setHorizontalHeaderLabels(["№", "Действие", "Пауза перед"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, self.table.fontMetrics().horizontalAdvance("▶ 9999") + 16)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.cellDoubleClicked.connect(lambda r, _c: self._edit_step(r))
        self.table.itemSelectionChanged.connect(self._update_tools)

        add = QToolButton()
        add.setText("Добавить шаг ▾")
        add.setPopupMode(QToolButton.InstantPopup)
        add.setMenu(self._add_menu(add))
        tools = QHBoxLayout()
        tools.addWidget(add)
        self.tool_buttons = {}
        for key, text, tip, fn in (
            ("edit", "Изменить", "Изменить шаг (Enter или двойной клик)", self._edit_current),
            ("dup", "Копия", "Копия выделенных шагов (Ctrl+D)", self._dup_steps),
            ("del", "Удалить", "Удалить выделенные шаги (Del)", self._del_steps),
            ("up", "↑", "Выше (Ctrl+↑)", lambda: self._move_steps(-1)),
            ("down", "↓", "Ниже (Ctrl+↓)", lambda: self._move_steps(+1)),
            ("undo", "↶", "Отменить (Ctrl+Z)", self._undo),
            ("redo", "↷", "Повторить (Ctrl+Y)", self._redo),
        ):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            if len(text) == 1:
                b.setObjectName("icon")
            tools.addWidget(b)
            self.tool_buttons[key] = b
        process = QToolButton()
        process.setText("Обработать ▾")
        process.setToolTip("Обработать запись: паузы, короткие паузы, мелкие повороты камеры.\n"
                           "Если выделено несколько шагов — только их, иначе весь макрос.")
        process.setPopupMode(QToolButton.InstantPopup)
        process.setMenu(self._process_menu(process))
        tools.addWidget(process)
        self.tool_note = ElidedLabel()
        self.tool_note.setObjectName("dim")
        tools.addWidget(self.tool_note, 1)
        self.blocks_warning = QLabel()
        self.blocks_warning.setStyleSheet("color:#FF8B82")
        self.blocks_warning.setWordWrap(True)
        steps_page = QWidget()
        sl = QVBoxLayout(steps_page)
        sl.addLayout(tools)
        sl.addWidget(self.table, 1)
        sl.addWidget(self.blocks_warning)
        keys_hint = QLabel("Выделение: Shift/Ctrl + клик. Del — удалить, Ctrl+C / Ctrl+X / Ctrl+V — копировать, "
                           "вырезать, вставить (и в другой макрос), Ctrl+D — копия, Ctrl+↑/↓ — переместить, "
                           "Enter — изменить, Ctrl+Z / Ctrl+Y — отменить и повторить. Правая кнопка — меню.")
        keys_hint.setWordWrap(True)
        keys_hint.setObjectName("dim")
        sl.addWidget(keys_hint)

        self.tabs = QTabWidget()
        self.tabs.addTab(steps_page, "Шаги")
        self.tabs.addTab(params, "Параметры")

        # --- запись и проверка
        self.window_pick = QComboBox()
        for i, wcfg in enumerate(ctl.cfg["windows"], start=1):
            if wcfg.get("enabled", True):
                self.window_pick.addItem(f"{i}. {wcfg['name']}", i)
        self.window_pick.currentIndexChanged.connect(self._window_picked)
        self.b_rec = QPushButton("● Записать")
        self.b_rec.clicked.connect(self._toggle_record)
        self.b_run = QPushButton("▶ Проверить")
        self.b_run.clicked.connect(lambda: self._run())
        self.b_run_from = QPushButton("▶ С выбранного шага")
        self.b_run_from.setToolTip("Первый проход начнётся с выделенного шага, следующие повторы — с начала")
        self.b_run_from.clicked.connect(self._run_from_selected)
        self.b_stop = QPushButton("■ Стоп")
        self.b_stop.clicked.connect(lambda: self.ctl.stop_macro(self._slot()))
        self.run_status = ElidedLabel()
        self.run_status.setObjectName("dim")
        rr = QHBoxLayout()
        rr.addWidget(QLabel("Окно:"))
        rr.addWidget(self.window_pick)
        rr.addWidget(self.b_rec)
        rr.addWidget(self.b_run)
        rr.addWidget(self.b_run_from)
        rr.addWidget(self.b_stop)
        rr.addWidget(self.run_status, 1)
        hint = QLabel("Запись: нажми «Записать», переключись в окно игры и действуй; остановить — этой же кнопкой "
                      f"или клавишей {ctl.cfg['hotkeys'].get('record') or '(не назначена)'}. "
                      "Шаги добавятся после выделенного шага. Идущий шаг подсвечивается зелёным.")
        hint.setWordWrap(True)
        hint.setObjectName("dim")

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.tabs, 1)
        rl.addLayout(rr)
        rl.addWidget(hint)
        self.editor = right

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([240, 860])
        lay = QVBoxLayout(self)
        lay.addWidget(split)
        self.resize(1150, 820)

        ctl.macros_changed.connect(self._macros_changed)
        ctl.recording_changed.connect(self._recording_changed)
        ctl.macro_status.connect(self._macro_status)
        self.refresh_list()

    # ------------------------------------------------------------ список
    def refresh_list(self, select_id: str | None = None) -> None:
        keep = select_id or (self.current["id"] if self.current else None)
        macros = self.ctl.preset["macros"]
        self.preset_label.setText(f"Набор «{self.ctl.preset['name']}»")
        self.setWindowTitle(f"Макросы — набор «{self.ctl.preset['name']}»")
        self.list.blockSignals(True)
        self.list.clear()
        select_row = 0
        for i, m in enumerate(macros):
            item = QListWidgetItem(self._item_text(m))
            item.setData(Qt.UserRole, m["id"])
            self.list.addItem(item)
            if m["id"] == keep:
                select_row = i
        if macros:
            self.list.setCurrentRow(select_row)
        self.list.blockSignals(False)
        found = macros[select_row] if macros else None
        if found is not self.current:
            self._load(found)

    @staticmethod
    def _item_text(m: dict) -> str:
        return m["name"] + (f"   [{m['hotkey']}]" if m["hotkey"] else "")

    def _select(self, item, _prev=None) -> None:
        if item is None:
            self._load(None)
            return
        self._load(self.ctl.macro(item.data(Qt.UserRole)))

    def _macros_changed(self) -> None:
        if not self._self_edit:
            self.refresh_list()

    def _new(self) -> None:
        m = new_macro(f"Макрос {len(self.ctl.preset['macros']) + 1}")
        self.ctl.preset["macros"].append(m)
        self._touch()
        self.refresh_list(select_id=m["id"])
        self.tabs.setCurrentIndex(1)
        self.name.setFocus()
        self.name.selectAll()

    def _copy(self) -> None:
        if not self.current:
            return
        m = clone_macro(self.current)
        self.ctl.preset["macros"].append(m)
        self._touch()
        self.refresh_list(select_id=m["id"])

    def _delete(self) -> None:
        if not self.current:
            return
        name = self.current["name"]
        if QMessageBox.question(self, "Удалить макрос", f"Удалить макрос «{name}»?") != QMessageBox.Yes:
            return
        mid = self.current["id"]
        self.ctl.preset["macros"] = [m for m in self.ctl.preset["macros"] if m["id"] != mid]
        for k, v in list(self.ctl.preset["window_macros"].items()):
            if v == mid:
                self.ctl.preset["window_macros"][k] = ""
        self._history.pop(mid, None)
        self.current = None
        self._touch(hotkeys=True)
        self.refresh_list()

    # ------------------------------------------------------------ параметры
    def _load(self, m: dict | None) -> None:
        self.current = m
        self._loading = True
        self.editor.setEnabled(m is not None)
        m = m or new_macro("")
        self.name.setText(m["name"])
        self.hotkey.setText(m["hotkey"])
        self.target.setCurrentIndex(1 if m["hotkey_target"] == "all" else 0)
        self.count.setValue(m["repeat"]["count"])
        self.gap.setValue(m["repeat"]["gap_ms"])
        self.user_input.setCurrentIndex(max(0, self.user_input.findData(m["user_input"])))
        for i, (on, cnt, gap) in enumerate(self.win_rows, start=1):
            ov = m["overrides"].get(str(i), {})
            on.setChecked(i in m["windows"])
            cnt.setValue(ov.get("count", -1))
            gap.setValue(ov.get("gap_ms", -1))
        self._loading = False
        self._running_row = -1
        self.tool_note.setText("")
        self._fill_steps()
        self._window_picked()

    def _edited_name(self, text: str) -> None:
        if self._loading or not self.current:
            return
        self.current["name"] = text.strip() or "Макрос"
        item = self.list.currentItem()
        if item:
            item.setText(self._item_text(self.current))
        self._touch()

    def _edited_props(self, *_):
        if self._loading or not self.current:
            return
        m = self.current
        hotkey_before = m["hotkey"]
        m["hotkey"] = self.hotkey.text()
        m["hotkey_target"] = self.target.currentData()
        m["repeat"] = {"count": self.count.value(), "gap_ms": self.gap.value()}
        m["user_input"] = self.user_input.currentData()
        n = len(self.win_rows)
        # окна, которых сейчас нет в настройках (их число уменьшили), остаются как были
        m["windows"] = [i for i, (on, _c, _g) in enumerate(self.win_rows, start=1) if on.isChecked()] + \
            [w for w in m["windows"] if w > n]
        overrides = {k: v for k, v in m["overrides"].items() if int(k) > n}
        for i, (_on, cnt, gap) in enumerate(self.win_rows, start=1):
            o = {}
            if cnt.value() >= 0:
                o["count"] = cnt.value()
            if gap.value() >= 0:
                o["gap_ms"] = gap.value()
            if o:
                overrides[str(i)] = o
        m["overrides"] = overrides
        item = self.list.currentItem()
        if item:
            item.setText(self._item_text(m))
        self._touch(hotkeys=m["hotkey"] != hotkey_before)

    def _touch(self, hotkeys: bool = False) -> None:
        self._hotkeys_dirty |= hotkeys
        self.save_timer.start()

    def _save(self) -> None:
        self._self_edit = True
        try:
            self.ctl.preset_edited()
            if self._hotkeys_dirty:
                self._hotkeys_dirty = False
                self.ctl.start_hotkeys()
        finally:
            self._self_edit = False

    def flush(self) -> None:
        if self.save_timer.isActive():
            self.save_timer.stop()
            self._save()

    def closeEvent(self, e):  # noqa: N802
        self.flush()
        super().closeEvent(e)

    # ------------------------------------------------------------ таблица шагов
    def _steps(self) -> list[dict]:
        return self.current["steps"] if self.current else []

    def _fill_steps(self, select=None) -> None:
        """Перерисовать таблицу; select — номер строки или список строк, которые выделить."""
        steps = self._steps()
        self.table.blockSignals(True)
        self.table.setRowCount(len(steps))
        depth = 0
        for i, s in enumerate(steps):
            if s["t"] == "endloop":
                depth = max(0, depth - 1)
            cells = [str(i + 1), "      " * depth + describe(s), delay_text(s)]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c != 1:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(i, c, item)
            if s["t"] == "loop":
                depth += 1
        self.table.blockSignals(False)
        if self._running_row >= len(steps):
            self._running_row = -1
        self._paint_row(self._running_row)
        if select is not None:
            self._select_rows([select] if isinstance(select, int) else list(select))
        errors = check_blocks(steps)
        self.blocks_warning.setText("Повторы: " + "; ".join(errors) if errors else "")
        self._update_tools()

    def _rows(self) -> list[int]:
        sm = self.table.selectionModel()
        return sorted({i.row() for i in sm.selectedRows()}) if sm else []

    def _row(self) -> int:
        rows = self._rows()
        return rows[0] if rows else -1

    def _select_rows(self, rows: list[int]) -> None:
        n = self.table.rowCount()
        rows = [r for r in rows if 0 <= r < n]
        self.table.clearSelection()
        if not rows:
            self._update_tools()
            return
        model = self.table.model()
        sel = QItemSelection()
        for r in rows:
            sel.select(model.index(r, 0), model.index(r, 2))
        sm = self.table.selectionModel()
        sm.setCurrentIndex(model.index(rows[-1], 0), QItemSelectionModel.NoUpdate)
        sm.select(sel, QItemSelectionModel.Select | QItemSelectionModel.Rows)
        self.table.scrollToItem(self.table.item(rows[-1], 0))
        self._update_tools()

    def _insert_pos(self) -> int:
        rows = self._rows()
        return rows[-1] + 1 if rows else len(self._steps())

    def _update_tools(self) -> None:
        rows = self._rows()
        has = bool(rows) and self.current is not None
        for key in ("dup", "del", "up", "down"):
            self.tool_buttons[key].setEnabled(has)
        self.tool_buttons["edit"].setEnabled(len(rows) == 1)
        undo, redo = self._history.get(self.current["id"], ([], [])) if self.current else ([], [])
        self.tool_buttons["undo"].setEnabled(bool(undo))
        self.tool_buttons["redo"].setEnabled(bool(redo))
        self.b_run_from.setEnabled(has)
        self.b_run_from.setText(f"▶ С шага {rows[0] + 1}" if has else "▶ С выбранного шага")

    def _table_key(self, e: QKeyEvent) -> bool:
        """Клавиши таблицы шагов. Буквы сравниваются по клавише, а не по символу — работают и в русской раскладке."""
        if not self.current:
            return False
        mods = e.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier | Qt.AltModifier)
        k = e.key()
        if mods == Qt.NoModifier and k in (Qt.Key_Delete, Qt.Key_Backspace):
            self._del_steps()
        elif mods == Qt.NoModifier and k in (Qt.Key_Return, Qt.Key_Enter):
            self._edit_current()
        elif mods == Qt.ControlModifier and k in (Qt.Key_Up, Qt.Key_Down):
            self._move_steps(-1 if k == Qt.Key_Up else +1)
        elif mods & Qt.ControlModifier and not mods & Qt.AltModifier:
            fn = {
                ord("C"): self._copy_steps, ord("X"): self._cut_steps, ord("V"): self._paste_steps,
                ord("D"): self._dup_steps, ord("Y"): self._redo,
                ord("Z"): self._redo if mods & Qt.ShiftModifier else self._undo,
            }.get(event_vk(e))
            if fn is None:
                return False
            fn()
        else:
            return False
        return True

    def _table_menu(self, pos) -> None:
        menu = self._build_table_menu(pos)
        if menu is not None:
            menu.exec(self.table.viewport().mapToGlobal(pos))
            menu.deleteLater()

    def _build_table_menu(self, pos) -> QMenu | None:
        """Меню по правой кнопке; строка под курсором выделяется, если она не была выделена."""
        if not self.current:
            return None
        row = self.table.rowAt(pos.y())
        if row >= 0 and row not in self._rows():
            self._select_rows([row])
        rows = self._rows()
        undo, redo = self._history.get(self.current["id"], ([], []))
        menu = QMenu(self)

        def item(text: str, fn, keys: str = "", enabled: bool = True) -> None:
            act = menu.addAction(text)
            act.triggered.connect(lambda _=False: fn())
            act.setEnabled(enabled)
            if keys:
                act.setShortcut(QKeySequence(keys))

        item("Изменить…", self._edit_current, "Return", len(rows) == 1)
        menu.addMenu(self._add_menu(menu))
        menu.addSeparator()
        item("Копировать", self._copy_steps, "Ctrl+C", bool(rows))
        item("Вырезать", self._cut_steps, "Ctrl+X", bool(rows))
        item("Вставить", self._paste_steps, "Ctrl+V", bool(steps_from_clipboard()))
        item("Копия шагов", self._dup_steps, "Ctrl+D", bool(rows))
        item("Удалить", self._del_steps, "Del", bool(rows))
        menu.addSeparator()
        item("Выше", lambda: self._move_steps(-1), "Ctrl+Up", bool(rows))
        item("Ниже", lambda: self._move_steps(+1), "Ctrl+Down", bool(rows))
        menu.addSeparator()
        if rows:
            item(f"▶ Проверить с шага {rows[0] + 1}", self._run_from_selected)
        menu.addMenu(self._process_menu(menu))
        menu.addSeparator()
        item("Отменить", self._undo, "Ctrl+Z", bool(undo))
        item("Повторить", self._redo, "Ctrl+Y", bool(redo))
        return menu

    def _add_menu(self, parent) -> QMenu:
        menu = QMenu("Добавить шаг", parent)
        for t, title in STEP_TYPES.items():
            if t == "random":
                menu.addSeparator()
            act = menu.addAction(title)
            act.triggered.connect(lambda _=False, t=t: self._add_step(t))
        return menu

    def _process_menu(self, parent) -> QMenu:
        menu = QMenu("Обработать запись", parent)
        for text, fn in (
            ("Паузы быстрее ×0,5", lambda: self._process("scale", 0.5)),
            ("Паузы медленнее ×2", lambda: self._process("scale", 2.0)),
            ("Паузы × свой множитель…", self._process_scale_ask),
            ("Паузы перед шагами случайно ±N %…", self._process_jitter_ask),
            (None, None),
            ("Убрать паузы короче…", self._process_drop_ask),
            ("Склеить мелкие повороты камеры", lambda: self._process("merge", 100)),
        ):
            if text is None:
                menu.addSeparator()
                continue
            act = menu.addAction(text)
            act.triggered.connect(lambda _=False, fn=fn: fn())
        return menu

    # ------------------------------------------------------------ изменение шагов
    def _snapshot(self) -> None:
        """Запомнить шаги перед изменением — для «Отменить»."""
        undo, redo = self._history.setdefault(self.current["id"], ([], []))
        undo.append(copy.deepcopy(self.current["steps"]))
        del undo[:-UNDO_LIMIT]
        redo.clear()

    def _changed(self, select=None) -> None:
        self._fill_steps(select=select)
        self._touch()

    def _undo(self) -> None:
        self._restore(0)

    def _redo(self) -> None:
        self._restore(1)

    def _restore(self, which: int) -> None:
        if not self.current:
            return
        stacks = self._history.get(self.current["id"])
        if not stacks or not stacks[which]:
            return
        stacks[1 - which].append(copy.deepcopy(self.current["steps"]))
        keep = self._row()
        self.current["steps"][:] = stacks[which].pop()
        self._changed(select=min(keep, len(self.current["steps"]) - 1) if keep >= 0 else None)
        self.tool_note.setText("Отменено" if which == 0 else "Повторено")

    def _shot(self) -> bytes | None:
        if not self.ctl.is_chrome(self._slot()):
            return None
        w = self.ctl.windows[self._slot() - 1]
        if not w.connected:
            return None
        return w.screenshot()

    def _slot(self) -> int:
        return self.window_pick.currentData() or 0

    def _add_step(self, t: str) -> None:
        if not self.current:
            return
        # у новой клавиши ещё нет кода, поэтому её шаг проверяется уже после ввода в диалоге
        step = dict(_NEW_STEP[t], delay=0) if t == "key" else normalize_step(_NEW_STEP[t])
        if t != "endloop":
            dlg = StepDialog(step, self._shot, self)
            if dlg.exec() != QDialog.Accepted:
                return
            step = dlg.step
        pos = self._insert_pos()
        self._snapshot()
        self.current["steps"].insert(pos, step)
        self._changed(select=pos)

    def _edit_current(self) -> None:
        rows = self._rows()
        if len(rows) == 1:
            self._edit_step(rows[0])

    def _edit_step(self, row: int) -> None:
        if not self.current or not 0 <= row < len(self.current["steps"]):
            return
        dlg = StepDialog(self.current["steps"][row], self._shot, self)
        if dlg.exec() == QDialog.Accepted and dlg.step != self.current["steps"][row]:
            self._snapshot()
            self.current["steps"][row] = dlg.step
            self._changed(select=row)

    def _dup_steps(self) -> None:
        rows = self._rows()
        if not self.current or not rows:
            return
        copies = [copy.deepcopy(self.current["steps"][r]) for r in rows]
        pos = rows[-1] + 1
        self._snapshot()
        self.current["steps"][pos:pos] = copies
        self._changed(select=range(pos, pos + len(copies)))

    def _del_steps(self) -> None:
        rows = self._rows()
        if not self.current or not rows:
            return
        self._snapshot()
        for r in reversed(rows):
            del self.current["steps"][r]
        self._changed(select=min(rows[0], len(self.current["steps"]) - 1))
        self.tool_note.setText(f"Удалено шагов: {len(rows)} (Ctrl+Z — вернуть)")

    def _move_steps(self, delta: int) -> None:
        rows = self._rows()
        steps = self._steps()
        if not rows or (delta < 0 and rows[0] == 0) or (delta > 0 and rows[-1] == len(steps) - 1):
            return
        self._snapshot()
        for r in (rows if delta < 0 else reversed(rows)):
            steps[r], steps[r + delta] = steps[r + delta], steps[r]
        self._changed(select=[r + delta for r in rows])

    def _copy_steps(self) -> None:
        rows = self._rows()
        if self.current and rows:
            steps_to_clipboard([self.current["steps"][r] for r in rows])
            self.tool_note.setText(f"Скопировано шагов: {len(rows)}")

    def _cut_steps(self) -> None:
        if self._rows():
            self._copy_steps()
            self._del_steps()

    def _paste_steps(self) -> None:
        steps = steps_from_clipboard()
        if not self.current or not steps:
            return
        pos = self._insert_pos()
        self._snapshot()
        self.current["steps"][pos:pos] = steps
        self._changed(select=range(pos, pos + len(steps)))
        self.tool_note.setText(f"Вставлено шагов: {len(steps)}")

    # ------------------------------------------------------------ обработка записи
    def _process_scale_ask(self) -> None:
        factor, ok = QInputDialog.getDouble(self, "Паузы", "Умножить паузы на:", 1.5, 0.05, 20.0, 2)
        if ok:
            self._process("scale", factor)

    def _process_jitter_ask(self) -> None:
        percent, ok = QInputDialog.getInt(self, "Случайные паузы", "Пауза перед шагом будет случайной в пределах ±, %:",
                                          30, 1, 100, 5)
        if ok:
            self._process("jitter", percent)

    def _process_drop_ask(self) -> None:
        ms, ok = QInputDialog.getInt(self, "Короткие паузы", "Убрать паузы короче, мс:", 50, 1, 60_000, 10)
        if ok:
            self._process("drop", ms)

    def _process(self, kind: str, value: float) -> None:
        """Обработка выделенных шагов (если выделено несколько) или всего макроса."""
        if not self.current:
            return
        steps = self.current["steps"]
        rows = self._rows()
        lo, hi = (rows[0], rows[-1] + 1) if len(rows) > 1 else (0, len(steps))
        where = f"шаги {lo + 1}–{hi}" if len(rows) > 1 else "весь макрос"
        part = steps[lo:hi]
        if kind == "scale":
            new, n = scale_pauses(part, value)
            text = f"паузы ×{value:g}: изменено шагов {n}"
        elif kind == "jitter":
            new, n = jitter_pauses(part, int(value))
            text = f"паузы перед шагами стали случайными ±{int(value)} %: изменено шагов {n}"
        elif kind == "drop":
            new, n = drop_short_pauses(part, int(value))
            text = f"паузы короче {int(value)} мс убраны: изменено шагов {n}"
        else:
            new, n = merge_looks(part, int(value))
            text = f"склеено поворотов камеры: {n}"
        if not n:
            self.tool_note.setText(f"{where.capitalize()}: менять нечего")
            return
        self._snapshot()
        steps[lo:hi] = new
        self._changed(select=range(lo, lo + len(new)) if len(rows) > 1 else None)
        size = f" Шагов: {len(part)} → {len(new)}." if len(new) != len(part) else ""
        self.tool_note.setText(f"{where.capitalize()}: {text}.{size} Ctrl+Z — отменить")

    # ------------------------------------------------------------ запись и проверка
    def _toggle_record(self) -> None:
        if self.ctl.recorder:
            self.ctl.stop_recording()
            return
        if not self.current:
            self._new()
        self.ctl.start_recording(self._slot(), sink=self._recorded)

    def _recorded(self, steps: list[dict]) -> None:
        if self.current is None or self.ctl.macro(self.current["id"]) is None:
            m = new_macro("Запись")
            self.ctl.preset["macros"].append(m)
            self.refresh_list(select_id=m["id"])
        pos = self._insert_pos()
        self._snapshot()
        self.current["steps"][pos:pos] = steps
        self._changed(select=range(pos, pos + len(steps)))
        self.tabs.setCurrentIndex(0)
        self.run_status.setText(f"Записано шагов: {len(steps)}")

    def _recording_changed(self, slot: int, count: int) -> None:
        if slot:
            self.b_rec.setText(f"■ Остановить запись ({count})")
            self.b_rec.setStyleSheet("color:#FF8B82")
        else:
            self.b_rec.setText("● Записать")
            self.b_rec.setStyleSheet("")

    def _run(self, start: int = 0) -> None:
        if not self.current:
            return
        self.flush()
        self.ctl.run_macro(self._slot(), self.current["id"], start=start)

    def _run_from_selected(self) -> None:
        rows = self._rows()
        if rows:
            self._run(start=rows[0])

    def _window_picked(self, *_) -> None:
        st = self.ctl.macro_states.get(self._slot())
        self.run_status.setText(status_text(st))
        self._show_running(st)

    def _macro_status(self, slot: int, st: dict) -> None:
        if slot == self._slot():
            self.run_status.setText(status_text(st))
            self._show_running(st)

    def _show_running(self, st: dict | None) -> None:
        """Подсветить шаг, который сейчас выполняется в выбранном окне."""
        row = -1
        if (st and self.current and st.get("macro_id") == self.current["id"]
                and st.get("state") in ("running", "paused", "paused_user")):
            row = st.get("index", -1)
        if row >= self.table.rowCount():
            row = -1
        if row == self._running_row:
            return
        old, self._running_row = self._running_row, row
        self._paint_row(old)
        self._paint_row(row)
        if row >= 0:
            self.table.scrollToItem(self.table.item(row, 0))

    def _paint_row(self, row: int) -> None:
        if not 0 <= row < self.table.rowCount():
            return
        running = row == self._running_row
        for c in range(self.table.columnCount()):
            item = self.table.item(row, c)
            if item is not None:
                item.setBackground(QBrush(RUN_BG) if running else QBrush())
        num = self.table.item(row, 0)
        if num is not None:
            num.setText(f"▶ {row + 1}" if running else str(row + 1))
