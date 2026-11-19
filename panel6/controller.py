"""Логика панели: слоты 1..N — копии Chrome, за ними — рабочие столы и окна выбранных программ.

Сколько окон каждого вида и какие из них включены — задаётся в настройках.

Здесь же запуск макросов в окнах, запись, наборы настроек и все горячие клавиши.
"""

from __future__ import annotations

import copy
import datetime
import logging
import threading

from PySide6.QtCore import QObject, QRect, QTimer, Signal
from PySide6.QtGui import QGuiApplication

from . import config, win32
from .chrome import ChromeWindow, find_chrome
from .keys import HotkeyError, parse_hotkey
from .macros import MacroRunner, new_macro, repeat_for
from .netguard import NetGuard, check_network, parse_allowed
from .presets import PresetStore
from .recorder import Recorder

log = logging.getLogger(__name__)

HOTKEY_TITLES = {
    "grid": "сетка", "back": "назад", "next": "следующее окно", "prev": "предыдущее окно",
    "macro_toggle": "старт/пауза макроса", "stop_all": "остановить все макросы", "record": "запись макроса",
}


def ordered_screens():
    """Мониторы: сначала основной, затем остальные слева направо."""
    screens = list(QGuiApplication.screens())
    primary = QGuiApplication.primaryScreen()
    rest = sorted((s for s in screens if s is not primary), key=lambda s: (s.geometry().x(), s.geometry().y()))
    return ([primary] if primary else []) + rest


class SlotHistory:
    """Текущее и предыдущее окно — для «назад» и перебора по кругу.

    slots — число окон (1..count) или функция, которая возвращает список включённых окон.
    """

    def __init__(self, slots=6):
        self._slots = slots
        self.current: int | None = None
        self.previous: int | None = None

    def slots(self) -> list[int]:
        s = self._slots
        return list(s()) if callable(s) else list(range(1, s + 1))

    def visit(self, slot: int) -> None:
        if slot != self.current:
            self.previous, self.current = self.current, slot

    def back(self) -> int | None:
        return self.previous if self.previous in self.slots() else None

    def step(self, delta: int) -> int | None:
        slots = self.slots()
        if not slots:
            return None
        if self.current not in slots:
            if self.current is None:
                return slots[0] if delta > 0 else slots[-1]
            # текущее окно выключили: ближайшее включённое в нужную сторону
            after = [s for s in slots if (s > self.current if delta > 0 else s < self.current)]
            return (after[0] if delta > 0 else after[-1]) if after else (slots[0] if delta > 0 else slots[-1])
        return slots[(slots.index(self.current) + delta) % len(slots)]


class Controller(QObject):
    hotkey_pressed = Signal(str)       # из потока горячих клавиш в главный поток
    # словари передаются как object: Signal(dict) превращает их в QVariantMap и теряет числовые ключи слотов
    statuses_changed = Signal(object)  # {слот: "off"|"starting"|"ready"|"missing"}
    current_changed = Signal(int)
    grid_toggle_requested = Signal()
    message = Signal(str)
    macro_status = Signal(int, object)  # слот, состояние макроса (из потока макроса)
    recording_changed = Signal(int, int)  # слот записи (0 — нет записи), сколько действий записано
    macros_changed = Signal()          # изменился набор или его макросы
    net_state = Signal(object)         # NetState: текущее подключение для строки kill switch (None — выключен)
    net_killed = Signal(str, object)   # kill switch сработал: что изменилось, какие окна закрыты
    _launch_checked = Signal(object, object)  # окна для запуска, (можно ли, внешний IP или причина отказа)
    _refreshed = Signal(object)

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self._chrome: dict[int, ChromeWindow] = {}  # все когда-либо созданные копии Chrome, в т. ч. убранные из настроек
        self.windows: list[ChromeWindow] = []
        self.sync_windows()
        self.history = SlotHistory(self.enabled_slots)
        self.statuses: dict[int, str] = {}
        self.parked = False  # окна Chrome убраны ради рабочего стола
        self.hotkeys: win32.HotkeyThread | None = None
        self.hotkey_errors: list[str] = []
        self.hotkey_pressed.connect(self._on_hotkey)
        self._refreshing = False
        self._refreshed.connect(self._apply_statuses)

        self.presets = PresetStore(config.app_dir() / "presets")
        name = cfg.get("active_preset") or ""
        if not name or not self.presets.exists(name):
            name = self.presets.ensure_default()
        self.preset = self.presets.load(name)
        self.cfg["active_preset"] = self.preset["name"]
        self.runners: dict[int, MacroRunner] = {}
        self.macro_states: dict[int, dict] = {}
        self.macro_status.connect(self._remember_status)
        self.recorder: Recorder | None = None
        self.recording_slot = 0
        self.recording_count = 0
        self.recording_changed.connect(self._remember_recording)
        self._record_sink = None
        self.grid_focus_slot: int | None = None
        self.netguard: NetGuard | None = None
        self.net_killed.connect(self._after_kill)
        self._checking_launch = False
        self._launch_checked.connect(self._launch_after_check)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)

    # ------------------------------------------------------------ мониторы
    def game_rect(self) -> QRect:
        screens = ordered_screens()
        idx = self.cfg.get("game_monitor", 0)
        screen = screens[idx] if 0 <= idx < len(screens) else screens[0]
        return screen.geometry()

    def panel_screen(self):
        screens = ordered_screens()
        idx = self.cfg.get("panel_monitor", 1)
        return screens[idx] if 0 <= idx < len(screens) else screens[-1]

    # ------------------------------------------------------------ окна из настроек
    def sync_windows(self) -> None:
        """Число окон Chrome изменилось в настройках: добавить новые, убрать лишние (их макросы останавливаются).

        Убранные копии Chrome не закрываются, но остаются под присмотром kill switch.
        """
        n = len(self.cfg["windows"])
        for i in range(1, n + 1):
            if i not in self._chrome:
                self._chrome[i] = ChromeWindow(i)
        for i, r in list(getattr(self, "runners", {}).items()):
            if i > n and r.active:
                r.stop("окно убрано в настройках")
        self.windows = [self._chrome[i] for i in range(1, n + 1)]
        if hasattr(self, "history"):
            for attr in ("current", "previous"):
                if getattr(self.history, attr) not in self.enabled_slots():
                    setattr(self.history, attr, None)
        if getattr(self, "grid_focus_slot", None) not in (None, *self.enabled_slots()):
            self.grid_focus_slot = None

    def chrome_count(self) -> int:
        return len(self.cfg["windows"])

    def slot_count(self) -> int:
        return len(self.cfg["windows"]) + len(self.cfg["extra_windows"])

    def is_chrome(self, slot: int) -> bool:
        return 1 <= slot <= self.chrome_count()

    def is_extra(self, slot: int) -> bool:
        return self.chrome_count() < slot <= self.slot_count()

    def slot_cfg(self, slot: int) -> dict:
        n = self.chrome_count()
        return self.cfg["windows"][slot - 1] if slot <= n else self.cfg["extra_windows"][slot - n - 1]

    def slot_enabled(self, slot: int) -> bool:
        return 1 <= slot <= self.slot_count() and bool(self.slot_cfg(slot).get("enabled", True))

    def enabled_slots(self) -> list[int]:
        return [s for s in range(1, self.slot_count() + 1) if self.slot_enabled(s)]

    def enabled_chrome_slots(self) -> list[int]:
        return [s for s in range(1, self.chrome_count() + 1) if self.slot_enabled(s)]

    def is_desktop(self, slot: int) -> bool:
        """Окно-«рабочий стол» (а не окно Chrome или программы)."""
        return self.is_extra(slot) and self.slot_cfg(slot).get("mode") == "desktop"

    def slot_hotkey(self, slot: int) -> str:
        return self.slot_cfg(slot).get("hotkey", "") if 1 <= slot <= self.slot_count() else ""

    # ------------------------------------------------------------ горячие клавиши
    def hotkey_title(self, name: str) -> str:
        if name.startswith("slot"):
            return f"окно {name[4:]}"
        if name.startswith("macro:"):
            m = self.macro(name[6:])
            return f"макрос «{m['name']}»" if m else "макрос"
        return HOTKEY_TITLES.get(name, name)

    def bindings(self) -> dict[str, tuple[int, int]]:
        """Все горячие клавиши; ошибочные и повторяющиеся пропускаются и попадают в hotkey_errors."""
        raw = {f"slot{s}": self.slot_hotkey(s) for s in self.enabled_slots()}
        raw.update(self.cfg["hotkeys"])
        for m in self.preset["macros"]:
            raw[f"macro:{m['id']}"] = m["hotkey"]
        out, seen, errors = {}, {}, []
        for name, text in raw.items():
            if not text.strip():
                continue
            try:
                combo = parse_hotkey(text)
            except HotkeyError as e:
                errors.append(f"{self.hotkey_title(name)} — {text}: {e}")
                continue
            if combo in seen:
                errors.append(f"{self.hotkey_title(name)} — {text}: уже назначено на «{self.hotkey_title(seen[combo])}»")
                continue
            seen[combo] = name
            out[name] = combo
        self.hotkey_errors = errors
        return out

    def start_hotkeys(self) -> None:
        self.stop_hotkeys()
        binds = self.bindings()
        self.hotkeys = win32.HotkeyThread(binds, self.hotkey_pressed.emit)
        self.hotkeys.start()
        self.hotkeys.ready.wait(2)
        for name in self.hotkeys.failed:
            self.hotkey_errors.append(f"{self.hotkey_title(name)}: клавиша занята другой программой")
        if self.hotkey_errors:
            self.message.emit("⚠ Горячие клавиши: " + "; ".join(self.hotkey_errors))

    def stop_hotkeys(self) -> None:
        if self.hotkeys:
            self.hotkeys.stop()
            self.hotkeys = None

    def _on_hotkey(self, name: str) -> None:
        log.info("горячая клавиша: %s", name)
        if name.startswith("slot"):
            self.activate(int(name[4:]))
        elif name == "grid":
            self.grid_toggle_requested.emit()
        elif name == "back":
            prev = self.history.back()
            if prev:
                self.activate(prev)
        elif name in ("next", "prev"):
            slot = self.history.step(+1 if name == "next" else -1)
            if slot:
                self.activate(slot)
        elif name == "macro_toggle":
            self.toggle_current_macro()
        elif name == "stop_all":
            self.stop_all()
        elif name == "record":
            self.toggle_recording()
        elif name.startswith("macro:"):
            self._macro_hotkey(name[6:])

    # ------------------------------------------------------------ состояние окон
    def refresh(self) -> None:
        """Опрос окон в фоновом потоке, чтобы интерфейс не подвисал."""
        if self._refreshing:
            return
        self._refreshing = True
        threading.Thread(target=self._refresh_worker, daemon=True, name="refresh").start()

    def _refresh_worker(self) -> None:
        statuses = {}
        try:
            for w in list(self.windows):
                try:
                    statuses[w.index] = w.refresh()
                except Exception as e:  # noqa: BLE001
                    log.warning("окно %s: %s", w.index, e)
                    statuses[w.index] = "off"
            n = len(self.windows)
            for i, x in enumerate(list(self.cfg["extra_windows"]), start=n + 1):
                if not x.get("enabled", True):
                    continue
                ok = x.get("mode") == "desktop" or self._extra_hwnd(x)
                statuses[i] = "ready" if ok else "missing"
        finally:
            try:
                self._refreshed.emit(statuses)
            except RuntimeError:  # панель уже закрылась, пока шёл опрос
                pass

    def _apply_statuses(self, statuses: dict) -> None:
        self._refreshing = False
        if statuses and statuses != self.statuses:
            self.statuses = statuses
            self.statuses_changed.emit(statuses)

    def chrome_pids(self) -> set[int]:
        return {w.pid for w in list(self._chrome.values()) if w.pid}

    def _extra_hwnd(self, x: dict) -> int | None:
        if x.get("mode") != "window" or not x.get("window_title", "").strip():
            return None
        return win32.find_window_by_title(x["window_title"], self.chrome_pids())

    def slot_hwnd(self, slot: int) -> int | None:
        if self.is_extra(slot):
            return self._extra_hwnd(self.slot_cfg(slot))
        if not self.is_chrome(slot):
            return None
        w = self.windows[slot - 1]
        return w.hwnd if win32.is_window(w.hwnd) else None

    def slot_name(self, slot: int) -> str:
        if 1 <= slot <= self.slot_count():
            return self.slot_cfg(slot)["name"]
        return f"Окно {slot}"

    def foreground_slot(self) -> int | None:
        """Окно Chrome, которое сейчас на переднем плане; None — впереди другая программа."""
        fg = win32.foreground_window()
        return next((w.index for w in self.windows if fg and w.hwnd == fg), None)

    def current_chrome_slot(self) -> int | None:
        """Окно Chrome, с которым ты сейчас работаешь: активное окно, выбранное в сетке или последнее открытое."""
        slot = self.foreground_slot()
        if slot:
            return slot
        if self.grid_focus_slot and self.is_chrome(self.grid_focus_slot):
            return self.grid_focus_slot
        cur = self.history.current
        return cur if cur and self.is_chrome(cur) else None

    # ------------------------------------------------------------ запуск окон
    def allowed_ips(self) -> list[str]:
        return parse_allowed(self.cfg["kill_switch"].get("allowed_ips", ""))[0]

    def launch(self, slot: int) -> None:
        self.launch_slots([slot])

    def launch_all(self) -> None:
        slots = self.enabled_chrome_slots()
        if not slots:
            self.message.emit("Нет включённых окон Chrome (Настройки → Окна Chrome)")
            return
        self.launch_slots(slots)

    def launch_slots(self, slots: list[int]) -> None:
        """Запуск окон. Если заданы разрешённые IP — только после свежей проверки внешнего IP (в фоне)."""
        allowed = self.allowed_ips()
        if not allowed:
            self._start_windows(slots)
            return
        if self._checking_launch:
            self.message.emit("Уже проверяю внешний IP перед запуском…")
            return
        self._checking_launch = True
        self.message.emit("Проверяю внешний IP перед запуском…")
        threading.Thread(target=lambda: self._launch_checked.emit(slots, check_network(allowed)), daemon=True,
                         name="launch-check").start()

    def _launch_after_check(self, slots: list[int], verdict: tuple[bool, str]) -> None:
        self._checking_launch = False
        ok, text = verdict
        if not ok:
            self.message.emit(f"⚠ Окна не запущены: {text}")
            return
        self.message.emit(f"Внешний IP {text} — разрешён")
        self._start_windows(slots)
        if self.netguard:
            self.netguard.wake()

    def _start_windows(self, slots: list[int]) -> None:
        chrome = find_chrome(self.cfg.get("chrome_path", ""))
        if not chrome:
            self.message.emit("⚠ Не нашёл Chrome. Укажи путь к chrome.exe в настройках.")
            return
        r = self.game_rect()
        for slot in slots:
            if not self.is_chrome(slot):
                continue
            self.windows[slot - 1].launch(chrome, self.cfg["windows"][slot - 1]["url"],
                                          (r.x(), r.y(), r.width(), r.height()))
            self.message.emit(f"Запускаю {self.slot_name(slot)}…")

    def fullscreen(self, slot: int) -> None:
        if not self.is_chrome(slot):
            return
        r = self.game_rect()
        w = self.windows[slot - 1]
        try:
            w.place_fullscreen(r.x(), r.y(), r.width(), r.height())
        except Exception as e:  # noqa: BLE001
            self.message.emit(f"⚠ {self.slot_name(slot)}: не удалось развернуть ({e})")

    # ------------------------------------------------------------ переключение
    def _unpark(self) -> None:
        if not self.parked:
            return
        r = self.game_rect()
        for w in list(self._chrome.values()):
            if not win32.is_window(w.hwnd):
                continue
            if win32.is_minimized(w.hwnd):
                win32.restore(w.hwnd)
            else:
                win32.move_window(w.hwnd, r.x(), r.y())
        self.parked = False

    def _park(self, strategy: str = "offscreen") -> None:
        x = win32.virtual_screen_right() + 200
        y = self.game_rect().y()
        for w in list(self._chrome.values()):
            if not win32.is_window(w.hwnd):
                continue
            if strategy == "minimize":
                win32.minimize_no_activate(w.hwnd)
            else:
                win32.move_window(w.hwnd, x, y)
        self.parked = True

    def activate(self, slot: int) -> None:
        if not 1 <= slot <= self.slot_count():
            return
        if not self.slot_enabled(slot):
            self.message.emit(f"{self.slot_name(slot)}: окно выключено в настройках")
            return
        if self.is_desktop(slot):
            self._park(self.slot_cfg(slot).get("desktop_strategy", "offscreen"))
            win32.show_desktop_shell()
            self._visited(slot)
            return
        hwnd = self.slot_hwnd(slot)
        if not hwnd:
            what = "окно не найдено (проверь название в настройках)" if self.is_extra(slot) else "не запущено"
            self.message.emit(f"{self.slot_name(slot)}: {what}")
            return
        if self.is_chrome(slot):
            self._unpark()
        win32.activate(hwnd)
        self._visited(slot)

    def _visited(self, slot: int) -> None:
        self.history.visit(slot)
        self.current_changed.emit(slot)

    # ------------------------------------------------------------ наборы
    def save_preset(self) -> None:
        self.presets.save(self.preset)

    def preset_edited(self) -> None:
        """Макросы набора изменились в редакторе: сохранить и перерегистрировать клавиши."""
        self.save_preset()
        self.macros_changed.emit()

    def switch_preset(self, name: str) -> None:
        if not self.presets.exists(name):
            self.message.emit(f"Набор «{name}» не найден")
            return
        self.preset = self.presets.load(name)
        self.cfg["active_preset"] = self.preset["name"]
        config.save(self.cfg)
        self.macros_changed.emit()
        self.start_hotkeys()
        self.message.emit(f"Набор «{self.preset['name']}»")

    def reload_preset(self) -> None:
        self.switch_preset(self.preset["name"])

    def macro(self, mid: str) -> dict | None:
        return next((m for m in self.preset["macros"] if m["id"] == mid), None)

    def main_macro_id(self, slot: int) -> str:
        return self.preset["window_macros"].get(str(slot), "")

    def set_main_macro(self, slot: int, mid: str) -> None:
        if self.preset["window_macros"].get(str(slot), "") != mid:
            self.preset["window_macros"][str(slot)] = mid
            self.save_preset()

    # ------------------------------------------------------------ макросы
    def user_active(self, slot: int) -> bool:
        """Ты сейчас действуешь в этом окне: было касание мыши или клавиатуры, пока оно активно."""
        idle_ms = float(self.cfg["macro_user_input"].get("idle_sec", 1.0)) * 1000
        if win32.last_input_age_ms() >= idle_ms or not self.is_chrome(slot):
            return False
        w = self.windows[slot - 1]
        fg = win32.foreground_window()
        return bool((fg and w.hwnd == fg) or self.grid_focus_slot == slot)

    def _remember_status(self, slot: int, status: dict) -> None:
        self.macro_states[slot] = status

    def _remember_recording(self, slot: int, count: int) -> None:
        self.recording_count = count if slot else 0

    def runner(self, slot: int) -> MacroRunner | None:
        r = self.runners.get(slot)
        return r if r and r.active else None

    def run_macro(self, slot: int, mid: str, quiet: bool = False, start: int = 0) -> bool:
        """Запускает макрос в окне; start — с какого шага начать первый проход."""
        m = self.macro(mid)
        if not m:
            self.message.emit("Макрос не найден")
            return False
        if not self.is_chrome(slot):
            if not quiet:
                self.message.emit(f"Окна Chrome {slot} нет (Настройки → Окна Chrome)")
            return False
        w = self.windows[slot - 1]
        if not w.connected:
            if not quiet:
                self.message.emit(f"{self.slot_name(slot)}: окно не запущено")
            return False
        if self.recording_slot == slot:
            self.message.emit(f"{self.slot_name(slot)}: сейчас идёт запись макроса")
            return False
        if not m["steps"]:
            self.message.emit(f"Макрос «{m['name']}» пустой")
            return False
        self.stop_macro(slot, wait=True)
        count, gap = repeat_for(m, slot)
        policy = m["user_input"] if m["user_input"] != "default" else self.cfg["macro_user_input"]["mode"]
        runner = MacroRunner(w, copy.deepcopy(m), count=count, gap_ms=gap, policy=policy,
                             activity=lambda: self.user_active(slot),
                             on_status=lambda st: self.macro_status.emit(slot, st), start=start)
        self.runners[slot] = runner
        runner.start()
        log.info("макрос «%s» в окне %s", m["name"], slot)
        return True

    def run_macro_all(self, mid: str) -> None:
        m = self.macro(mid)
        if not m:
            return
        targets = [s for s in m["windows"] if s in self.enabled_chrome_slots()]
        started = [slot for slot in targets if self.run_macro(slot, mid, quiet=True)]
        skipped = [str(s) for s in targets if s not in started]
        text = f"«{m['name']}» запущен в окнах: {', '.join(map(str, started)) or 'ни в одном'}"
        if skipped:
            text += f" (не запущены: {', '.join(skipped)})"
        self.message.emit(text)

    def stop_macro(self, slot: int, wait: bool = False) -> None:
        r = self.runners.get(slot)
        if r and r.active:
            r.stop("остановлен")
            if wait:
                r.join(2)

    def stop_all(self) -> None:
        active = [s for s, r in self.runners.items() if r.active]
        for s in active:
            self.runners[s].stop("остановлен кнопкой «стоп всё»")
        if self.recorder:
            self.stop_recording()
        self.message.emit("Все макросы остановлены" if active else "Макросы не запущены")

    def toggle_pause(self, slot: int) -> None:
        r = self.runner(slot)
        if r:
            r.toggle_pause()

    def toggle_current_macro(self) -> None:
        slot = self.current_chrome_slot()
        if not slot:
            self.message.emit("Не понятно, в каком окне: открой окно клавишей или выбери его в сетке")
            return
        r = self.runner(slot)
        if r:
            r.toggle_pause()
            return
        mid = self.main_macro_id(slot)
        if not mid:
            self.message.emit(f"{self.slot_name(slot)}: не выбран основной макрос (выбери его на карточке окна)")
            return
        self.run_macro(slot, mid)

    def _macro_hotkey(self, mid: str) -> None:
        m = self.macro(mid)
        if not m:
            return
        if m["hotkey_target"] == "all":
            running = [s for s, r in self.runners.items() if r.active and r.macro["id"] == mid]
            if running:
                for s in running:
                    self.stop_macro(s)
                self.message.emit(f"«{m['name']}» остановлен")
            else:
                self.run_macro_all(mid)
            return
        slot = self.current_chrome_slot()
        if not slot:
            self.message.emit("Не понятно, в каком окне запускать макрос")
            return
        r = self.runner(slot)
        if r and r.macro["id"] == mid:
            self.stop_macro(slot)
        else:
            self.run_macro(slot, mid)

    # ------------------------------------------------------------ запись
    def start_recording(self, slot: int, sink=None) -> bool:
        """Начинает запись в окне. sink(steps) получит шаги; без sink запись станет новым макросом."""
        if self.recorder:
            self.message.emit("Запись уже идёт")
            return False
        if not self.is_chrome(slot):
            return False
        w = self.windows[slot - 1]
        if not w.connected:
            self.message.emit(f"{self.slot_name(slot)}: окно не запущено")
            return False
        self.stop_macro(slot, wait=True)
        rec = Recorder(w, on_count=lambda n: self.recording_changed.emit(slot, n))
        try:
            rec.start()
        except Exception as e:  # noqa: BLE001
            self.message.emit(f"⚠ Не удалось начать запись: {e}")
            return False
        self.recorder, self.recording_slot, self._record_sink = rec, slot, sink
        self.recording_changed.emit(slot, 0)
        key = self.cfg["hotkeys"].get("record") or "кнопкой в панели"
        self.message.emit(f"Идёт запись в окне {slot}. Остановить — {key}")
        return True

    def stop_recording(self) -> list[dict] | None:
        rec = self.recorder
        if not rec:
            return None
        slot, sink = self.recording_slot, self._record_sink
        steps = rec.stop()
        self.recorder, self.recording_slot, self._record_sink = None, 0, None
        self.recording_changed.emit(0, 0)
        if not steps:
            self.message.emit("Ничего не записано" + (f": {rec.error}" if rec.error else ""))
        elif sink:
            sink(steps)
        else:
            m = new_macro(f"Запись {datetime.datetime.now():%H:%M:%S}")
            m["steps"] = steps
            self.preset["macros"].append(m)
            self.preset_edited()
            self.message.emit(f"Записан макрос «{m['name']}» из окна {slot}: {len(steps)} шагов")
        return steps

    def toggle_recording(self) -> None:
        if self.recorder:
            self.stop_recording()
            return
        slot = self.current_chrome_slot()
        if not slot:
            self.message.emit("Не понятно, в каком окне записывать: открой окно клавишей")
            return
        self.start_recording(slot)

    # ------------------------------------------------------------ kill switch
    def start_netguard(self) -> None:
        """Включить или перезапустить наблюдение за сетью по настройкам."""
        self.stop_netguard()
        ks = self.cfg["kill_switch"]
        if not ks.get("enabled"):
            self.net_state.emit(None)
            return
        self.netguard = NetGuard(self._on_net_change, self.net_state.emit, check_public=ks.get("public_ip", True),
                                 public_every_s=max(1.0, float(ks.get("interval_sec", 5.0))),
                                 allowed=self.allowed_ips())
        self.netguard.start()
        log.info("kill switch включён: %s", self.netguard.state.text())

    def stop_netguard(self) -> None:
        if self.netguard:
            self.netguard.stop()
            self.netguard = None

    def _on_net_change(self, what: str) -> None:
        """Из потока наблюдения: окна закрываются сразу, интерфейс узнаёт уже после."""
        self.net_killed.emit(what, self.kill_windows("kill switch: сменилось подключение к интернету"))

    def kill_windows(self, reason: str) -> list[int]:
        """Останавливает макросы и мгновенно завершает все окна Chrome. Возвращает номера закрытых окон."""
        for r in list(self.runners.values()):
            if r.active:
                r.stop(reason)
        return [w.index for w in list(self._chrome.values()) if w.kill()]

    def _after_kill(self, _what: str, _closed: list) -> None:
        if self.recorder:
            self.stop_recording()
        self.refresh()

    # ------------------------------------------------------------ выход
    def shutdown(self) -> None:
        self.timer.stop()
        self.stop_netguard()
        self.stop_hotkeys()
        if self.recorder:
            self.recorder.stop()
            self.recorder = None
        for r in self.runners.values():
            if r.active:
                r.stop()
        for r in self.runners.values():
            r.join(2)
        self._unpark()
