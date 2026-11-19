"""Проверка интерфейса и контроллера без экрана (QT_QPA_PLATFORM=offscreen)."""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def ctl(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL6_HOME", str(tmp_path))
    from panel6 import config
    from panel6.controller import Controller

    c = Controller(config.load())
    yield c
    c.shutdown()


def wait_until(qapp, cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_default_preset_and_bindings(ctl):
    assert ctl.preset["name"] == "Основной"
    names = ctl.bindings()
    assert {"slot1", "grid", "macro_toggle", "stop_all", "record"} <= set(names)
    assert ctl.hotkey_errors == []


def test_macro_hotkey_conflict_reported(ctl):
    from panel6.macros import new_macro

    m = new_macro("A")
    m["hotkey"] = "Num1"  # уже занята окном 1
    ctl.preset["macros"].append(m)
    binds = ctl.bindings()
    assert f"macro:{m['id']}" not in binds
    assert any("уже назначено" in e for e in ctl.hotkey_errors)


def test_macros_dialog_edit_flow(ctl, qapp):
    from panel6.macro_ui import MacrosDialog
    from panel6.presets import PresetStore

    dlg = MacrosDialog(ctl)
    dlg._new()
    assert len(ctl.preset["macros"]) == 1
    m = ctl.preset["macros"][0]
    assert dlg.current is m
    dlg.name.setText("Анти-АФК")
    dlg._edited_name("Анти-АФК")
    dlg.count.setValue(0)
    dlg.win_rows[2][1].setValue(5)  # окно 3: 5 повторов
    dlg._recorded([{"t": "click", "x": 0.5, "y": 0.5, "button": "left", "count": 1, "delay": 0},
                   {"t": "wait", "ms": 1000, "delay": 0}])
    assert len(m["steps"]) == 2 and dlg.table.rowCount() == 2
    dlg.table.selectRow(1)
    dlg._move_steps(-1)
    assert m["steps"][0]["t"] == "wait"
    dlg._dup_steps()
    assert len(m["steps"]) == 3
    dlg.flush()
    saved = PresetStore(ctl.presets.root).load("Основной")["macros"][0]
    assert saved["name"] == "Анти-АФК" and saved["repeat"]["count"] == 0
    assert saved["overrides"] == {"3": {"count": 5}} and len(saved["steps"]) == 3
    # второй новый макрос не должен унаследовать поля первого
    dlg._new()
    assert dlg.current is not m and dlg.name.text().startswith("Макрос") and dlg.table.rowCount() == 0
    dlg.close()


def test_main_window_and_settings_build(ctl, qapp):
    from panel6.app import MainWindow, SettingsDialog
    from panel6.macros import new_macro

    ctl.preset["macros"].append(new_macro("Фарм"))
    ctl.preset_edited()
    win = MainWindow(ctl)
    win.refresh_macros()
    assert win.all_combo.count() == 2 and win.cards[0].macro_combo.count() == 2
    win.cards[0].macro_combo.setCurrentIndex(1)
    assert ctl.main_macro_id(1) == ctl.preset["macros"][0]["id"]
    dlg = SettingsDialog(ctl.cfg, win)
    assert dlg.hk["record"].text() == "NumMul"
    dlg.close()
    win.quitting = True
    win.close()


def test_status_text():
    from panel6.macro_ui import status_text

    assert status_text({"state": "running", "macro": "A", "step": 2, "total": 5, "iteration": 1, "count": 0}) == \
        "▶ «A» · шаг 2/5 · повтор 1/∞"
    assert status_text({"state": "error", "macro": "A", "reason": "окно закрыто"}) == "⚠ «A» ошибка: окно закрыто"
    assert status_text(None) == ""


def test_controller_runs_and_records_in_chrome(ctl, qapp, tmp_path):
    """Сквозной путь через контроллер: запуск макроса, статусы через сигналы Qt, запись в новый макрос."""
    import socket

    from panel6.chrome import ChromeWindow, find_chrome
    from panel6.macros import normalize_macro

    chrome = find_chrome(os.environ.get("PANEL6_CHROME", ""))
    if not chrome:
        pytest.skip("Chrome не найден")
    os.environ["PANEL6_HEADLESS"] = "1"
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        win = ChromeWindow(1, port=port)
        page = ("data:text/html,<body style='margin:0'><script>window.__n=0;"
                "addEventListener('click',()=>window.__n++,true)</script></body>")
        win.launch(chrome, page, None)
        assert wait_until(qapp, lambda: win.refresh() == "ready", 40)
        # «ready» — Chrome подключён, но страница может ещё грузиться: ждём её скрипт, иначе клики уйдут в пустую
        assert wait_until(qapp, lambda: win.evaluate("window.__n") == 0, 20)
        ctl.windows[0] = win

        m = normalize_macro({"name": "Клики", "repeat": {"count": 3},
                             "steps": [{"t": "click", "x": 0.5, "y": 0.5}, {"t": "wait", "ms": 50}]})
        ctl.preset["macros"].append(m)
        states = []
        ctl.macro_status.connect(lambda slot, st: states.append((slot, st["state"])))
        assert ctl.run_macro(1, m["id"])
        assert wait_until(qapp, lambda: (1, "done") in states)
        assert win.evaluate("window.__n") == 3
        assert ctl.macro_states[1]["state"] == "done"

        assert ctl.start_recording(1)
        win.mouse("mouseMoved", 100, 100, button="none")
        win.mouse("mousePressed", 100, 100, buttons=1)
        win.mouse("mouseReleased", 100, 100)
        time.sleep(0.3)
        before = len(ctl.preset["macros"])
        steps = ctl.stop_recording()
        assert steps and steps[0]["t"] == "click"
        assert len(ctl.preset["macros"]) == before + 1
        assert ctl.preset["macros"][-1]["name"].startswith("Запись")
        win.close_browser()
    finally:
        os.environ.pop("PANEL6_HEADLESS", None)


def test_random_step_dialog(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from panel6.macro_ui import StepDialog, _NEW_STEP
    from panel6.macros import normalize_step

    dlg = StepDialog(normalize_step(_NEW_STEP["random"]), lambda: None)
    assert dlg._checked_kinds() == ["click"]
    dlg.r_kinds["key"].setChecked(True)
    dlg.r_kinds["look"].setChecked(True)
    dlg._accept()
    assert dlg.result() == 0 and "клавиши" in dlg.error.text().lower()  # без клавиш нельзя
    dlg.r_keys.setFocus()
    for k in (Qt.Key_W, Qt.Key_A, Qt.Key_W, Qt.Key_S):
        QTest.keyClick(dlg.r_keys, k)
    assert [k["code"] for k in dlg.r_keys.keys] == ["KeyW", "KeyA", "KeyS"]  # повтор не добавляется
    QTest.keyClick(dlg.r_keys, Qt.Key_Backspace)
    assert [k["code"] for k in dlg.r_keys.keys] == ["KeyW", "KeyA"]
    dlg.r_where.setCurrentIndex(2)  # на месте курсора
    dlg.r_min.setValue(40)
    dlg.r_max.setValue(80)
    dlg._accept()
    s = dlg.step
    assert s["t"] == "random" and s["kinds"] == ["click", "look", "key"] and s["area"] is None
    assert (s["min_ms"], s["max_ms"]) == (40, 80) and [k["code"] for k in s["keys"]] == ["KeyW", "KeyA"]


def test_region_picker_drag(qapp):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QPixmap
    from PySide6.QtTest import QTest

    from panel6.macro_ui import ShotView

    pm = QPixmap(480, 270)  # совпадает с минимальным размером просмотра — снимок без полей
    pm.fill(Qt.darkGray)
    view = ShotView(pm, mode="region")
    view.resize(480, 270)
    got = []
    view.region_picked.connect(lambda *r: got.append(r))
    QTest.mousePress(view, Qt.LeftButton, Qt.NoModifier, QPoint(360, 202))
    QTest.mouseMove(view, QPoint(120, 67))
    QTest.mouseRelease(view, Qt.LeftButton, Qt.NoModifier, QPoint(120, 67))
    assert got and all(abs(a - b) < 0.01 for a, b in zip(got[0], (0.25, 0.25, 0.75, 0.75)))


def test_theme_keeps_inputs_tall(qapp):
    from PySide6.QtWidgets import QComboBox, QSpinBox, QVBoxLayout, QWidget

    from panel6.app import apply_theme

    apply_theme(qapp)
    box = QWidget()
    lay = QVBoxLayout(box)
    fields = [QSpinBox(), QComboBox()]
    for w in fields:
        lay.addWidget(w)
    box.resize(200, 10)  # раскладке дали слишком мало места
    box.show()
    qapp.processEvents()
    for w in fields:
        assert w.height() >= 28, type(w).__name__  # поля не сжимаются в «полоску»
    box.close()


@pytest.mark.parametrize("font", ["", "font-size:16pt"])
def test_spin_numbers_fit_at_any_font(qapp, font):
    from PySide6.QtWidgets import QDoubleSpinBox, QHBoxLayout, QLineEdit, QWidget

    from panel6.app import apply_theme

    apply_theme(qapp)
    box = QWidget()
    box.setStyleSheet(font)  # крупный шрифт — как при увеличенном тексте в Windows
    lay = QHBoxLayout(box)
    sp = QDoubleSpinBox()
    sp.setRange(0, 100)
    sp.setDecimals(2)
    sp.setSuffix(" %")
    sp.setValue(100)
    lay.addWidget(sp)
    lay.addStretch(1)
    box.resize(10, 40)  # поле получает не больше, чем просит само
    box.show()
    qapp.processEvents()
    edit = sp.findChild(QLineEdit)
    assert edit.width() - 4 >= sp.fontMetrics().horizontalAdvance("100.00 %")
    box.close()


def test_field_report_flags_clipped_text(qapp):
    from PySide6.QtWidgets import QSpinBox, QVBoxLayout, QWidget

    from panel6.app import apply_theme
    from panel6.screens import field_report

    apply_theme(qapp)
    box = QWidget()
    lay = QVBoxLayout(box)
    sp = QSpinBox()
    sp.setRange(0, 10_000_000)
    sp.setSuffix(" мс")
    sp.setValue(3_600_000)
    lay.addWidget(sp)
    box.show()
    qapp.processEvents()
    assert field_report(box).endswith("нигде")
    sp.setFixedWidth(70)
    qapp.processEvents()
    assert "«3600000 мс» 70px" in field_report(box)
    box.close()


def test_hotkey_and_key_fields_take_punctuation(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from panel6.macro_ui import KeyCaptureEdit
    from panel6.macros import describe, normalize_step
    from panel6.widgets import HotkeyEdit

    hk = HotkeyEdit("")
    QTest.keyClick(hk, Qt.Key_Minus)
    assert hk.text() == "-"
    QTest.keyClick(hk, Qt.Key_Equal, Qt.ControlModifier)
    assert hk.text() == "Ctrl+="
    QTest.keyClick(hk, Qt.Key_Minus, Qt.KeypadModifier)
    assert hk.text() == "NumSub"  # минус цифрового блока — отдельная клавиша
    key = KeyCaptureEdit()
    QTest.keyClick(key, Qt.Key_BracketLeft)
    assert key.params["code"] == "BracketLeft" and key.text() == "["
    step = normalize_step({"t": "key", "action": "press", "ms": 50, **key.params})
    assert describe(step) == "Клавиша [: нажать (50 мс)"


def _dialog_with_steps(ctl, steps):
    from panel6.macro_ui import MacrosDialog
    from panel6.macros import normalize_macro

    m = normalize_macro({"name": "Тест", "steps": steps})
    ctl.preset["macros"].append(m)
    dlg = MacrosDialog(ctl)
    dlg.refresh_list(select_id=m["id"])
    return dlg, m


def _clicks(n):
    return [{"t": "click", "x": round(0.1 * (i + 1), 1), "y": 0.5, "delay": 100 * i} for i in range(n)]


def test_steps_keys_clipboard_and_undo(ctl, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    dlg, m = _dialog_with_steps(ctl, _clicks(4))
    t = dlg.table
    assert dlg.tabs.tabText(0) == "Шаги" and dlg.tabs.tabText(1) == "Параметры"
    dlg._select_rows([1, 2])
    QTest.keyClick(t, Qt.Key_C, Qt.ControlModifier)
    dlg._select_rows([3])
    QTest.keyClick(t, Qt.Key_V, Qt.ControlModifier)
    assert [s["x"] for s in m["steps"]] == [0.1, 0.2, 0.3, 0.4, 0.2, 0.3]
    assert dlg._rows() == [4, 5]  # вставленные шаги выделены
    QTest.keyClick(t, Qt.Key_Delete)
    assert [s["x"] for s in m["steps"]] == [0.1, 0.2, 0.3, 0.4]
    QTest.keyClick(t, Qt.Key_Z, Qt.ControlModifier)
    assert len(m["steps"]) == 6
    QTest.keyClick(t, Qt.Key_Y, Qt.ControlModifier)
    assert len(m["steps"]) == 4
    dlg._select_rows([2, 3])
    QTest.keyClick(t, Qt.Key_Up, Qt.ControlModifier)
    assert [s["x"] for s in m["steps"]] == [0.1, 0.3, 0.4, 0.2] and dlg._rows() == [1, 2]
    QTest.keyClick(t, Qt.Key_D, Qt.ControlModifier)
    assert [s["x"] for s in m["steps"]] == [0.1, 0.3, 0.4, 0.3, 0.4, 0.2]
    QTest.keyClick(t, Qt.Key_X, Qt.ControlModifier)
    assert [s["x"] for s in m["steps"]] == [0.1, 0.3, 0.4, 0.2]
    # вырезанное вставляется в другой макрос
    other, m2 = _dialog_with_steps(ctl, [])
    other._paste_steps()
    assert [s["x"] for s in m2["steps"]] == [0.3, 0.4]
    dlg.close()
    other.close()


def test_running_step_highlight_and_run_from_selected(ctl, qapp, monkeypatch):
    from PySide6.QtCore import Qt

    from panel6.macro_ui import RUN_BG

    dlg, m = _dialog_with_steps(ctl, _clicks(4))
    ctl.macro_status.emit(1, {"state": "running", "macro": "Тест", "macro_id": m["id"], "index": 2,
                              "step": 3, "total": 4, "iteration": 1, "count": 1})
    qapp.processEvents()
    assert dlg.table.item(2, 0).text() == "▶ 3"
    assert dlg.table.item(2, 1).background().color() == RUN_BG
    ctl.macro_status.emit(1, {"state": "done", "macro": "Тест", "macro_id": m["id"], "index": -1})
    qapp.processEvents()
    assert dlg.table.item(2, 0).text() == "3" and dlg.table.item(2, 1).background().style() == Qt.NoBrush
    calls = []
    monkeypatch.setattr(ctl, "run_macro", lambda slot, mid, quiet=False, start=0: calls.append((slot, mid, start)))
    dlg._select_rows([2])
    assert dlg.b_run_from.text() == "▶ С шага 3"
    dlg.b_run_from.click()
    assert calls == [(1, m["id"], 2)]
    dlg.close()


def test_process_recording_selection_or_whole(ctl, qapp):
    dlg, m = _dialog_with_steps(ctl, _clicks(4) + [{"t": "look", "dx": 5, "dy": 0, "ms": 50},
                                                   {"t": "look", "dx": 7, "dy": 0, "ms": 50, "delay": 4}])
    dlg._select_rows([1, 2])
    dlg._process("scale", 2.0)
    assert [s["delay"] for s in m["steps"]] == [0, 200, 400, 300, 0, 4]  # только выделенные
    assert dlg.tool_note.text().startswith("Шаги 2–3: паузы ×2: изменено шагов 2.")
    assert "→" not in dlg.tool_note.text()  # число шагов не менялось
    dlg._select_rows([0])
    dlg._process("merge", 100)
    assert len(m["steps"]) == 5 and m["steps"][-1]["dx"] == 12  # весь макрос
    assert "Весь макрос: склеено поворотов камеры: 1. Шагов: 6 → 5." in dlg.tool_note.text()
    dlg._process("merge", 100)
    assert "менять нечего" in dlg.tool_note.text()
    dlg._undo()
    assert len(m["steps"]) == 6
    dlg.close()


def test_steps_context_menu_builds(ctl, qapp):
    from PySide6.QtCore import QPoint

    dlg, _m = _dialog_with_steps(ctl, _clicks(3))
    dlg.show()
    qapp.processEvents()
    y = dlg.table.rowViewportPosition(1) + 5
    menu = dlg._build_table_menu(QPoint(10, y))
    assert dlg._rows() == [1]  # строка под курсором выделилась
    texts = [a.text() for a in menu.actions()]
    assert "Копировать" in texts and "▶ Проверить с шага 2" in texts and "Обработать запись" in texts
    assert next(a for a in menu.actions() if a.text() == "Отменить").isEnabled() is False
    menu.deleteLater()
    dlg.close()


def test_badge_follows_foreground_game_window(ctl, qapp, monkeypatch):
    from PySide6.QtCore import QRect, Qt

    from panel6.badge import Badge, badge_content

    blocked = {"on": False}
    badge = Badge(ctl, blocked=lambda: blocked["on"])
    flags = badge.windowFlags()
    assert flags & Qt.WindowTransparentForInput and flags & Qt.WindowDoesNotAcceptFocus  # не мешает игре
    monkeypatch.setattr(ctl, "game_rect", lambda: QRect(0, 0, 2560, 1440))
    fg = {"slot": None}
    monkeypatch.setattr(ctl, "foreground_slot", lambda: fg["slot"])
    badge.check()
    assert not badge.isVisible()  # впереди не окно игры
    ctl.macro_states[2] = {"state": "running", "macro": "Анти-АФК", "step": 3, "total": 9, "iteration": 1,
                           "count": 0}
    ctl.macro_states[4] = {"state": "error", "macro": "Сбор", "reason": "окно закрыто"}
    fg["slot"] = 2
    badge.check()
    assert badge.isVisible() and badge.slot == 2
    g = badge.geometry()
    assert g.right() == 2560 - 16 - 1 and g.top() == 16  # сверху справа по умолчанию
    title, line, marks = badge_content(ctl, 2)
    assert title == "2  Окно 2" and line[0].startswith("▶ «Анти-АФК» · шаг 3/9")
    assert [m[0] for m in marks] == ["1·", "2▶", "3·", "4⚠", "5·"] and marks[1][2] is True
    ctl.cfg["badge"]["corner"] = "bottom_left"
    badge.check()
    g = badge.geometry()
    assert g.left() == 16 and g.bottom() == 1440 - 16 - 1
    blocked["on"] = True  # открыта сетка
    badge.check()
    assert not badge.isVisible()
    blocked["on"] = False
    ctl.cfg["badge"]["enabled"] = False
    badge.check()
    assert not badge.isVisible()
    badge.timer.stop()


def test_event_log_collapses_and_limits(qapp):
    from panel6.widgets import EventLog

    ev = EventLog(limit=3)
    for i in range(5):
        ev.add(f"событие {i}")
    assert [line[10:] for line in ev.lines()] == ["событие 2", "событие 3", "событие 4"]
    ev.add("⚠ Горячие клавиши: ошибка")
    assert ev.list.item(2).foreground().color() == EventLog.WARN
    ev.set_collapsed(True)
    assert ev.last.text().endswith("⚠ Горячие клавиши: ошибка") and ev.toggle.text() == "Развернуть"
    ev.add("ещё")
    assert ev.last.text().endswith("ещё")


def test_main_window_logs_events(ctl, qapp):
    from panel6.app import MainWindow

    win = MainWindow(ctl)
    ctl.message.emit("Привет")
    for st in ({"state": "running", "macro": "А"}, {"state": "paused_user", "macro": "А"},
               {"state": "running", "macro": "А"}, {"state": "paused", "macro": "А"},
               {"state": "running", "macro": "А"}, {"state": "stopped", "macro": "А", "reason": "остановлен"},
               {"state": "error", "macro": "Б", "reason": "окно закрыто"}):
        ctl.macro_status.emit(3, st)
    ctl.statuses_changed.emit({1: "ready", 2: "off"})
    ctl.statuses_changed.emit({1: "off", 2: "starting"})
    texts = [line[10:] for line in win.events.lines()]
    assert texts == ["Привет", "Окно 3: ▶ «А» запущен", "Окно 3: ❚❚ «А» на паузе", "Окно 3: ▶ «А» продолжен",
                     "Окно 3: ■ «А» остановлен", "Окно 3: ⚠ «Б» ошибка: окно закрыто", "Окно 1: окно закрылось"]
    assert win.events.list.item(6).foreground().color() == win.events.WARN
    win.quitting = True
    win.badge.timer.stop()
    win.close()


def test_settings_save_badge(ctl, qapp):
    from panel6.app import SettingsDialog

    dlg = SettingsDialog(ctl.cfg)
    dlg.badge_on.setChecked(False)
    dlg.badge_corner.setCurrentIndex(dlg.badge_corner.findData("top_center"))
    dlg._save()
    assert dlg.result_cfg["badge"] == {"enabled": False, "corner": "top_center"}
    assert not dlg.badge_corner.isEnabled()


def test_window_polling_updates_statuses(ctl, qapp, monkeypatch):
    """Опрос окон в потоке доходит до карточек: номера окон в словаре статусов не теряются."""
    import threading

    for w, st in zip(ctl.windows, ("ready", "off", "starting", "off", "ready")):
        monkeypatch.setattr(w, "refresh", lambda st=st: st)
    got = []
    ctl.statuses_changed.connect(got.append)
    worker = threading.Thread(target=ctl._refresh_worker)  # как в программе — из фонового потока
    worker.start()
    worker.join()
    assert wait_until(qapp, lambda: bool(got))
    assert ctl.statuses == {1: "ready", 2: "off", 3: "starting", 4: "off", 5: "ready", 6: "ready"}
    assert got[-1] == ctl.statuses


def test_copying_steps_does_not_crash_on_exit(tmp_path):
    """Буфер обмена переживает Python: после копирования шагов программа должна закрываться без падения."""
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from PySide6.QtWidgets import QApplication\n"
        "app = QApplication([])\n"
        "from panel6.macro_ui import steps_from_clipboard, steps_to_clipboard\n"
        "steps_to_clipboard([{'t': 'wait', 'ms': 100, 'delay': 0}])\n"
        "assert steps_from_clipboard()[0]['ms'] == 100\n"
    )
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PANEL6_HOME=str(tmp_path))
    root = str(Path(__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-X", "dev", "-c", code, root], env=env, capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode(errors="replace")[-2000:]


def test_step_dialog_random_delay_and_table(ctl, qapp):
    from panel6.macro_ui import StepDialog
    from panel6.macros import normalize_step

    dlg = StepDialog(normalize_step({"t": "click", "x": 0.5, "y": 0.5, "delay": 150}), lambda: None)
    assert not dlg.delay_rand.isChecked() and not dlg.delay_max.isEnabled()
    dlg.delay_rand.setChecked(True)
    assert dlg.delay_max.isEnabled() and dlg.delay_max.value() == 300  # сразу заметный интервал
    dlg.delay_max.setValue(450)
    dlg._accept()
    assert (dlg.step["delay"], dlg.step["delay_max"]) == (150, 450)
    again = StepDialog(dlg.step, lambda: None)
    assert again.delay_rand.isChecked() and again.delay_max.value() == 450
    again.delay_rand.setChecked(False)
    again._accept()
    assert again.step["delay"] == 150 and "delay_max" not in again.step

    mdlg, m = _dialog_with_steps(ctl, [dlg.step, {"t": "wait", "ms": 100, "delay": 200}])
    assert mdlg.table.item(0, 2).text() == "150–450 мс" and mdlg.table.item(1, 2).text() == "200 мс"
    mdlg._select_rows([1])
    mdlg._process("jitter", 50)
    assert (m["steps"][1]["delay"], m["steps"][1]["delay_max"]) == (100, 300)
    assert mdlg.table.item(1, 2).text() == "100–300 мс"
    mdlg.close()


def test_long_texts_do_not_widen_windows(ctl, qapp):
    """Длинная строка журнала или ошибки не раздвигает панель: раньше она растягивалась на два монитора."""
    from panel6.app import MainWindow

    win = MainWindow(ctl)
    win.resize(1200, 800)
    win.show()
    qapp.processEvents()
    base, width = win.minimumSizeHint().width(), win.width()  # на крупном шрифте окно бывает шире 1200
    long = "⚠ Горячие клавиши: " + "; ".join(f"окно {i} — Num{i}: клавиша занята другой программой" for i in range(13))
    win.events.add(long)
    win.events.set_collapsed(True)
    win.events.add(long)
    ctl.macro_states[1] = {"state": "error", "macro": "А", "reason": "соединение потеряно " * 40}
    win.cards[0].refresh_macro()
    qapp.processEvents()
    assert win.minimumSizeHint().width() == base and win.width() == width
    last = win.events.last
    assert last.text().endswith(long) and last.toolTip() == last.text()  # целиком — в подсказке
    win.quitting = True
    win.badge.timer.stop()
    win.close()

    dlg, _m = _dialog_with_steps(ctl, _clicks(2))
    dlg.show()
    qapp.processEvents()
    base = dlg.minimumSizeHint().width()
    dlg.tool_note.setText("очень длинная заметка " * 100)
    dlg.run_status.setText("очень длинное состояние " * 100)
    qapp.processEvents()
    assert dlg.minimumSizeHint().width() == base
    dlg.close()


def test_tray_menu_has_no_launch_all(ctl, qapp):
    from panel6.app import MainWindow

    win = MainWindow(ctl)
    texts = [a.text() for a in win.tray_menu.actions()]
    assert texts == ["Показать панель", "Сетка всех окон", "Остановить все макросы", "", "Выход"]
    win.quitting = True
    win.badge.timer.stop()
    win.close()


def test_kill_switch_closes_windows_stops_macros_and_logs(ctl, qapp, monkeypatch):
    import threading

    from panel6.app import MainWindow

    win = MainWindow(ctl)
    killed = []
    for w, alive in zip(ctl.windows, (True, False, True, False, False)):
        monkeypatch.setattr(w, "kill", lambda w=w, alive=alive: killed.append(w.index) or alive)

    class Runner:
        active, reason = True, ""

        def stop(self, reason=""):
            self.active, self.reason = False, reason

        def join(self, timeout=None):
            pass

    runner = Runner()
    ctl.runners[2] = runner
    what = "сменилось подключение: ProtonVPN 10.2.0.2 → Ethernet 192.168.1.10"
    t = threading.Thread(target=ctl._on_net_change, args=(what,))  # как из потока наблюдения за сетью
    t.start()
    t.join()
    assert killed == [1, 2, 3, 4, 5] and "kill switch" in runner.reason  # закрыто сразу, ещё в том потоке
    assert wait_until(qapp, lambda: any("Kill switch" in line for line in win.events.lines()))
    assert win.events.lines()[-1].endswith(f"⚠ Kill switch: {what}. Закрыты окна: 1, 3")
    assert win.events.list.item(win.events.list.count() - 1).foreground().color() == win.events.WARN
    win._net_killed(what, [])
    assert win.events.lines()[-1].endswith("открытых окон не было")
    win.quitting = True
    win.badge.timer.stop()
    win.close()


def test_kill_switch_toggle_and_status_line(ctl, qapp, monkeypatch):
    from PySide6.QtWidgets import QPushButton

    from panel6 import controller as controller_module
    from panel6.app import MainWindow, SettingsDialog
    from panel6.netguard import NetState

    made = []

    class FakeGuard:
        def __init__(self, on_change, on_state, **kw):
            self.on_state, self.kw, self.stopped = on_state, kw, False
            self.state = NetState("10.2.0.2", "ProtonVPN")
            made.append(self)

        def start(self):
            self.on_state(self.state)

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(controller_module, "NetGuard", FakeGuard)
    win = MainWindow(ctl)
    ctl.start_netguard()
    qapp.processEvents()
    assert win.kill_on.isChecked() and win.net_line.text() == "ProtonVPN 10.2.0.2"
    assert made[-1].kw == {"check_public": True, "public_every_s": 5.0, "allowed": ["185.163.47.154"]}
    win.kill_on.setChecked(False)
    qapp.processEvents()
    assert ctl.cfg["kill_switch"]["enabled"] is False and ctl.netguard is None and made[-1].stopped
    assert win.net_line.text() == "выключен"

    dlg = SettingsDialog(ctl.cfg, current_ip="203.0.113.5")
    assert dlg.ks_allowed.text() == "185.163.47.154"  # IP по умолчанию
    dlg.ks_on.setChecked(True)
    dlg.ks_public.setChecked(False)
    dlg.ks_every.setValue(10)
    dlg.ks_allowed.setText("185.163.47.154; 300.1.1.1")
    monkeypatch.setattr("panel6.app.QMessageBox.warning", lambda *a: None)
    dlg._save()
    assert dlg.result() == 0  # неверный адрес — настройки не закрываются
    dlg.ks_allowed.clear()
    next(b for b in dlg.findChildren(QPushButton) if b.text() == "Взять текущий").click()
    dlg.ks_allowed.setText(dlg.ks_allowed.text() + ", 185.163.47.154")
    dlg._save()
    assert dlg.result_cfg["kill_switch"] == {"enabled": True, "public_ip": False, "interval_sec": 10.0,
                                             "allowed_ips": "203.0.113.5, 185.163.47.154"}
    win.quitting = True
    win.badge.timer.stop()
    win.close()


def test_launch_waits_for_allowed_ip(ctl, qapp, monkeypatch):
    from panel6 import controller as controller_module

    started, messages = [], []
    monkeypatch.setattr(ctl, "_start_windows", lambda slots: started.append(list(slots)))
    ctl.message.connect(messages.append)
    verdict = {"v": (False, "внешний IP 203.0.113.5, а разрешён только 185.163.47.154")}
    monkeypatch.setattr(controller_module, "check_network", lambda allowed: verdict["v"])
    ctl.launch_all()
    ctl.launch(2)  # пока идёт проверка — второй запуск не начинается
    assert wait_until(qapp, lambda: not ctl._checking_launch)
    assert started == [] and messages[-1] == "⚠ Окна не запущены: внешний IP 203.0.113.5, а разрешён только 185.163.47.154"
    assert "Уже проверяю внешний IP перед запуском…" in messages
    verdict["v"] = (True, "185.163.47.154")
    ctl.launch(3)
    assert wait_until(qapp, lambda: started)
    assert started == [[3]] and "Внешний IP 185.163.47.154 — разрешён" in messages
    ctl.cfg["kill_switch"]["allowed_ips"] = ""  # без списка — запуск сразу, без проверки
    ctl.launch(4)
    assert started[-1] == [4]


def test_window_count_and_enabled_from_settings(ctl, qapp, monkeypatch):
    """Число окон Chrome и рабочих столов задаётся в настройках, выключенные окна пропадают отовсюду."""
    from panel6 import app as app_mod
    from panel6.app import MainWindow, SettingsDialog
    from panel6.grid import grid_shape

    warnings = []
    monkeypatch.setattr(app_mod.QMessageBox, "warning", lambda *a: warnings.append(a[2]))

    win = MainWindow(ctl)
    assert [c.slot for c in win.cards] == [1, 2, 3, 4, 5, 6]
    dlg = SettingsDialog(ctl.cfg, win)
    dlg.win_count.setValue(7)
    assert len(dlg.win_rows) == 7 and dlg.extra_rows[0]["box"].title() == "Окно 8"
    dlg.win_rows[1]["enabled"].setChecked(False)
    dlg.win_rows[6]["hotkey"].setText("F7")
    dlg.extra_count.setValue(3)
    dlg.extra_rows[1]["mode"].setCurrentIndex(1)
    dlg.extra_rows[1]["title"].setCurrentText("Steam")
    dlg.extra_rows[2]["enabled"].setChecked(False)
    assert not dlg.extra_rows[0]["title"].isEnabled() and dlg.extra_rows[1]["title"].isEnabled()
    dlg._save()
    cfg = dlg.result_cfg
    assert len(cfg["windows"]) == 7 and cfg["windows"][1]["enabled"] is False
    assert [x["mode"] for x in cfg["extra_windows"]] == ["desktop", "window", "desktop"]
    assert cfg["extra_windows"][1]["window_title"] == "Steam" and cfg["extra_windows"][2]["enabled"] is False

    ctl.cfg.clear()
    ctl.cfg.update(cfg)
    ctl.sync_windows()
    win.build_cards()
    assert ctl.enabled_slots() == [1, 3, 4, 5, 6, 7, 8, 9]
    assert [c.slot for c in win.cards] == [1, 3, 4, 5, 6, 7, 8, 9]
    assert len(ctl.windows) == 7 and ctl.is_chrome(7) and ctl.is_desktop(8) and ctl.is_extra(9)
    assert not ctl.is_desktop(9) and ctl.slot_name(9) == "Рабочий стол 2"
    binds = ctl.bindings()
    assert "slot2" not in binds and "slot10" not in binds and {"slot7", "slot8"} <= set(binds)
    messages = []
    ctl.message.connect(messages.append)
    ctl.activate(2)
    assert messages[-1].endswith("окно выключено в настройках")
    assert grid_shape(len(ctl.enabled_slots())) == (3, 3) and grid_shape(6) == (3, 2) and grid_shape(4) == (2, 2)

    dlg = SettingsDialog(ctl.cfg, win)
    dlg.win_count.setValue(3)  # меньше окон — лишние скрываются, но не теряются, пока окно открыто
    dlg.win_count.setValue(4)
    assert dlg.win_rows[3]["name"].text() == "Окно 4"
    dlg.extra_rows[0]["hotkey"].setText("F7")  # уже у окна 7
    dlg.win_count.setValue(7)
    dlg.result_cfg = None
    dlg._save()
    assert dlg.result_cfg is None and "F7" in warnings[-1]  # повтор клавиши не сохранился
    win.quitting = True
    win.badge.timer.stop()
    win.close()


def test_launch_all_only_enabled(ctl, qapp, monkeypatch):
    ctl.cfg["windows"][0]["enabled"] = False
    started = []
    monkeypatch.setattr(ctl, "launch_slots", started.extend)
    ctl.launch_all()
    assert started == [2, 3, 4, 5]


def test_kill_switch_covers_windows_removed_from_settings(qapp, tmp_path, monkeypatch):
    """Окно 8 убрали из настроек, а его Chrome остался открытым: после перезапуска панели kill switch закроет и его."""
    monkeypatch.setenv("PANEL6_HOME", str(tmp_path))
    from panel6 import config
    from panel6.controller import Controller

    (tmp_path / "profiles" / "w8").mkdir(parents=True)
    c = Controller(config.load())
    try:
        assert len(c.windows) == 5 and 8 in c._chrome and 6 not in c._chrome
        assert c.enabled_chrome_slots() == [1, 2, 3, 4, 5]  # на панели убранного окна нет
        for w in c._chrome.values():
            monkeypatch.setattr(w, "kill", lambda: True)
        assert c.kill_windows("тест") == [1, 2, 3, 4, 5, 8]
    finally:
        c.shutdown()
