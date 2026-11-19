"""Запись макроса: в страницу внедряется слушатель событий, панель забирает их по протоколу отладки.

Запись идёт внутри страницы, поэтому координаты сразу в долях страницы, а в режиме захвата курсора
браузер сам отдаёт относительное движение мыши (поворот камеры).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .macros import normalize_step

log = logging.getLogger(__name__)

RECORDER_JS = r"""
(() => {
  if (!window.__p6rec) {
    const q = [];
    window.__p6rec = q;
    window.__p6recOn = false;
    const W = () => window.innerWidth || 1, H = () => window.innerHeight || 1;
    const push = (o) => {
      if (!window.__p6recOn) return;
      o.t = performance.now();
      o.locked = !!document.pointerLockElement;
      q.push(o);
      if (q.length > 50000) q.splice(0, 10000);
    };
    const opt = {capture: true, passive: true};
    const pos = (e) => ({x: e.clientX / W(), y: e.clientY / H()});
    const isMouse = (e) => !e.pointerType || e.pointerType === 'mouse';
    addEventListener('pointerdown', e => { if (isMouse(e)) push(Object.assign({e: 'down', b: e.button}, pos(e))); }, opt);
    addEventListener('pointerup', e => { if (isMouse(e)) push(Object.assign({e: 'up', b: e.button}, pos(e))); }, opt);
    addEventListener('pointermove', e => {
      if (!isMouse(e)) return;
      if (e.button >= 0) {
        // ещё одна кнопка нажата или отпущена, пока первая зажата
        const bit = {0: 1, 1: 4, 2: 2}[e.button] || 0;
        push(Object.assign({e: (e.buttons & bit) ? 'down' : 'up', b: e.button}, pos(e)));
      }
      push(Object.assign({e: 'move', bs: e.buttons, mx: e.movementX || 0, my: e.movementY || 0}, pos(e)));
    }, opt);
    addEventListener('wheel', e => push(Object.assign({e: 'wheel', dy: e.deltaY}, pos(e))), opt);
    addEventListener('keydown', e => { if (!e.repeat) push({e: 'kd', code: e.code, key: e.key, vk: e.keyCode}); }, opt);
    addEventListener('keyup', e => push({e: 'ku', code: e.code, key: e.key, vk: e.keyCode}), opt);
  }
  window.__p6rec.length = 0;
  window.__p6recOn = true;
  return true;
})()
"""

POLL_JS = "(() => { const q = window.__p6rec; if (!q || !window.__p6recOn) return null; return q.splice(0, q.length); })()"
STOP_JS = "(() => { window.__p6recOn = false; const q = window.__p6rec; return q ? q.splice(0, q.length) : []; })()"

_JS_BUTTON = {0: "left", 1: "middle", 2: "right"}
_JS_BUTTON_BIT = {0: 1, 1: 4, 2: 2}


# ------------------------------------------------------------------ преобразование событий в шаги

def events_to_steps(events: list[dict], *, move_ms: float = 30, look_ms: float = 50,
                    click_ms: float = 400, click_dist: float = 0.01, dbl_ms: float = 500) -> list[dict]:
    """Сырые события страницы -> шаги макроса с паузами между ними."""
    acts: list[dict] = []          # шаги с временем начала _at и длительностью _dur
    look: dict | None = None
    last_move_at = -1e9

    def close_look():
        nonlocal look
        if look and (look["dx"] or look["dy"]):
            ms = max(int(round(look["_end"] - look["_at"])), 8)
            acts.append({"t": "look", "dx": round(look["dx"], 1), "dy": round(look["dy"], 1), "ms": ms,
                         "_at": look["_at"], "_dur": ms})
        look = None

    for ev in sorted(events, key=lambda e: e.get("t", 0)):
        kind, at = ev.get("e"), float(ev.get("t", 0))
        locked = bool(ev.get("locked"))
        if kind == "move":
            if locked:
                if look and at - look["_at"] > look_ms:
                    close_look()
                if look is None:
                    look = {"dx": 0.0, "dy": 0.0, "_at": at, "_end": at}
                look["dx"] += ev.get("mx", 0)
                look["dy"] += ev.get("my", 0)
                look["_end"] = at
            elif ev.get("bs") and at - last_move_at >= move_ms:
                close_look()
                acts.append({"t": "move", "x": ev["x"], "y": ev["y"], "_at": at, "_dur": 0})
                last_move_at = at
            continue
        close_look()
        x, y = (None, None) if locked else (ev.get("x"), ev.get("y"))
        if kind in ("down", "up") and ev.get("b") in _JS_BUTTON:
            acts.append({"t": kind, "x": x, "y": y, "button": _JS_BUTTON[ev["b"]], "_at": at, "_dur": 0})
        elif kind == "wheel" and ev.get("dy"):
            acts.append({"t": "wheel", "x": x, "y": y, "dy": ev["dy"], "_at": at, "_dur": 0})
        elif kind in ("kd", "ku") and (ev.get("code") or ev.get("key")):
            acts.append({"t": "key", "action": "down" if kind == "kd" else "up", "code": ev.get("code", ""),
                         "key": ev.get("key", ""), "vk": ev.get("vk", 0), "_at": at, "_dur": 0})
    close_look()

    acts = _collapse_clicks(acts, click_ms, click_dist, dbl_ms)
    acts = _collapse_keys(acts)
    acts += _release_stuck(acts)

    steps, prev_end = [], None
    for a in acts:
        delay = 0 if prev_end is None else max(0, int(round(a["_at"] - prev_end)))
        prev_end = a["_at"] + a["_dur"]
        step = {k: v for k, v in a.items() if not k.startswith("_")}
        step["delay"] = delay
        steps.append(normalize_step(step))
    return steps


def _near(a: dict, b: dict, dist: float) -> bool:
    if a.get("x") is None or b.get("x") is None:
        return a.get("x") is None and b.get("x") is None
    return abs(a["x"] - b["x"]) <= dist and abs(a["y"] - b["y"]) <= dist


def _collapse_clicks(acts, click_ms, click_dist, dbl_ms):
    out = []
    i = 0
    while i < len(acts):
        a = acts[i]
        # мелкое дрожание мыши между нажатием и отпусканием — всё ещё клик
        j = i + 1
        while a["t"] == "down" and j < len(acts) and acts[j]["t"] == "move" and _near(a, acts[j], click_dist):
            j += 1
        nxt = acts[j] if j < len(acts) else None
        if (a["t"] == "down" and nxt and nxt["t"] == "up" and nxt["button"] == a["button"]
                and nxt["_at"] - a["_at"] <= click_ms and _near(a, nxt, click_dist)):
            click = {"t": "click", "x": a["x"], "y": a["y"], "button": a["button"], "count": 1,
                     "_at": a["_at"], "_dur": 0}
            prev = out[-1] if out else None
            if (prev and prev["t"] == "click" and prev["count"] == 1 and prev["button"] == click["button"]
                    and click["_at"] - prev["_at"] <= dbl_ms and _near(prev, click, click_dist)):
                prev["count"] = 2
            else:
                out.append(click)
            i = j + 1
            continue
        out.append(a)
        i += 1
    return out


def _collapse_keys(acts):
    out = []
    i = 0
    while i < len(acts):
        a = acts[i]
        nxt = acts[i + 1] if i + 1 < len(acts) else None
        if (a["t"] == "key" and a["action"] == "down" and nxt and nxt["t"] == "key" and nxt["action"] == "up"
                and nxt["code"] == a["code"]):
            hold = max(int(round(nxt["_at"] - a["_at"])), 1)
            out.append(dict(a, action="press", ms=hold, _dur=hold))
            i += 2
            continue
        out.append(a)
        i += 1
    return out


def _release_stuck(acts):
    """Если запись остановили, пока что-то было зажато, добавляет отпускание в конец."""
    keys, buttons = {}, {}
    for a in acts:
        if a["t"] == "key" and a["action"] in ("down", "up"):
            if a["action"] == "down":
                keys[a["code"] or a["key"]] = a
            else:
                keys.pop(a["code"] or a["key"], None)
        elif a["t"] in ("down", "up"):
            if a["t"] == "down":
                buttons[a["button"]] = a
            else:
                buttons.pop(a["button"], None)
    end = acts[-1]["_at"] + acts[-1]["_dur"] if acts else 0
    tail = [dict(k, action="up", _at=end, _dur=0) for k in keys.values()]
    tail += [{"t": "up", "x": None, "y": None, "button": b, "_at": end, "_dur": 0} for b in buttons]
    return tail


# ------------------------------------------------------------------ запись из окна

class Recorder:
    """Записывает действия пользователя в окне Chrome. window — ChromeWindow (нужен метод call)."""

    def __init__(self, window, on_count: Callable[[int], None] = lambda n: None, poll_s: float = 0.1):
        self.window = window
        self.on_count = on_count
        self.poll_s = poll_s
        self.events: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error = ""

    def _eval(self, expr: str):
        res = self.window.call("Runtime.evaluate", expression=expr, returnByValue=True)
        return res.get("result", {}).get("value")

    def start(self) -> None:
        self._eval(RECORDER_JS)
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="recorder")
        self._thread.start()

    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_s):
            try:
                batch = self._eval(POLL_JS)
                if batch is None:
                    # страница перезагрузилась — слушатель пропал, ставим заново
                    self._eval(RECORDER_JS)
                    continue
                if batch:
                    self.events.extend(batch)
                    self.on_count(self.action_count())
            except Exception as e:  # noqa: BLE001
                self.error = str(e)
                log.info("запись: %s", e)
                time.sleep(0.5)

    def action_count(self) -> int:
        return sum(1 for e in self.events if e.get("e") in ("down", "kd", "wheel"))

    def stop(self) -> list[dict]:
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        try:
            tail = self._eval(STOP_JS)
            if tail:
                self.events.extend(tail)
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
        return events_to_steps(self.events)
