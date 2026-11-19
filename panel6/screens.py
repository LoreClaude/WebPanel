"""Снимки окон панели с примером данных — чтобы проверять внешний вид без живого ПК."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QComboBox, QLineEdit, QStyle, QStyleOptionComboBox, QStyleOptionFrame, QTabWidget,
    QWidget,
)

from .macros import normalize_macro


def _demo_macros() -> list[dict]:
    w = {"code": "KeyW", "key": "w", "vk": 87}
    return [
        normalize_macro({"name": "Анти-АФК", "hotkey": "Ctrl+1", "repeat": {"count": 0, "gap_ms": 30000},
                         "overrides": {"3": {"count": 5}}, "steps": [
            {"t": "key", "code": "KeyW", "key": "w", "vk": 87, "ms": 400},
            {"t": "look", "dx": 120, "dy": 0, "ms": 300, "delay": 200},
            {"t": "loop", "times": 3},
            {"t": "click", "x": None, "y": None, "delay": 150},
            {"t": "wait", "ms": 500},
            {"t": "endloop"},
            {"t": "random", "ms": 5000, "min_ms": 50, "max_ms": 50, "kinds": ["click", "look", "key"],
             "keys": [w, {"code": "KeyA", "key": "a", "vk": 65}], "area": None},
            {"t": "click", "x": 0.52, "y": 0.31, "count": 2, "delay": 1000, "delay_max": 1500},
            {"t": "wheel", "x": 0.5, "y": 0.5, "dy": -240},
        ]}),
        normalize_macro({"name": "Сбор наград", "steps": [{"t": "click", "x": 0.9, "y": 0.1}]}),
    ]


def _text_room(w: QWidget) -> int:
    """Ширина, в которой поле на самом деле рисует текст (без рамки, отступов, кнопок и полей строки ввода)."""
    if isinstance(w, QAbstractSpinBox):
        return w.findChild(QLineEdit).width() - 4
    if isinstance(w, QComboBox):
        opt = QStyleOptionComboBox()
        w.initStyleOption(opt)
        return w.style().subControlRect(QStyle.CC_ComboBox, opt, QStyle.SC_ComboBoxEditField, w).width()
    opt = QStyleOptionFrame()
    w.initStyleOption(opt)
    return w.style().subElementRect(QStyle.SE_LineEditContents, opt, w).width() - 4


def field_report(widget) -> str:
    """Высота полей ввода и поля, где текст не помещается, — проверка вёрстки без картинки."""
    fields = [w for w in widget.findChildren(QWidget) if isinstance(w, (QLineEdit, QComboBox, QAbstractSpinBox))
              and w.isVisible() and not isinstance(w.parent(), (QComboBox, QAbstractSpinBox))]
    if not fields:
        return "полей нет"
    heights = [w.height() for w in fields]
    clipped = []
    for w in fields:
        text = w.currentText() if isinstance(w, QComboBox) else w.text()
        room = _text_room(w)
        if text and w.fontMetrics().horizontalAdvance(text) > room:
            clipped.append(f"«{text}» {w.width()}px")
    return (f"полей {len(fields)}, высота {min(heights)}–{max(heights)}px, "
            f"текст не помещается: {', '.join(clipped) if clipped else 'нигде'}")


def render_screens(app: QApplication, ctl, win, out: Path) -> list[Path]:
    from .macro_ui import MacrosDialog, StepDialog
    from .app import SettingsDialog

    out.mkdir(parents=True, exist_ok=True)
    saved = []

    def grab(widget, name):
        app.processEvents()
        path = out / f"{name}.png"
        widget.grab().save(str(path))
        saved.append(path)
        print(f"{name}: {widget.width()}x{widget.height()}, {field_report(widget)}", flush=True)

    # пример данных только в памяти: набор на диске не меняется
    ctl.preset = dict(ctl.preset, macros=_demo_macros(), window_macros=dict(ctl.preset["window_macros"]))
    ctl.preset["window_macros"]["1"] = ctl.preset["macros"][0]["id"]
    ctl.save_preset = lambda: None
    ctl.preset_edited = ctl.macros_changed.emit
    ctl.macros_changed.emit()
    win.resize(1500, 860)
    win.show()
    ctl.statuses = {1: "ready", 2: "ready", 3: "starting", 4: "off", 5: "off", 6: "ready"}
    ctl.macro_states[1] = {"state": "running", "macro": "Анти-АФК", "macro_id": ctl.preset["macros"][0]["id"],
                           "step": 4, "index": 3, "total": 9, "iteration": 2, "count": 0}
    ctl.macro_states[2] = {"state": "paused_user", "macro": "Сбор наград"}
    ctl.macro_states[4] = {"state": "error", "macro": "Сбор наград", "reason": "окно закрыто"}
    win.refresh_all()
    ctl.message.emit("Запускаю Окно 3…")
    for slot, st in ((1, {"state": "running", "macro": "Анти-АФК"}), (2, {"state": "running", "macro": "Сбор наград"}),
                     (2, {"state": "paused", "macro": "Сбор наград"}), (4, ctl.macro_states[4])):
        win._log_macro(slot, st)
    from .netguard import NetState

    win._net_state(NetState("10.2.0.2", "ProtonVPN", public="185.163.47.154", allowed=True))
    win._net_killed("сменилось подключение: ProtonVPN 10.2.0.2 → Ethernet 192.168.1.10", [1, 2])
    grab(win, "1_panel")

    ctl.foreground_slot = lambda: 1  # будто впереди окно игры 1
    win.badge.check()
    grab(win.badge, "7_badge")
    win.badge.hide()

    dlg = MacrosDialog(ctl, win)
    dlg.show()
    dlg._select_rows([6])
    grab(dlg, "2_macros")
    dlg.tabs.setCurrentIndex(1)
    grab(dlg, "2b_macros_params")
    dlg.tabs.setCurrentIndex(0)
    steps = ctl.preset["macros"][0]["steps"]
    for name, step in (("3_step_random", steps[6]), ("4_step_click", steps[7]), ("5_step_key", steps[0])):
        sd = StepDialog(step, lambda: None, dlg)
        sd.show()
        grab(sd, name)
        sd.close()
    dlg.close()

    st = SettingsDialog(ctl.cfg, win)
    st.resize(700, 560)
    st.show()
    tabs = st.findChild(QTabWidget)
    for i in range(tabs.count()):
        tabs.setCurrentIndex(i)
        grab(st, f"6_settings_{i + 1}")
    st.close()
    return saved
