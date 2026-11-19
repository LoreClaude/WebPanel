"""Главное окно панели (обычно на втором мониторе), настройки и значок в трее."""

from __future__ import annotations

import copy
import logging
import math
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPalette, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QProxyStyle, QPushButton, QScrollArea, QSpinBox, QStyle, QSystemTrayIcon, QTabWidget, QToolButton, QVBoxLayout,
    QWidget,
)

from . import VERSION, config, win32
from .badge import CORNERS, Badge
from .controller import Controller, ordered_screens
from .grid import GridOverlay
from .macro_ui import MacrosDialog, status_text
from .macros import USER_INPUT_MODES
from .netguard import parse_allowed
from .thumbs import ThumbHost
from .widgets import ElidedLabel, EventLog, HotkeyEdit

log = logging.getLogger(__name__)

STYLE = """
QWidget { background:#0E1318; color:#E4EBF2; font-size:10pt; }
QLabel { background:transparent; }
QFrame#card { background:#151C23; border:1px solid #26313C; border-radius:8px; }
QFrame#card[current="true"] { border:2px solid #7FA3FF; }
QFrame#thumb { background:#080C10; border-radius:4px; }
QLabel#title { font-weight:600; font-size:11pt; }
QLabel#dim { color:#97A5B4; }
QPushButton, QToolButton { background:#19222B; border:1px solid #3B4957; border-radius:6px; padding:6px 12px; }
QPushButton:hover, QToolButton:hover { border-color:#7FA3FF; }
QPushButton:disabled { color:#5C6B7A; border-color:#26313C; }
QPushButton#primary { background:#2350D8; border-color:#2350D8; color:white; font-weight:600; }
QPushButton#icon { padding:4px 8px; min-width:22px; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
  background:#19222B; border:1px solid #3B4957; border-radius:6px; padding:4px 6px; min-height:20px; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled { color:#5C6B7A; border-color:#26313C; }
QSpinBox, QDoubleSpinBox { padding-right:2px; }
QSpinBox::up-button, QDoubleSpinBox::up-button { subcontrol-origin:border; subcontrol-position:top right; width:20px;
  border-left:1px solid #3B4957; border-top-right-radius:6px; background:#1E2832; }
QSpinBox::down-button, QDoubleSpinBox::down-button { subcontrol-origin:border; subcontrol-position:bottom right;
  width:20px; border-left:1px solid #3B4957; border-bottom-right-radius:6px; background:#1E2832; }
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover { background:#24365E; }
QComboBox { padding-right:24px; }
QComboBox::drop-down { subcontrol-origin:padding; subcontrol-position:top right; width:22px;
  border-left:1px solid #3B4957; }
QComboBox QAbstractItemView { background:#151C23; border:1px solid #3B4957; selection-background-color:#24365E;
  outline:0; }
QToolButton { padding-right:12px; }
QToolButton::menu-indicator { image:none; width:0px; }
QScrollBar:vertical { background:#11171D; width:12px; margin:0; border:none; }
QScrollBar::handle:vertical { background:#3B4957; min-height:30px; border-radius:5px; margin:2px; }
QScrollBar:horizontal { background:#11171D; height:12px; margin:0; border:none; }
QScrollBar::handle:horizontal { background:#3B4957; min-width:30px; border-radius:5px; margin:2px; }
QScrollBar::handle:hover { background:#4E5F70; }
QScrollBar::add-line, QScrollBar::sub-line { width:0; height:0; border:none; background:none; }
QScrollBar::add-page, QScrollBar::sub-page { background:none; }
QToolTip { background:#151C23; color:#E4EBF2; border:1px solid #3B4957; padding:4px; }
QTableCornerButton::section { background:#151C23; border:0; }
QListWidget, QTableWidget { background:#11171D; border:1px solid #26313C; border-radius:6px;
  alternate-background-color:#151C23; selection-background-color:#24365E; }
QHeaderView::section { background:#151C23; color:#97A5B4; border:0; padding:4px 6px; }
QGroupBox { border:1px solid #26313C; border-radius:6px; margin-top:10px; padding-top:6px; }
QGroupBox::title { subcontrol-origin:margin; left:8px; color:#97A5B4; }
QTabWidget::pane { border:1px solid #26313C; }
QTabBar::tab { background:#151C23; padding:6px 12px; }
QTabBar::tab:selected { background:#19222B; color:#7FA3FF; }
QCheckBox::indicator { width:14px; height:14px; border:1px solid #5C6B7A; border-radius:3px; background:#19222B; }
QCheckBox::indicator:checked { background:#2350D8; border-color:#7FA3FF; }
QMenu { background:#151C23; border:1px solid #3B4957; }
QMenu::item:selected { background:#24365E; }
"""


class _Style(QProxyStyle):
    """Fusion считает ширину поля со счётчиком без отступов и кнопок из STYLE, и число обрезалось."""

    SPIN_EXTRA = 2 + 6 + 2 + 21 + 4  # рамка, отступы слева и справа, кнопки ▲▼ с линией, поля строки ввода

    def sizeFromContents(self, ct, opt, size, widget=None):
        hint = super().sizeFromContents(ct, opt, size, widget)
        if ct == QStyle.CT_SpinBox:
            hint.setWidth(size.width() + self.SPIN_EXTRA)  # size — ширина самого длинного числа
        return hint


def _arrow_images() -> dict[str, str]:
    """Стрелки для полей со счётчиком и списков: рисуются один раз в папку темы."""
    folder = config.app_dir() / "theme"
    folder.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, color, up in (("up", "#B8C4D0", True), ("down", "#B8C4D0", False),
                            ("up_off", "#4E5B69", True), ("down_off", "#4E5B69", False)):
        pm = QPixmap(18, 10)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        pts = [QPointF(1, 9), QPointF(9, 1), QPointF(17, 9)] if up else [QPointF(1, 1), QPointF(9, 9), QPointF(17, 1)]
        p.drawPolygon(QPolygonF(pts))
        p.end()
        path = folder / f"{name}.png"
        pm.save(str(path))
        out[name] = path.as_posix()
    return out


def apply_theme(app: QApplication) -> None:
    """Тёмная тема одинаково на всех системах: стиль Fusion + палитра + таблица стилей.

    Родной стиль Windows плохо дружит с таблицей стилей: поля со счётчиком получались слишком низкими.
    """
    app.setStyle(_Style("Fusion"))
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark)  # тёмные заголовки окон Windows
    except Exception:  # noqa: BLE001
        pass
    pal = QPalette()
    for role, color in (
        (QPalette.Window, "#0E1318"), (QPalette.WindowText, "#E4EBF2"), (QPalette.Base, "#19222B"),
        (QPalette.AlternateBase, "#151C23"), (QPalette.Text, "#E4EBF2"), (QPalette.Button, "#19222B"),
        (QPalette.ButtonText, "#E4EBF2"), (QPalette.Highlight, "#24365E"), (QPalette.HighlightedText, "#FFFFFF"),
        (QPalette.ToolTipBase, "#151C23"), (QPalette.ToolTipText, "#E4EBF2"), (QPalette.PlaceholderText, "#7C8A99"),
        (QPalette.Link, "#7FA3FF"),
    ):
        pal.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor("#5C6B7A"))
    app.setPalette(pal)
    try:
        a = _arrow_images()
        arrows = f"""
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image:url("{a['up']}"); width:9px; height:5px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow, QComboBox::down-arrow {{ image:url("{a['down']}"); width:9px; height:5px; }}
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled {{ image:url("{a['up_off']}"); }}
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled, QComboBox::down-arrow:disabled {{ image:url("{a['down_off']}"); }}
"""
    except Exception:  # noqa: BLE001
        log.exception("стрелки темы")
        arrows = ""
    app.setStyleSheet(STYLE + arrows)


STATUS_TEXT = {"off": "не запущено", "starting": "запускается…", "ready": "работает", "missing": "окно не найдено"}
STATUS_COLOR = {"off": "#97A5B4", "starting": "#E5A84E", "ready": "#5CC48B", "missing": "#FF8B82"}


def app_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    for i in range(6):
        c = QColor("#7FA3FF") if i < 5 else QColor("#5CC48B")
        p.setBrush(c)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(4 + (i % 3) * 20, 14 + (i // 3) * 20, 16, 16, 3, 3)
    p.end()
    return QIcon(pm)


def _icon_button(text: str, tip: str) -> QPushButton:
    b = QPushButton(text)
    b.setObjectName("icon")
    b.setToolTip(tip)
    return b


def fill_macro_combo(combo: QComboBox, macros: list[dict], selected: str, empty: str) -> None:
    combo.blockSignals(True)
    combo.clear()
    combo.addItem(empty, "")
    for m in macros:
        combo.addItem(m["name"], m["id"])
    idx = combo.findData(selected)
    combo.setCurrentIndex(idx if idx >= 0 else 0)
    combo.blockSignals(False)


# ------------------------------------------------------------------ карточка окна

class SlotCard(QFrame):
    def __init__(self, slot: int, win: "MainWindow"):
        super().__init__()
        self.slot = slot
        self.win = win
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 10)
        head = QHBoxLayout()
        self.title = ElidedLabel()
        self.title.setObjectName("title")
        self.hotkey = QLabel()
        self.hotkey.setObjectName("dim")
        head.addWidget(self.title, 1)
        head.addWidget(self.hotkey)
        lay.addLayout(head)
        self.thumb = QFrame()
        self.thumb.setObjectName("thumb")
        self.thumb.setMinimumHeight(110)
        self.placeholder = QLabel("", self.thumb)
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setObjectName("dim")
        lay.addWidget(self.thumb, 1)
        row = QHBoxLayout()
        self.status = QLabel()
        row.addWidget(self.status, 1)
        self.btn_show = QPushButton("Показать")
        self.btn_show.clicked.connect(lambda: win.ctl.activate(slot))
        row.addWidget(self.btn_show)
        self.chrome = win.ctl.is_chrome(slot)
        if self.chrome:
            self.btn_launch = QPushButton("Запустить")
            self.btn_launch.clicked.connect(lambda: win.ctl.launch(slot))
            self.btn_full = QPushButton("⛶")
            self.btn_full.setToolTip("Снова развернуть на весь игровой монитор")
            self.btn_full.clicked.connect(lambda: win.ctl.fullscreen(slot))
            row.addWidget(self.btn_launch)
            row.addWidget(self.btn_full)
        lay.addLayout(row)
        self.macro_combo = None
        self.macro_line = ElidedLabel()  # текст ошибки макроса бывает длинным
        self.macro_line.setObjectName("dim")
        if self.chrome:
            mrow = QHBoxLayout()
            self.macro_combo = QComboBox()
            self.macro_combo.setToolTip("Основной макрос окна: запускается кнопкой ▶ и клавишей старт/пауза")
            self.macro_combo.currentIndexChanged.connect(
                lambda _: win.ctl.set_main_macro(slot, self.macro_combo.currentData() or ""))
            b_run = _icon_button("▶", "Запустить основной макрос в этом окне")
            b_run.clicked.connect(self._run)
            b_pause = _icon_button("❚❚", "Пауза / продолжить")
            b_pause.clicked.connect(lambda: win.ctl.toggle_pause(slot))
            b_stop = _icon_button("■", "Остановить макрос в этом окне")
            b_stop.clicked.connect(lambda: win.ctl.stop_macro(slot))
            mrow.addWidget(self.macro_combo, 1)
            for b in (b_run, b_pause, b_stop):
                mrow.addWidget(b)
            lay.addLayout(mrow)
            lay.addWidget(self.macro_line)
        self.thumb.installEventFilter(self)

    def _run(self) -> None:
        mid = self.macro_combo.currentData() if self.macro_combo else ""
        if not mid:
            self.win.ctl.message.emit(f"{self.win.ctl.slot_name(self.slot)}: выбери макрос в списке на карточке")
            return
        self.win.ctl.run_macro(self.slot, mid)

    def eventFilter(self, obj, ev):  # noqa: N802
        if obj is self.thumb and ev.type() == QEvent.Resize:
            self.placeholder.setGeometry(self.thumb.rect())
            self.win.schedule_thumbs()
        if obj is self.thumb and ev.type() == QEvent.MouseButtonDblClick:
            self.win.ctl.activate(self.slot)
        return False

    def refresh(self, status: str, current: bool) -> None:
        ctl = self.win.ctl
        self.title.setText(f"{self.slot}. {ctl.slot_name(self.slot)}")
        self.hotkey.setText(ctl.slot_hotkey(self.slot))
        if ctl.is_desktop(self.slot):
            text, color = "рабочий стол", STATUS_COLOR["ready"]
        else:
            text, color = STATUS_TEXT.get(status, status), STATUS_COLOR.get(status, "#97A5B4")
        self.status.setText(f"● {text}")
        self.status.setStyleSheet(f"color:{color}")
        if self.chrome:
            self.btn_launch.setText("Запустить" if status == "off" else "Запущено")
            self.btn_launch.setEnabled(status == "off")
            self.btn_full.setEnabled(status == "ready")
        self.placeholder.setText("" if self.win.host.has(self.slot) else
                                 ("Рабочий стол" if ctl.is_desktop(self.slot) else STATUS_TEXT.get(status, "")))
        self.setProperty("current", current)
        self.style().unpolish(self)
        self.style().polish(self)
        self.refresh_macro()

    def refresh_macro(self) -> None:
        if not self.chrome:
            return
        ctl = self.win.ctl
        if ctl.recording_slot == self.slot:
            self.macro_line.setText(f"● идёт запись: {ctl.recording_count} действий")
            self.macro_line.setStyleSheet("color:#FF8B82")
            return
        self.macro_line.setStyleSheet("")
        self.macro_line.setText(status_text(ctl.macro_states.get(self.slot)) or "макрос не запущен")

    def refresh_macro_list(self) -> None:
        if self.macro_combo is not None:
            ctl = self.win.ctl
            fill_macro_combo(self.macro_combo, ctl.preset["macros"], ctl.main_macro_id(self.slot),
                             "— основной макрос —")


# ------------------------------------------------------------------ главное окно

class MainWindow(QMainWindow):
    def __init__(self, ctl: Controller):
        super().__init__()
        self.ctl = ctl
        self.setWindowTitle(f"Panel6 {VERSION}")
        self.setWindowIcon(app_icon())
        self.host = ThumbHost(self)
        self.grid = GridOverlay(ctl)
        self.badge = Badge(ctl, blocked=self.grid.isVisible)
        self.macros_dialog: MacrosDialog | None = None
        self.quitting = False
        self._macro_seen: dict[int, str] = {}   # последнее состояние макроса окна — для журнала
        self._status_seen: dict[int, str] = {}  # последнее состояние окна — для журнала

        root = QWidget()
        lay = QVBoxLayout(root)

        # --- окна
        bar = QHBoxLayout()
        b_all = QPushButton("Запустить все окна")
        b_all.setObjectName("primary")
        b_all.clicked.connect(ctl.launch_all)
        b_grid = QPushButton("Сетка всех окон")
        b_grid.clicked.connect(self.grid.toggle)
        b_set = QPushButton("Настройки")
        b_set.clicked.connect(self.open_settings)
        self.kill_on = QCheckBox("Kill switch")
        self.kill_on.setToolTip("Закрыть окна Chrome, как только сменится подключение к интернету "
                                "(VPN выключился, переподключился, сменился сервер)")
        self.kill_on.setChecked(ctl.cfg["kill_switch"].get("enabled", True))
        self.kill_on.toggled.connect(self._kill_switch_toggled)
        self.net_line = ElidedLabel()
        self.net_line.setObjectName("dim")
        bar.addWidget(b_all)
        bar.addWidget(b_grid)
        bar.addSpacing(16)
        bar.addWidget(self.kill_on)
        bar.addWidget(self.net_line, 1)
        bar.addWidget(b_set)
        lay.addLayout(bar)

        # --- наборы и макросы
        bar2 = QHBoxLayout()
        bar2.addWidget(QLabel("Набор:"))
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(160)
        self.preset_combo.activated.connect(lambda _: self._switch_preset(self.preset_combo.currentText()))
        bar2.addWidget(self.preset_combo)
        presets_btn = QToolButton()
        presets_btn.setText("Наборы ▾")
        presets_btn.setPopupMode(QToolButton.InstantPopup)
        pm = QMenu(presets_btn)
        for text, fn in (("Новый пустой набор…", self._preset_new), ("Копия текущего набора…", self._preset_copy),
                         ("Переименовать…", self._preset_rename), ("Удалить…", self._preset_delete),
                         (None, None), ("Сохранить в файл…", self._preset_export),
                         ("Загрузить из файла…", self._preset_import)):
            if text is None:
                pm.addSeparator()
                continue
            act = pm.addAction(text)
            act.triggered.connect(lambda _=False, fn=fn: fn())
        presets_btn.setMenu(pm)
        bar2.addWidget(presets_btn)
        b_macros = QPushButton("Макросы…")
        b_macros.clicked.connect(self.open_macros)
        bar2.addWidget(b_macros)
        bar2.addStretch(1)
        bar2.addWidget(QLabel("Во всех отмеченных окнах:"))
        self.all_combo = QComboBox()
        self.all_combo.setMinimumWidth(160)
        bar2.addWidget(self.all_combo)
        b_run_all = QPushButton("▶ Запустить")
        b_run_all.clicked.connect(self._run_all)
        b_stop_all = QPushButton("■ Стоп всё")
        b_stop_all.setToolTip(f"Остановить все макросы ({ctl.cfg['hotkeys'].get('stop_all') or 'клавиша не назначена'})")
        b_stop_all.clicked.connect(ctl.stop_all)
        bar2.addWidget(b_run_all)
        bar2.addWidget(b_stop_all)
        lay.addLayout(bar2)

        self.cards_box = QGridLayout()
        self.cards_box.setSpacing(10)
        self.cards: list[SlotCard] = []
        self.no_cards = QLabel("Все окна выключены. Включи их в «Настройки → Окна Chrome» или «Рабочий стол и программы».")
        self.no_cards.setObjectName("dim")
        self.no_cards.setAlignment(Qt.AlignCenter)
        self.build_cards()
        lay.addLayout(self.cards_box, 1)
        self.events = EventLog()
        lay.addWidget(self.events)
        self.setCentralWidget(root)

        self.thumb_timer = QTimer(self)
        self.thumb_timer.setSingleShot(True)
        self.thumb_timer.timeout.connect(self.update_thumbs)

        ctl.statuses_changed.connect(self._statuses_changed)
        ctl.current_changed.connect(lambda _: (self.grid.close_grid(), self.refresh_all()))
        ctl.grid_toggle_requested.connect(self.grid.toggle)
        ctl.message.connect(self.events.add)
        ctl.macros_changed.connect(self.refresh_macros)
        ctl.macro_status.connect(self._macro_status)
        ctl.recording_changed.connect(self._recording_changed)
        ctl.net_state.connect(self._net_state)
        ctl.net_killed.connect(self._net_killed)

        self.tray = QSystemTrayIcon(app_icon(), self)
        # «Запустить все окна» здесь нет: стоя рядом с «Выходом», он запускал пять окон вместо выхода
        self.tray_menu = QMenu()
        for text, fn in (("Показать панель", self.show_panel), ("Сетка всех окон", self.grid.toggle),
                         ("Остановить все макросы", ctl.stop_all), (None, None), ("Выход", self.quit)):
            if text is None:
                self.tray_menu.addSeparator()
                continue
            act = QAction(text, self)
            act.triggered.connect(lambda _=False, fn=fn: fn())
            self.tray_menu.addAction(act)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(lambda r: self.show_panel() if r == QSystemTrayIcon.Trigger else None)
        self.tray.setToolTip("Panel6")
        self.tray.show()
        self.refresh_macros()
        self.refresh_all()

    # ------------------------------------------------------------ показ
    def place_on_panel_monitor(self) -> None:
        g = self.ctl.panel_screen().availableGeometry()
        self.setGeometry(g.adjusted(40, 40, -40, -40))

    def show_panel(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def build_cards(self) -> None:
        """Карточки только для включённых окон; столбцов — 3, а если окон больше девяти — 4 и больше."""
        for c in self.cards:
            self.cards_box.removeWidget(c)
            c.deleteLater()
        self.cards_box.removeWidget(self.no_cards)
        self.no_cards.hide()
        slots = self.ctl.enabled_slots()
        self.cards = [SlotCard(s, self) for s in slots]
        self.card_by_slot = {c.slot: c for c in self.cards}
        cols = max(3, math.ceil(math.sqrt(len(slots))))
        for i, c in enumerate(self.cards):
            self.cards_box.addWidget(c, i // cols, i % cols)
        if not self.cards:
            self.cards_box.addWidget(self.no_cards, 0, 0)
            self.no_cards.show()
        self.host.clear()

    def refresh_all(self) -> None:
        for c in self.cards:
            c.refresh(self.ctl.statuses.get(c.slot, "off"), c.slot == self.ctl.history.current)
        self.schedule_thumbs()
        if self.grid.isVisible():
            self.grid.relayout()

    def refresh_macros(self) -> None:
        names = self.ctl.presets.names()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItems(names)
        self.preset_combo.setCurrentText(self.ctl.preset["name"])
        self.preset_combo.blockSignals(False)
        keep = self.all_combo.currentData() or ""
        fill_macro_combo(self.all_combo, self.ctl.preset["macros"], keep, "— выбери макрос —")
        for c in self.cards:
            c.refresh_macro_list()

    def _macro_status(self, slot: int, st: dict) -> None:
        card = self.card_by_slot.get(slot)
        if card:
            card.refresh_macro()
        self._log_macro(slot, st)

    def _log_macro(self, slot: int, st: dict) -> None:
        """Смена состояния макроса — в журнал. Пауза, пока ты действуешь в окне, слишком частая и не пишется."""
        state, prev = st.get("state", ""), self._macro_seen.get(slot)
        self._macro_seen[slot] = state
        if state == prev:
            return
        who, name, reason = self.ctl.slot_name(slot), st.get("macro", ""), st.get("reason", "")
        if state == "running" and prev != "paused_user":
            self.events.add(f"{who}: ▶ «{name}» " + ("продолжен" if prev == "paused" else "запущен"))
        elif state == "paused":
            self.events.add(f"{who}: ❚❚ «{name}» на паузе")
        elif state == "done":
            self.events.add(f"{who}: ✓ «{name}» выполнен")
        elif state == "stopped":
            why = f" — {reason}" if reason and reason != "остановлен" else ""
            self.events.add(f"{who}: ■ «{name}» остановлен{why}")
        elif state == "error":
            self.events.add(f"{who}: ⚠ «{name}» ошибка: {reason}", warn=True)

    def _statuses_changed(self, statuses: dict) -> None:
        self.refresh_all()
        for slot, status in statuses.items():
            prev = self._status_seen.get(slot)
            self._status_seen[slot] = status
            if prev is None or prev == status:
                continue  # первое состояние после запуска панели не пишем
            who = self.ctl.slot_name(slot)
            if status == "ready":
                self.events.add(f"{who}: работает" if self.ctl.is_chrome(slot) else f"{who}: окно найдено")
            elif status == "off" and prev in ("ready", "missing"):
                self.events.add(f"{who}: окно закрылось", warn=True)
            elif status == "missing":
                self.events.add(f"{who}: окно не найдено", warn=True)

    def _kill_switch_toggled(self, on: bool) -> None:
        self.ctl.cfg["kill_switch"]["enabled"] = on
        config.save(self.ctl.cfg)
        self.ctl.start_netguard()
        self.events.add("Kill switch включён" if on else "Kill switch выключен")

    def _net_state(self, state) -> None:
        self.net_line.setText(state.text() if state else "выключен")
        bad = bool(state) and (not state.local or state.allowed is False or state.leak6)
        self.net_line.setStyleSheet("color:#FF8B82" if bad else "")

    def _net_killed(self, what: str, closed: list) -> None:
        if not closed:
            self.events.add(f"Kill switch: {what}; открытых окон не было")
            return
        names = ", ".join(str(s) for s in closed)
        self.events.add(f"⚠ Kill switch: {what}. Закрыты окна: {names}", warn=True)
        self.tray.showMessage("Panel6: kill switch", f"{what}\nЗакрыты окна: {names}", QSystemTrayIcon.Warning, 10000)

    def _recording_changed(self, _slot: int, _count: int) -> None:
        for c in self.cards:
            c.refresh_macro()

    def schedule_thumbs(self) -> None:
        self.thumb_timer.start(50)

    def update_thumbs(self) -> None:
        if not self.isVisible() or self.isMinimized():
            self.host.clear()
            return
        self.host.sync({c.slot: self.ctl.slot_hwnd(c.slot) for c in self.cards})
        for c in self.cards:
            self.host.place(c.slot, c.thumb)
            c.placeholder.setVisible(not self.host.has(c.slot))

    def resizeEvent(self, e):  # noqa: N802
        self.schedule_thumbs()
        super().resizeEvent(e)

    def showEvent(self, e):  # noqa: N802
        self.schedule_thumbs()
        super().showEvent(e)

    def changeEvent(self, e):  # noqa: N802
        if e.type() == QEvent.WindowStateChange:
            self.schedule_thumbs()
        super().changeEvent(e)

    def closeEvent(self, e):  # noqa: N802
        if self.quitting:
            e.accept()
            return
        e.ignore()
        self.hide()
        self.host.clear()
        self.tray.showMessage("Panel6", "Панель работает в трее. Выход — через меню значка.", app_icon(), 3000)

    def quit(self) -> None:
        self.quitting = True
        if self.macros_dialog:
            self.macros_dialog.flush()
            self.macros_dialog.close()
        self.grid.close_grid()
        self.badge.timer.stop()
        self.badge.hide()
        self.host.clear()
        self.ctl.shutdown()
        self.tray.hide()
        QApplication.quit()

    # ------------------------------------------------------------ макросы и наборы
    def open_macros(self) -> None:
        if self.macros_dialog is None:
            self.macros_dialog = MacrosDialog(self.ctl, self)
        self.macros_dialog.refresh_list()
        self.macros_dialog.show()
        self.macros_dialog.raise_()
        self.macros_dialog.activateWindow()

    def _flush_macros(self) -> None:
        if self.macros_dialog:
            self.macros_dialog.flush()

    def _run_all(self) -> None:
        mid = self.all_combo.currentData()
        if not mid:
            self.events.add("Выбери макрос в списке «Во всех отмеченных окнах»")
            return
        self.ctl.run_macro_all(mid)

    def _switch_preset(self, name: str) -> None:
        self._flush_macros()
        if name and name != self.ctl.preset["name"]:
            self.ctl.switch_preset(name)

    def _ask_name(self, title: str, text: str) -> str:
        name, ok = QInputDialog.getText(self, title, "Название набора:", text=text)
        return name.strip() if ok else ""

    def _preset_new(self) -> None:
        name = self._ask_name("Новый набор", "Новый набор")
        if name:
            self._flush_macros()
            self.ctl.switch_preset(self.ctl.presets.create(name))

    def _preset_copy(self) -> None:
        name = self._ask_name("Копия набора", f"{self.ctl.preset['name']} — копия")
        if name:
            self._flush_macros()
            self.ctl.switch_preset(self.ctl.presets.create(name, copy_from=self.ctl.preset))

    def _preset_rename(self) -> None:
        old = self.ctl.preset["name"]
        name = self._ask_name("Переименовать набор", old)
        if name and name != old:
            self._flush_macros()
            self.ctl.save_preset()
            self.ctl.switch_preset(self.ctl.presets.rename(old, name))

    def _preset_delete(self) -> None:
        names = self.ctl.presets.names()
        old = self.ctl.preset["name"]
        if len(names) <= 1:
            QMessageBox.information(self, "Panel6", "Это единственный набор, его нельзя удалить.")
            return
        if QMessageBox.question(self, "Удалить набор", f"Удалить набор «{old}» со всеми его макросами?") \
                != QMessageBox.Yes:
            return
        self.ctl.presets.delete(old)
        self.ctl.switch_preset(next(n for n in names if n != old))

    def _preset_export(self) -> None:
        self._flush_macros()
        self.ctl.save_preset()
        name = self.ctl.preset["name"]
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить набор", str(Path.home() / f"{name}.json"),
                                              "Набор Panel6 (*.json)")
        if path:
            self.ctl.presets.export(name, Path(path))
            self.events.add(f"Набор «{name}» сохранён в {path}")

    def _preset_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Загрузить набор", str(Path.home()), "Набор Panel6 (*.json)")
        if not path:
            return
        try:
            name = self.ctl.presets.import_file(Path(path))
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Panel6", f"Не удалось загрузить набор: {e}")
            return
        self._flush_macros()
        self.ctl.switch_preset(name)

    # ------------------------------------------------------------ настройки
    def _layout_key(self) -> tuple:
        """Что влияет на набор карточек: число окон, какие включены и какого они вида."""
        cfg = self.ctl.cfg
        return (tuple(bool(w.get("enabled", True)) for w in cfg["windows"]),
                tuple((bool(x.get("enabled", True)), x.get("mode")) for x in cfg["extra_windows"]))

    def open_settings(self) -> None:
        current_ip = self.ctl.netguard.state.public if self.ctl.netguard else None
        dlg = SettingsDialog(self.ctl.cfg, self, current_ip=current_ip)
        if dlg.exec() == QDialog.Accepted:
            layout_before = self._layout_key()
            self.ctl.cfg.clear()
            self.ctl.cfg.update(dlg.result_cfg)
            config.save(self.ctl.cfg)
            if self._layout_key() != layout_before:
                self.ctl.sync_windows()
                self._status_seen.clear()
                self.build_cards()
                self.refresh_macros()
                if self.macros_dialog:  # в редакторе макросов список окон — пересоздать
                    self.macros_dialog.flush()
                    self.macros_dialog.close()
                    self.macros_dialog.deleteLater()
                    self.macros_dialog = None
                self.ctl.refresh()
            self.ctl.start_hotkeys()
            self.kill_on.blockSignals(True)
            self.kill_on.setChecked(self.ctl.cfg["kill_switch"]["enabled"])
            self.kill_on.blockSignals(False)
            self.ctl.start_netguard()
            self.refresh_all()
            if not self.ctl.hotkey_errors:
                self.events.add("Настройки сохранены")


# ------------------------------------------------------------------ настройки

class SettingsDialog(QDialog):
    def __init__(self, cfg: dict, parent=None, current_ip: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Настройки Panel6")
        self.cfg = copy.deepcopy(cfg)
        self.result_cfg = self.cfg
        tabs = QTabWidget()

        # --- окна Chrome: сколько угодно, каждое можно выключить
        self.win_rows: list[dict] = []
        self.win_count = QSpinBox()
        self.win_count.setRange(0, config.MAX_WINDOWS)
        self.win_list = QVBoxLayout()
        tabs.addTab(self._list_tab(
            "Окон Chrome", self.win_count, self.win_list,
            "У каждого окна Chrome свой профиль и свой вход в аккаунт. Выключенное окно не запускается кнопкой "
            "«Запустить все окна», его нет на панели, в сетке и в переборе по кругу, а клавиша не занята; "
            "настройки и профиль сохраняются. Уменьшишь число окон — лишние окна панель перестанет показывать, "
            "но их профили останутся."), "Окна Chrome")
        for w in self.cfg["windows"]:
            self._add_win_row(w)
        self.win_count.setValue(len(self.cfg["windows"]))
        self.win_count.valueChanged.connect(self._win_count_changed)
        self._win_count_changed(self.win_count.value())

        # --- рабочие столы и окна программ
        self._titles = sorted({win32.window_title(h) for h in win32.top_windows()} - {""})
        self.extra_rows: list[dict] = []
        self.extra_count = QSpinBox()
        self.extra_count.setRange(0, config.MAX_EXTRA)
        self.extra_list = QVBoxLayout()
        tabs.addTab(self._list_tab(
            "Окон «рабочий стол / программа»", self.extra_count, self.extra_list,
            "Эти окна идут после окон Chrome. «Рабочий стол Windows» убирает окна Chrome с экрана, "
            "«Окно программы» находит окно по части заголовка (например, Steam). "
            "Можно сделать несколько таких окон, у каждого своя клавиша, и выключить ненужные."),
            "Рабочий стол и программы")
        for x in self.cfg["extra_windows"]:
            self._add_extra_row(x)
        self.extra_count.setValue(len(self.cfg["extra_windows"]))
        self.extra_count.valueChanged.connect(self._extra_count_changed)
        self._extra_count_changed(self.extra_count.value())

        # --- клавиши и мониторы
        k_tab = QWidget()
        k = QFormLayout(k_tab)
        hk = self.cfg["hotkeys"]
        self.hk = {name: HotkeyEdit(hk.get(name, "")) for name in
                   ("grid", "back", "next", "prev", "macro_toggle", "stop_all", "record")}
        k.addRow("Сетка всех окон", self.hk["grid"])
        k.addRow("Предыдущее окно (туда-обратно)", self.hk["back"])
        k.addRow("Следующее по кругу", self.hk["next"])
        k.addRow("Предыдущее по кругу", self.hk["prev"])
        k.addRow("Старт / пауза основного макроса окна", self.hk["macro_toggle"])
        k.addRow("Остановить все макросы", self.hk["stop_all"])
        k.addRow("Начать / закончить запись макроса", self.hk["record"])
        screens = ordered_screens()
        labels = [f"{i + 1}: {sc.name()} {sc.geometry().width()}×{sc.geometry().height()}" + (" (основной)" if i == 0 else "")
                  for i, sc in enumerate(screens)]
        self.game_mon, self.panel_mon = QComboBox(), QComboBox()
        for cb, key in ((self.game_mon, "game_monitor"), (self.panel_mon, "panel_monitor")):
            cb.addItems(labels)
            cb.setCurrentIndex(min(self.cfg[key], len(labels) - 1))
        k.addRow("Монитор для игровых окон", self.game_mon)
        k.addRow("Монитор для панели", self.panel_mon)
        chrome_row = QHBoxLayout()
        self.chrome = QLineEdit(self.cfg["chrome_path"])
        self.chrome.setPlaceholderText("найти автоматически")
        pick = QPushButton("…")
        pick.clicked.connect(self._pick_chrome)
        chrome_row.addWidget(self.chrome, 1)
        chrome_row.addWidget(pick)
        k.addRow("chrome.exe", chrome_row)
        tabs.addTab(k_tab, "Клавиши и мониторы")

        # --- макросы
        m_tab = QWidget()
        m = QFormLayout(m_tab)
        ui = self.cfg["macro_user_input"]
        self.ui_mode = QComboBox()
        for key, text in USER_INPUT_MODES.items():
            self.ui_mode.addItem(text, key)
        self.ui_mode.setCurrentIndex(max(0, self.ui_mode.findData(ui.get("mode", "pause"))))
        self.ui_idle = QDoubleSpinBox()
        self.ui_idle.setRange(0.2, 60)
        self.ui_idle.setSingleStep(0.5)
        self.ui_idle.setSuffix(" с")
        self.ui_idle.setValue(float(ui.get("idle_sec", 1.0)))
        m.addRow("Когда ты сам действуешь в окне, где идёт макрос", self.ui_mode)
        m.addRow("Продолжить после тишины", self.ui_idle)
        note = QLabel("«Действуешь» — двигаешь мышью или жмёшь клавиши, пока это окно активно или выбрано в сетке. "
                      "У отдельного макроса можно задать своё поведение в окне «Макросы».")
        note.setWordWrap(True)
        note.setObjectName("dim")
        m.addRow(note)
        tabs.addTab(m_tab, "Макросы")

        # --- метка над игрой
        b_tab = QWidget()
        b = QFormLayout(b_tab)
        badge = self.cfg["badge"]
        self.badge_on = QCheckBox("Показывать метку поверх окна игры")
        self.badge_on.setChecked(badge.get("enabled", True))
        self.badge_corner = QComboBox()
        for key, text in CORNERS.items():
            self.badge_corner.addItem(text, key)
        self.badge_corner.setCurrentIndex(max(0, self.badge_corner.findData(badge.get("corner", "top_right"))))
        self.badge_on.toggled.connect(self.badge_corner.setEnabled)
        self.badge_corner.setEnabled(self.badge_on.isChecked())
        b.addRow(self.badge_on)
        b.addRow("Где на игровом мониторе", self.badge_corner)
        b_note = QLabel("Метка видна, только когда впереди окно игры: номер и название окна, что делает его макрос, "
                        "и значки макросов всех окон (▶ идёт, ❚❚ пауза, ⚠ ошибка, ● запись). "
                        "Клики проходят сквозь метку, фокус и захват курсора она не забирает.")
        b_note.setWordWrap(True)
        b_note.setObjectName("dim")
        b.addRow(b_note)
        tabs.addTab(b_tab, "Метка над игрой")

        # --- kill switch
        n_tab = QWidget()
        n = QFormLayout(n_tab)
        ks = self.cfg["kill_switch"]
        self.ks_on = QCheckBox("Закрывать окна Chrome, когда меняется подключение к интернету")
        self.ks_on.setChecked(ks.get("enabled", True))
        self.ks_public = QCheckBox("Проверять и внешний IP")
        self.ks_public.setChecked(ks.get("public_ip", True))
        self.ks_every = QDoubleSpinBox()
        self.ks_every.setRange(2, 60)
        self.ks_every.setDecimals(0)
        self.ks_every.setSuffix(" с")
        self.ks_every.setValue(float(ks.get("interval_sec", 5.0)))
        every_row = QHBoxLayout()
        every_row.addWidget(self.ks_public)
        every_row.addWidget(QLabel("каждые"))
        every_row.addWidget(self.ks_every)
        every_row.addStretch(1)
        self.ks_allowed = QLineEdit(ks.get("allowed_ips", ""))
        self.ks_allowed.setPlaceholderText("пусто — любой; несколько — через запятую")
        take = QPushButton("Взять текущий")
        take.setEnabled(bool(current_ip))
        take.setToolTip(f"Подставить текущий внешний IP {current_ip}" if current_ip
                        else "Текущий внешний IP ещё не известен: включи kill switch и подожди пару секунд")
        self._current_ip = current_ip or ""
        take.clicked.connect(self._take_current_ip)
        allowed_row = QHBoxLayout()
        allowed_row.addWidget(self.ks_allowed, 1)
        allowed_row.addWidget(take)
        n.addRow(self.ks_on)
        n.addRow(every_row)
        n.addRow("Разрешённый внешний IP", allowed_row)
        n_note = QLabel(
            "Окна закрываются сразу (процессы Chrome завершаются), макросы останавливаются, рабочие столы и окна "
            "программ не трогаются. Срабатывает, когда:\n"
            "• сменился путь в интернет — VPN выключился, включился, переподключился, Wi-Fi сменился на кабель "
            "или связь пропала. Windows сообщает об этом сразу, и окна закрываются раньше, чем Chrome начнёт "
            "переподключаться; запросов в сеть эта проверка не делает;\n"
            "• сменился внешний IP — например, VPN переключился на другой сервер с тем же адресом туннеля "
            "(запрос к Cloudflare 1.1.1.1, при недоступности — api.ipify.org; после любой смены сети — сразу "
            "и ещё раз в секунду три секунды);\n"
            "• внешний IP не из разрешённых.\n"
            "Разрешённый IP: окна запускаются только после свежей проверки, что внешний IP из списка, а IPv6 "
            "не идёт мимо VPN. Без VPN (или с другим IP) окна не запустятся.\n"
            "Если внешний IP меняется сам (мобильный интернет без VPN), выключи проверку внешнего IP и очисти "
            "список разрешённых.")
        n_note.setWordWrap(True)
        n_note.setObjectName("dim")
        n.addRow(n_note)
        tabs.addTab(n_tab, "Kill switch")

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(buttons)

    # ------------------------------------------------------------ списки окон
    @staticmethod
    def _list_tab(label: str, count: QSpinBox, rows: QVBoxLayout, hint: str) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        top = QHBoxLayout()
        top.addWidget(QLabel(label))
        top.addWidget(count)
        top.addStretch(1)
        lay.addLayout(top)
        note = QLabel(hint)
        note.setWordWrap(True)
        note.setObjectName("dim")
        lay.addWidget(note)
        inner = QWidget()
        rows.setContentsMargins(0, 0, 6, 0)
        inner.setLayout(rows)
        rows.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)
        return tab

    @staticmethod
    def _insert(rows: QVBoxLayout, box: QWidget) -> None:
        rows.insertWidget(rows.count() - 1, box)  # перед растяжкой в конце

    def _add_win_row(self, w: dict) -> None:
        i = len(self.win_rows) + 1
        box = QGroupBox(f"Окно {i}")
        f = QFormLayout(box)
        on = QCheckBox("Включено")
        on.setChecked(w.get("enabled", True))
        name, url, hk = QLineEdit(w["name"]), QLineEdit(w["url"]), HotkeyEdit(w["hotkey"])
        f.addRow(on)
        f.addRow("Название", name)
        f.addRow("Адрес страницы", url)
        f.addRow("Клавиша", hk)
        for wid in (name, url, hk):
            on.toggled.connect(wid.setEnabled)
            wid.setEnabled(on.isChecked())
        self._insert(self.win_list, box)
        self.win_rows.append({"box": box, "src": w, "enabled": on, "name": name, "url": url, "hotkey": hk})

    def _win_count_changed(self, n: int) -> None:
        while len(self.win_rows) < n:
            self._add_win_row(config.chrome_window(len(self.win_rows) + 1))
        for i, row in enumerate(self.win_rows):
            row["box"].setVisible(i < n)

    def _add_extra_row(self, x: dict) -> None:
        i = len(self.extra_rows) + 1
        box = QGroupBox()
        f = QFormLayout(box)
        on = QCheckBox("Включено")
        on.setChecked(x.get("enabled", True))
        name, hk = QLineEdit(x["name"]), HotkeyEdit(x["hotkey"])
        mode = QComboBox()
        mode.addItem("Рабочий стол Windows", "desktop")
        mode.addItem("Окно программы", "window")
        mode.setCurrentIndex(0 if x.get("mode") == "desktop" else 1)
        title = QComboBox()
        title.setEditable(True)
        title.addItems(self._titles)
        title.setCurrentText(x.get("window_title", ""))
        strategy = QComboBox()
        strategy.addItem("Сдвигать окна Chrome за край экрана (игра не замирает)", "offscreen")
        strategy.addItem("Сворачивать окна Chrome (игра замирает)", "minimize")
        strategy.setCurrentIndex(0 if x.get("desktop_strategy", "offscreen") == "offscreen" else 1)
        f.addRow(on)
        f.addRow("Название", name)
        f.addRow("Клавиша", hk)
        f.addRow("Что это", mode)
        f.addRow("Часть заголовка окна", title)
        f.addRow("Как показывать рабочий стол", strategy)
        row = {"box": box, "src": x, "enabled": on, "name": name, "hotkey": hk, "mode": mode, "title": title,
               "strategy": strategy, "index": i}

        def update(*_):
            enabled = on.isChecked()
            desktop = mode.currentData() == "desktop"
            for wid in (name, hk, mode):
                wid.setEnabled(enabled)
            title.setEnabled(enabled and not desktop)
            strategy.setEnabled(enabled and desktop)
            box.setTitle(f"Окно {self.win_count.value() + i}")

        on.toggled.connect(update)
        mode.currentIndexChanged.connect(update)
        self.win_count.valueChanged.connect(update)  # номер окна идёт после окон Chrome
        update()
        self._insert(self.extra_list, box)
        self.extra_rows.append(row)

    def _extra_count_changed(self, n: int) -> None:
        while len(self.extra_rows) < n:
            self._add_extra_row(config.extra_window(len(self.extra_rows) + 1))
        for i, row in enumerate(self.extra_rows):
            row["box"].setVisible(i < n)

    def _take_current_ip(self) -> None:
        self.ks_allowed.setText(self._current_ip)

    def _pick_chrome(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "chrome.exe", "", "chrome.exe (chrome.exe);;Все файлы (*)")
        if path:
            self.chrome.setText(path)

    def _save(self) -> None:
        cfg = self.cfg
        windows = []
        for i, row in enumerate(self.win_rows[:self.win_count.value()], start=1):
            w = dict(row["src"])
            w.update(name=row["name"].text().strip() or f"Окно {i}", url=row["url"].text().strip(),
                     hotkey=row["hotkey"].text(), enabled=row["enabled"].isChecked())
            windows.append(w)
        extra = []
        for i, row in enumerate(self.extra_rows[:self.extra_count.value()], start=1):
            x = dict(row["src"])
            x.update(name=row["name"].text().strip() or config.extra_window(i)["name"], hotkey=row["hotkey"].text(),
                     enabled=row["enabled"].isChecked(), mode=row["mode"].currentData(),
                     window_title=row["title"].currentText().strip(), desktop_strategy=row["strategy"].currentData())
            if x["enabled"] and x["mode"] == "window" and not x["window_title"]:
                QMessageBox.warning(self, "Panel6", f"«{x['name']}»: укажи часть заголовка окна программы.")
                return
            extra.append(x)
        cfg["windows"], cfg["extra_windows"] = windows, extra
        cfg["hotkeys"] = {name: edit.text() for name, edit in self.hk.items()}
        cfg["game_monitor"] = self.game_mon.currentIndex()
        cfg["panel_monitor"] = self.panel_mon.currentIndex()
        cfg["chrome_path"] = self.chrome.text().strip()
        cfg["macro_user_input"] = {"mode": self.ui_mode.currentData(), "idle_sec": self.ui_idle.value()}
        cfg["badge"] = {"enabled": self.badge_on.isChecked(), "corner": self.badge_corner.currentData()}
        allowed, bad = parse_allowed(self.ks_allowed.text())
        if bad:
            QMessageBox.warning(self, "Panel6", "Это не IPv4-адреса: " + ", ".join(bad))
            return
        cfg["kill_switch"] = {"enabled": self.ks_on.isChecked(), "public_ip": self.ks_public.isChecked(),
                              "interval_sec": self.ks_every.value(), "allowed_ips": ", ".join(allowed)}
        all_keys = [w["hotkey"] for w in windows + extra if w["enabled"]] + list(cfg["hotkeys"].values())
        used = [k for k in all_keys if k]
        dupes = sorted({k for k in used if used.count(k) > 1})
        if dupes:
            QMessageBox.warning(self, "Panel6", "Одна клавиша назначена дважды: " + ", ".join(dupes))
            return
        self.result_cfg = cfg
        self.accept()
