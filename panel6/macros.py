"""Макросы: формат шагов, описание для редактора и движок воспроизведения.

Координаты хранятся в долях страницы (0..1), поэтому макрос работает при любом размере окна.
Каждый шаг выполняется после своей паузы ``delay`` (мс); если задан ``delay_max``, пауза каждый раз
случайная в интервале ``delay``…``delay_max``. Шаги с длительностью (нажатие клавиши,
поворот камеры, пауза) держат время сами. ``loop``/``endloop`` — повтор блока шагов.
``random`` — случайный ввод: заданное время через случайные промежутки выполняет случайное действие
из выбранных видов (клик, кнопка мыши, движение, поворот камеры, колесо, клавиша).
"""

from __future__ import annotations

import copy
import logging
import random
import secrets
import threading
import time
from typing import Callable, Protocol

log = logging.getLogger(__name__)

BUTTONS = {"left": "ЛКМ", "right": "ПКМ", "middle": "СКМ"}
_BUTTON_BIT = {"left": 1, "right": 2, "middle": 4}
# модификаторы для протокола Chrome: Alt=1, Ctrl=2, Meta=4, Shift=8
_MODIFIER_BITS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}

STEP_TYPES: dict[str, str] = {
    "click": "Клик",
    "down": "Зажать кнопку мыши",
    "up": "Отпустить кнопку мыши",
    "move": "Вести мышь",
    "look": "Поворот камеры",
    "wheel": "Колесо мыши",
    "key": "Клавиша",
    "text": "Ввод текста",
    "wait": "Пауза",
    "loop": "Повторить блок",
    "endloop": "Конец блока",
    "random": "Случайный ввод",
}

# какие действия умеет случайный ввод (всё, кроме текста, пауз и блоков)
RANDOM_KINDS: dict[str, str] = {
    "click": "клик",
    "down": "зажать кнопку мыши",
    "up": "отпустить кнопку мыши",
    "move": "вести мышь",
    "look": "поворот камеры",
    "wheel": "колесо",
    "key": "клавиша",
}

USER_INPUT_MODES = {"pause": "поставить на паузу", "keep": "продолжать", "stop": "остановить"}
MAX_REPEAT = 1_000_000
MIN_ITERATION_S = 0.02  # повтор без пауз не должен крутиться быстрее, чем 50 раз в секунду


# ------------------------------------------------------------------ формат

def new_id() -> str:
    return "m_" + secrets.token_hex(4)


def new_macro(name: str = "Новый макрос") -> dict:
    return normalize_macro({"id": new_id(), "name": name})


def _num(value, default, lo=None, hi=None, cast=float):
    try:
        v = cast(value)
    except (TypeError, ValueError):
        return default
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


def _coord(value):
    """Координата в долях страницы или None — «на текущем месте курсора»."""
    if value is None:
        return None
    return _num(value, 0.5, 0.0, 1.0)


def normalize_step(step: dict) -> dict:
    """Проверяет шаг и дополняет значения по умолчанию. Неизвестный тип -> ValueError."""
    if not isinstance(step, dict) or step.get("t") not in STEP_TYPES:
        raise ValueError(f"неизвестный шаг: {step!r}")
    t = step["t"]
    out = {"t": t, "delay": _num(step.get("delay"), 0, 0, 3_600_000, int)}
    if step.get("delay_max") is not None:  # случайная пауза перед шагом: от delay до delay_max
        lo, hi = sorted((out["delay"], _num(step["delay_max"], 0, 0, 3_600_000, int)))
        if hi > lo:
            out["delay"], out["delay_max"] = lo, hi
    if t in ("click", "down", "up", "wheel"):
        out["x"], out["y"] = _coord(step.get("x")), _coord(step.get("y"))
        if (out["x"] is None) != (out["y"] is None):
            out["x"] = out["y"] = None
    if t == "move":
        out["x"], out["y"] = _coord(step.get("x", 0.5)), _coord(step.get("y", 0.5))
        if out["x"] is None or out["y"] is None:
            out["x"] = out["y"] = 0.5
    if t in ("click", "down", "up"):
        out["button"] = step.get("button") if step.get("button") in BUTTONS else "left"
    if t == "click":
        out["count"] = 2 if step.get("count") == 2 else 1
    if t == "look":
        out["dx"] = _num(step.get("dx"), 0.0, -100_000, 100_000)
        out["dy"] = _num(step.get("dy"), 0.0, -100_000, 100_000)
        out["ms"] = _num(step.get("ms"), 0, 0, 600_000, int)
    if t == "wheel":
        out["dy"] = _num(step.get("dy"), 120.0, -100_000, 100_000)
    if t == "key":
        out["code"] = str(step.get("code") or "")
        out["key"] = str(step.get("key") or "")
        out["vk"] = _num(step.get("vk"), 0, 0, 255, int)
        out["action"] = step.get("action") if step.get("action") in ("press", "down", "up") else "press"
        out["ms"] = _num(step.get("ms"), 50, 0, 600_000, int)
        if not out["code"] and not out["key"]:
            raise ValueError("клавиша не задана")
    if t == "text":
        out["text"] = str(step.get("text") or "")
    if t == "wait":
        out["ms"] = _num(step.get("ms"), 1000, 0, 86_400_000, int)
    if t == "loop":
        out["times"] = _num(step.get("times"), 2, 1, MAX_REPEAT, int)
    if t == "random":
        out.update(_normalize_random(step))
    return out


def _key_spec(k) -> dict | None:
    if isinstance(k, dict) and (k.get("code") or k.get("key")):
        return {"code": str(k.get("code") or ""), "key": str(k.get("key") or ""), "vk": _num(k.get("vk"), 0, 0, 255, int)}
    return None


def _normalize_random(step: dict) -> dict:
    lo = _num(step.get("min_ms"), 50, 1, 600_000, int)
    hi = _num(step.get("max_ms"), lo, 1, 600_000, int)
    keys, seen = [], set()
    for k in step.get("keys") or []:
        spec = _key_spec(k)
        if spec and (spec["code"] or spec["key"]) not in seen:
            seen.add(spec["code"] or spec["key"])
            keys.append(spec)
    kinds = []
    for k in step.get("kinds") or []:
        if k in RANDOM_KINDS and k not in kinds and (k != "key" or keys):
            kinds.append(k)
    area = step.get("area", [0.0, 0.0, 1.0, 1.0])
    if area is not None:
        try:
            x1, y1, x2, y2 = (_num(v, 0.0, 0.0, 1.0) for v in area)
            area = [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
        except (TypeError, ValueError):
            area = [0.0, 0.0, 1.0, 1.0]
    buttons = [b for b in BUTTONS if b in (step.get("buttons") or ["left"])] or ["left"]
    hmin = _num(step.get("hold_min"), 30, 0, 600_000, int)
    hmax = _num(step.get("hold_max"), 120, 0, 600_000, int)
    return {
        "ms": _num(step.get("ms"), 3000, 0, 86_400_000, int),      # сколько длится случайный ввод
        "min_ms": min(lo, hi), "max_ms": max(lo, hi),             # промежуток между действиями
        "kinds": kinds or ["click"],
        "area": area,                                             # None — на текущем месте курсора
        "buttons": buttons,
        "keys": keys,
        "hold_min": min(hmin, hmax), "hold_max": max(hmin, hmax),  # сколько держать клавишу
        "look_dx": _num(step.get("look_dx"), 100.0, 0, 100_000),  # поворот камеры до ±look_dx
        "look_dy": _num(step.get("look_dy"), 30.0, 0, 100_000),
        "wheel": _num(step.get("wheel"), 120.0, 1, 100_000),      # колесо до ±wheel
    }


def normalize_macro(m: dict) -> dict:
    steps = []
    for s in m.get("steps") or []:
        try:
            steps.append(normalize_step(s))
        except ValueError as e:
            log.warning("макрос %s: пропущен шаг: %s", m.get("name"), e)
    rep = m.get("repeat") or {}
    windows = [w for w in (m.get("windows") or [1, 2, 3, 4, 5]) if isinstance(w, int) and 1 <= w <= 5]
    overrides = {}
    for key, ov in (m.get("overrides") or {}).items():
        if str(key) in {"1", "2", "3", "4", "5"} and isinstance(ov, dict):
            o = {}
            if ov.get("count") is not None:
                o["count"] = _num(ov.get("count"), 1, 0, MAX_REPEAT, int)
            if ov.get("gap_ms") is not None:
                o["gap_ms"] = _num(ov.get("gap_ms"), 0, 0, 86_400_000, int)
            if o:
                overrides[str(key)] = o
    return {
        "id": str(m.get("id") or new_id()),
        "name": str(m.get("name") or "Макрос"),
        "hotkey": str(m.get("hotkey") or ""),
        "hotkey_target": "all" if m.get("hotkey_target") == "all" else "current",
        "repeat": {
            "count": _num(rep.get("count"), 1, 0, MAX_REPEAT, int),  # 0 — до остановки
            "gap_ms": _num(rep.get("gap_ms"), 0, 0, 86_400_000, int),
        },
        "user_input": m.get("user_input") if m.get("user_input") in USER_INPUT_MODES else "default",
        "windows": sorted(set(windows)),
        "overrides": overrides,
        "steps": steps,
    }


def repeat_for(macro: dict, slot: int) -> tuple[int, int]:
    """(повторов, пауза между повторами) с учётом настройки для конкретного окна."""
    count, gap = macro["repeat"]["count"], macro["repeat"]["gap_ms"]
    ov = macro.get("overrides", {}).get(str(slot), {})
    return ov.get("count", count), ov.get("gap_ms", gap)


def check_blocks(steps: list[dict]) -> list[str]:
    """Ошибки в структуре повторов: незакрытые блоки и лишние «конец блока»."""
    errors, stack = [], []
    for i, s in enumerate(steps, start=1):
        if s["t"] == "loop":
            stack.append(i)
        elif s["t"] == "endloop":
            if stack:
                stack.pop()
            else:
                errors.append(f"шаг {i}: «конец блока» без начала")
    errors += [f"шаг {i}: блок повтора не закрыт" for i in stack]
    return errors


# ------------------------------------------------------------------ описание шагов

_CODE_NAMES = {
    "Space": "Пробел", "Enter": "Enter", "Escape": "Esc", "Tab": "Tab", "Backspace": "Backspace",
    "ShiftLeft": "Shift", "ShiftRight": "Shift (правый)", "ControlLeft": "Ctrl", "ControlRight": "Ctrl (правый)",
    "AltLeft": "Alt", "AltRight": "Alt (правый)", "ArrowUp": "↑", "ArrowDown": "↓", "ArrowLeft": "←",
    "ArrowRight": "→", "CapsLock": "CapsLock", "Backquote": "`", "Minus": "-", "Equal": "=", "BracketLeft": "[",
    "BracketRight": "]", "Semicolon": ";", "Quote": "'", "Comma": ",", "Period": ".", "Slash": "/",
    "Backslash": "\\", "NumpadMultiply": "Num*", "NumpadAdd": "Num+", "NumpadSubtract": "Num-",
    "NumpadDecimal": "Num.", "NumpadDivide": "Num/", "NumpadEnter": "Num Enter",
}


def key_title(step: dict) -> str:
    code = step.get("code", "")
    if code in _CODE_NAMES:
        return _CODE_NAMES[code]
    if code.startswith("Key") and len(code) == 4:
        return code[3]
    if code.startswith("Digit") and len(code) == 6:
        return code[5]
    if code.startswith("Numpad"):
        return "Num" + code[6:]
    key = step.get("key", "")
    if len(key) == 1:
        return key.upper()
    return code or key or "?"


def delay_text(step: dict) -> str:
    """«Пауза перед» для таблицы шагов: «150 мс» или «100–300 мс»."""
    if "delay_max" in step:
        return f"{step['delay']}–{step['delay_max']} мс"
    return f"{step.get('delay', 0)} мс"


def _where(step: dict) -> str:
    if step.get("x") is None:
        return "на месте"
    return f"в ({step['x'] * 100:.1f}%, {step['y'] * 100:.1f}%)"


def describe(step: dict) -> str:
    t = step["t"]
    if t == "click":
        times = " ×2" if step.get("count") == 2 else ""
        return f"Клик {BUTTONS[step['button']]}{times} {_where(step)}"
    if t == "down":
        return f"Зажать {BUTTONS[step['button']]} {_where(step)}"
    if t == "up":
        return f"Отпустить {BUTTONS[step['button']]} {_where(step)}"
    if t == "move":
        return f"Вести мышь к ({step['x'] * 100:.1f}%, {step['y'] * 100:.1f}%)"
    if t == "look":
        return f"Поворот камеры: x {step['dx']:+.0f}, y {step['dy']:+.0f} за {step['ms']} мс"
    if t == "wheel":
        direction = "вниз" if step["dy"] > 0 else "вверх"
        return f"Колесо {direction} {abs(step['dy']):.0f} {_where(step)}"
    if t == "key":
        name = key_title(step)
        if step["action"] == "press":
            return f"Клавиша {name}: нажать ({step['ms']} мс)"
        return f"Клавиша {name}: {'зажать' if step['action'] == 'down' else 'отпустить'}"
    if t == "text":
        return f"Ввести текст «{step['text']}»"
    if t == "wait":
        return f"Пауза {step['ms']} мс"
    if t == "loop":
        return f"Повторить блок ×{step['times']} ⟶"
    if t == "random":
        every = str(step["min_ms"]) if step["min_ms"] == step["max_ms"] else f"{step['min_ms']}–{step['max_ms']}"
        text = f"Случайный ввод {step['ms']} мс, каждые {every} мс: " + ", ".join(RANDOM_KINDS[k] for k in step["kinds"])
        if "key" in step["kinds"]:
            text += " [" + " ".join(key_title(k) for k in step["keys"]) + "]"
        return text
    return "⟵ конец блока"


# ------------------------------------------------------------------ воспроизведение

class Target(Protocol):
    """Окно, в котором выполняется макрос (ChromeWindow или подделка в тестах)."""

    def page_size(self) -> tuple[float, float]: ...
    def mouse(self, kind: str, x: float, y: float, button: str = "left", clicks: int = 1,
              buttons: int = 0, modifiers: int = 0) -> None: ...
    def wheel(self, x: float, y: float, dy: float, modifiers: int = 0) -> None: ...
    def key(self, kind: str, params: dict) -> None: ...
    def insert_text(self, text: str) -> None: ...


class Stopped(Exception):
    pass


def key_params(step: dict) -> dict:
    p = {"key": step.get("key") or "", "code": step.get("code") or "", "windowsVirtualKeyCode": step.get("vk", 0)}
    if len(p["key"]) == 1:
        p["text"] = p["key"]
    return p


class MacroRunner:
    """Выполняет макрос в одном окне в отдельном потоке.

    activity() — True, когда пользователь сейчас действует в этом окне; policy решает, что делать:
    "pause" — ждать тишины, "stop" — остановиться, "keep" — не обращать внимания.
    on_status(dict) вызывается при смене состояния и по ходу шагов (не чаще раза в 100 мс).
    """

    def __init__(self, target: Target, macro: dict, *, count: int = 1, gap_ms: int = 0, policy: str = "pause",
                 activity: Callable[[], bool] = lambda: False, on_status: Callable[[dict], None] = lambda s: None,
                 clock: Callable[[], float] = time.perf_counter, sleep: Callable[[float], None] = time.sleep,
                 rng: random.Random | None = None, start: int = 0):
        self.target = target
        self.macro = macro
        self.steps = macro["steps"]
        self.first_step = max(0, min(int(start), len(self.steps)))  # первый проход — с этого шага
        self.count = count
        self.gap_ms = gap_ms
        self.policy = policy
        self.activity = activity
        self.on_status = on_status
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._thread: threading.Thread | None = None
        self.state = "idle"     # idle running paused paused_user done stopped error
        self.reason = ""
        self.pc = self.first_step
        self.iteration = 0
        self.page = (1.0, 1.0)
        self._page_at = -1e9
        self.cursor: tuple[float, float] | None = None
        self.buttons = 0
        self.held_keys: dict[str, dict] = {}
        self.held_buttons: set[str] = set()
        self._last_report = 0.0

    # ------------------------------------------------------------ управление
    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, daemon=True, name=f"macro-{self.macro['name']}")
        self._thread.start()

    def stop(self, reason: str = "") -> None:
        if reason:
            self.reason = reason
        self._stop.set()

    def toggle_pause(self) -> None:
        if self._pause.is_set():
            self._pause.clear()
        else:
            self._pause.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    @property
    def active(self) -> bool:
        return self.state in ("running", "paused", "paused_user")

    def status(self) -> dict:
        return {
            "state": self.state, "macro": self.macro["name"], "macro_id": self.macro["id"],
            "step": min(self.pc + 1, len(self.steps)), "total": len(self.steps),
            "index": self.pc if self.pc < len(self.steps) else -1,  # шаг, который идёт сейчас; -1 — между повторами
            "iteration": self.iteration, "count": self.count, "reason": self.reason,
        }

    def _report(self, force: bool = False) -> None:
        now = self.clock()
        if force or now - self._last_report >= 0.1:
            self._last_report = now
            try:
                self.on_status(self.status())
            except Exception:  # noqa: BLE001
                log.exception("on_status")

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self._report(force=True)

    # ------------------------------------------------------------ основной цикл
    def run(self) -> None:
        self._set_state("running")
        try:
            while self.count == 0 or self.iteration < self.count:
                self.iteration += 1
                started = self.clock()
                self._refresh_page(force=True)
                if self.cursor is None:
                    self.cursor = (self.page[0] / 2, self.page[1] / 2)
                self._run_steps(self.first_step if self.iteration == 1 else 0)
                if self.count != 0 and self.iteration >= self.count:
                    break
                spent = self.clock() - started
                self._sleep_ms(max(self.gap_ms, (MIN_ITERATION_S - spent) * 1000))
            self._finish("done")
        except Stopped:
            self._finish("stopped")
        except Exception as e:  # noqa: BLE001
            log.exception("макрос %s", self.macro["name"])
            self.reason = str(e)
            self._finish("error")

    def _finish(self, state: str) -> None:
        self._release_all()
        self._set_state(state)

    def _run_steps(self, start: int = 0) -> None:
        steps, stack = self.steps, []
        self.pc = start
        while self.pc < len(steps):
            step = steps[self.pc]
            self._sleep_ms(self._delay_of(step))
            self._report()
            t = step["t"]
            if t == "loop":
                stack.append([self.pc, step["times"]])
            elif t == "endloop":
                if stack:
                    stack[-1][1] -= 1
                    if stack[-1][1] > 0:
                        self.pc = stack[-1][0] + 1
                        continue
                    stack.pop()
            else:
                self._checkpoint()
                self._do(step)
            self.pc += 1

    def _delay_of(self, step: dict) -> int:
        """Пауза перед шагом: заданная или случайная из интервала delay..delay_max."""
        lo = step.get("delay", 0)
        hi = step.get("delay_max", lo)
        return self.rng.randint(lo, hi) if hi > lo else lo

    # ------------------------------------------------------------ пауза и стоп
    def _checkpoint(self) -> float:
        """Проверяет стоп и паузы. Возвращает, сколько секунд простояли на паузе."""
        if self._stop.is_set():
            raise Stopped
        paused = 0.0
        if self.policy != "keep" and self.activity():
            if self.policy == "stop":
                self.reason = "ты начал действовать в окне"
                raise Stopped
            t0 = self.clock()
            self._release_all()
            self._set_state("paused_user")
            while self.activity():
                if self._stop.is_set():
                    raise Stopped
                self.sleep(0.05)
            paused += self.clock() - t0
            self._set_state("running")
        if self._pause.is_set():
            t0 = self.clock()
            self._release_all()
            self._set_state("paused")
            while self._pause.is_set():
                if self._stop.is_set():
                    raise Stopped
                self.sleep(0.05)
            paused += self.clock() - t0
            self._set_state("running")
        return paused

    def _sleep_ms(self, ms: float) -> float:
        """Ждёт ms миллисекунд, не считая пауз. Возвращает, сколько секунд простояли на паузе."""
        end = self.clock() + ms / 1000
        paused_total = 0.0
        while True:
            paused = self._checkpoint()
            paused_total += paused
            end += paused
            rest = end - self.clock()
            if rest <= 0:
                return paused_total
            self._report()  # во время долгой паузы редактор всё равно видит, какой шаг идёт
            self.sleep(min(rest, 0.02))

    # ------------------------------------------------------------ действия
    def _modifiers(self) -> int:
        bits = 0
        for p in self.held_keys.values():
            bits |= _MODIFIER_BITS.get(p.get("key", ""), 0)
        return bits

    def _refresh_page(self, force: bool = False) -> None:
        """Размер страницы перечитывается не чаще раза в 0,5 с: окно могли развернуть или уменьшить."""
        now = self.clock()
        if force or now - self._page_at >= 0.5:
            self.page = self.target.page_size()
            self._page_at = now

    def _point(self, step: dict) -> tuple[float, float]:
        if step.get("x") is None:
            return self.cursor
        self._refresh_page()
        return step["x"] * self.page[0], step["y"] * self.page[1]

    def _move(self, x: float, y: float) -> None:
        self.target.mouse("mouseMoved", x, y, button="none", buttons=self.buttons, modifiers=self._modifiers())
        self.cursor = (x, y)

    def _press(self, x: float, y: float, button: str, clicks: int = 1) -> None:
        self.buttons |= _BUTTON_BIT[button]
        self.held_buttons.add(button)
        self.target.mouse("mousePressed", x, y, button=button, clicks=clicks, buttons=self.buttons,
                          modifiers=self._modifiers())

    def _release(self, x: float, y: float, button: str, clicks: int = 1) -> None:
        self.buttons &= ~_BUTTON_BIT[button]
        self.held_buttons.discard(button)
        self.target.mouse("mouseReleased", x, y, button=button, clicks=clicks, buttons=self.buttons,
                          modifiers=self._modifiers())

    def _do(self, step: dict) -> None:
        t = step["t"]
        if t in ("click", "down", "up", "wheel"):
            x, y = self._point(step)
            if step.get("x") is not None and (x, y) != self.cursor:
                self._move(x, y)
            if t == "click":
                for n in range(1, step["count"] + 1):
                    self._press(x, y, step["button"], n)
                    self._release(x, y, step["button"], n)
            elif t == "down":
                self._press(x, y, step["button"])
            elif t == "up":
                self._release(x, y, step["button"])
            else:
                self.target.wheel(x, y, step["dy"], modifiers=self._modifiers())
        elif t == "move":
            self._move(*self._point(step))
        elif t == "look":
            self._look(step["dx"], step["dy"], step["ms"])
        elif t == "key":
            self._key(step)
        elif t == "text":
            if step["text"]:
                self.target.insert_text(step["text"])
        elif t == "wait":
            self._sleep_ms(step["ms"])
        elif t == "random":
            self._random(step)

    def _look(self, dx: float, dy: float, ms: int) -> float:
        """Плавное относительное движение мыши — в режиме захвата курсора это поворот камеры."""
        parts = max(1, min(int(ms // 10), int(max(abs(dx), abs(dy)))))
        x0, y0 = self.cursor
        paused = 0.0
        for i in range(1, parts + 1):
            if ms:
                paused += self._sleep_ms(ms / parts)
            if dx or dy:
                self._move(x0 + dx * i / parts, y0 + dy * i / parts)
        return paused

    def _key(self, step: dict) -> float:
        params = key_params(step)
        paused = 0.0
        name = step.get("code") or step.get("key")
        if step["action"] in ("press", "down"):
            down = dict(params, modifiers=self._modifiers())
            self.held_keys[name] = params
            self.target.key("keyDown", down)
        if step["action"] == "press":
            paused = self._sleep_ms(step["ms"])
        if step["action"] in ("press", "up"):
            self.held_keys.pop(name, None)
            up = {k: v for k, v in params.items() if k != "text"}
            up["modifiers"] = self._modifiers()
            self.target.key("keyUp", up)
        return paused

    # ------------------------------------------------------------ случайный ввод
    def _random_point(self, step: dict) -> tuple[float, float]:
        if step["area"] is None:
            return self.cursor
        self._refresh_page()
        x1, y1, x2, y2 = step["area"]
        return self.rng.uniform(x1, x2) * self.page[0], self.rng.uniform(y1, y2) * self.page[1]

    def _random(self, step: dict) -> None:
        """Случайные действия из выбранных видов, пока не истечёт время шага."""
        mine_keys: set[str] = set()
        mine_buttons: set[str] = set()
        end = self.clock() + step["ms"] / 1000
        try:
            while self.clock() < end:
                started = self.clock()
                kind = self.rng.choice(step["kinds"])
                end += self._random_action(step, kind, mine_keys, mine_buttons)
                interval = self.rng.uniform(step["min_ms"], step["max_ms"])
                spent_ms = (self.clock() - started) * 1000
                end += self._sleep_ms(max(0.0, interval - spent_ms))
        finally:
            # отпускаем только то, что нажал сам случайный ввод
            for name in mine_keys:
                params = self.held_keys.pop(name, None)
                if params:
                    try:
                        self.target.key("keyUp", {k: v for k, v in params.items() if k != "text"})
                    except Exception:  # noqa: BLE001
                        pass
            for button in mine_buttons & self.held_buttons:
                try:
                    self._release(*self.cursor, button)
                except Exception:  # noqa: BLE001
                    self.held_buttons.discard(button)

    def _random_action(self, step: dict, kind: str, mine_keys: set, mine_buttons: set) -> float:
        """Одно случайное действие. Возвращает, сколько секунд простояли на паузе внутри него."""
        rng = self.rng
        if kind in ("click", "down", "up", "wheel") or (kind == "move" and step["area"] is not None):
            x, y = self._random_point(step)
            if step["area"] is not None and (x, y) != self.cursor:
                self._move(x, y)
        if kind == "click":
            button = rng.choice(step["buttons"])
            self._press(x, y, button)
            self._release(x, y, button)
        elif kind == "down":
            free = [b for b in step["buttons"] if b not in self.held_buttons] or step["buttons"]
            button = rng.choice(free)
            if button not in self.held_buttons:
                self._press(x, y, button)
                mine_buttons.add(button)
        elif kind == "up":
            held = sorted(mine_buttons & self.held_buttons)
            if held:
                button = rng.choice(held)
                self._release(x, y, button)
                mine_buttons.discard(button)
        elif kind == "wheel":
            amount = max(1.0, round(rng.uniform(0.25, 1.0) * step["wheel"]))
            self.target.wheel(x, y, rng.choice((-1, 1)) * amount, modifiers=self._modifiers())
        elif kind == "look" or (kind == "move" and step["area"] is None):
            # без области «вести мышь» — небольшой сдвиг от текущего места
            dx = rng.uniform(-step["look_dx"], step["look_dx"])
            dy = rng.uniform(-step["look_dy"], step["look_dy"])
            return self._look(dx, dy, step["min_ms"] if kind == "look" else 0)
        elif kind == "key" and step["keys"]:
            spec = rng.choice(step["keys"])
            name = spec["code"] or spec["key"]
            hold = rng.randint(step["hold_min"], step["hold_max"])
            mine_keys.add(name)
            paused = self._key(dict(spec, action="press", ms=hold))
            mine_keys.discard(name)
            return paused
        return 0.0

    def _release_all(self) -> None:
        """Отпускает всё, что макрос держит нажатым, чтобы не было «залипших» клавиш."""
        for name, params in list(self.held_keys.items()):
            try:
                self.target.key("keyUp", {k: v for k, v in params.items() if k != "text"})
            except Exception:  # noqa: BLE001
                pass
            self.held_keys.pop(name, None)
        for button in list(self.held_buttons):
            try:
                x, y = self.cursor or (0, 0)
                self._release(x, y, button)
            except Exception:  # noqa: BLE001
                self.held_buttons.discard(button)
        self.buttons = 0


# ------------------------------------------------------------------ обработка записи

def scale_pauses(steps: list[dict], factor: float) -> tuple[list[dict], int]:
    """Паузы перед шагами и шаги «Пауза» × factor. Возвращает новые шаги и сколько из них изменилось."""
    out, changed = [], 0
    for s in steps:
        n = dict(s, delay=int(round(s.get("delay", 0) * factor)))
        if "delay_max" in s:
            n["delay_max"] = int(round(s["delay_max"] * factor))
        if s["t"] == "wait":
            n["ms"] = int(round(s["ms"] * factor))
        n = normalize_step(n)
        changed += n != s
        out.append(n)
    return out, changed


def drop_short_pauses(steps: list[dict], min_ms: int) -> tuple[list[dict], int]:
    """Обнуляет паузы перед шагами короче min_ms и убирает такие же короткие шаги «Пауза».

    Случайная пауза считается короткой, только если короче min_ms весь её интервал.
    """
    out, changed, carry_lo, carry_hi = [], 0, 0, 0
    for s in steps:
        lo = s.get("delay", 0)
        hi = s.get("delay_max", lo)
        if hi < min_ms:
            lo = hi = 0
        if s["t"] == "wait" and s["ms"] < min_ms:
            carry_lo, carry_hi = carry_lo + lo, carry_hi + hi  # длинная пауза перед убранным шагом остаётся
            changed += 1
            continue
        n = {k: v for k, v in s.items() if k != "delay_max"}
        n["delay"] = lo + carry_lo
        if hi + carry_hi > n["delay"]:
            n["delay_max"] = hi + carry_hi
        carry_lo = carry_hi = 0
        changed += n != s
        out.append(n)
    return out, changed


def jitter_pauses(steps: list[dict], percent: int) -> tuple[list[dict], int]:
    """Паузы перед шагами становятся случайными: ±percent % от нынешней (у интервала — от его середины).

    Шаги без паузы не трогаются, поэтому ритм записи сохраняется.
    """
    out, changed = [], 0
    for s in steps:
        lo = s.get("delay", 0)
        mid = (lo + s.get("delay_max", lo)) / 2
        n = dict(s)
        if mid > 0:
            n.pop("delay_max", None)
            n["delay"] = int(round(mid * (1 - percent / 100)))
            n["delay_max"] = int(round(mid * (1 + percent / 100)))
            n = normalize_step(n)
        changed += n != s
        out.append(n)
    return out, changed


def merge_looks(steps: list[dict], max_gap_ms: int = 100) -> tuple[list[dict], int]:
    """Склеивает идущие подряд повороты камеры в одну сторону, между которыми не больше max_gap_ms.

    Запись режет плавный поворот на куски по 50 мс; после склейки сдвиг и общее время те же, шагов меньше.
    """
    out, merged = [], 0
    for s in steps:
        prev = out[-1] if out else None
        if (prev and prev["t"] == "look" and s["t"] == "look" and s.get("delay", 0) <= max_gap_ms
                and "delay_max" not in s  # случайную паузу между поворотами склейка потеряла бы
                and prev["dx"] * s["dx"] >= 0 and prev["dy"] * s["dy"] >= 0
                and prev["ms"] + s.get("delay", 0) + s["ms"] <= 600_000):
            prev["dx"] = round(prev["dx"] + s["dx"], 1)
            prev["dy"] = round(prev["dy"] + s["dy"], 1)
            prev["ms"] += s.get("delay", 0) + s["ms"]
            merged += 1
            continue
        out.append(dict(s))
    return out, merged


def clone_macro(macro: dict, name: str | None = None) -> dict:
    m = copy.deepcopy(macro)
    m["id"] = new_id()
    m["name"] = name or (macro["name"] + " (копия)")
    m["hotkey"] = ""
    return m
