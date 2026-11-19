"""Сквозная проверка с настоящим Chrome без окна: макрос доходит до страницы, запись превращается в шаги.

Пропускается, если Chrome не найден. Путь можно задать переменной PANEL6_CHROME.
"""

import http.server
import os
import socket
import threading
import time

import pytest

from panel6.chrome import ChromeWindow, find_chrome
from panel6.macros import MacroRunner, normalize_macro
from panel6.recorder import Recorder

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;overflow:hidden;background:#123}
#lock{position:absolute;left:600px;top:200px;width:300px;height:200px;background:#2350d8}
</style></head><body><div id="lock"></div><input id="inp" style="position:absolute;left:10px;top:600px">
<script>
window.__log = [];
const rec = e => window.__log.push({t: e.type, x: e.clientX, y: e.clientY, mx: e.movementX || 0,
  my: e.movementY || 0, code: e.code || '', key: e.key || '', shift: !!e.shiftKey,
  locked: !!document.pointerLockElement, trusted: e.isTrusted});
['mousedown','mouseup','click','dblclick','mousemove','wheel','keydown','keyup'].forEach(t => addEventListener(t, rec, true));
document.getElementById('lock').addEventListener('click', () => document.getElementById('lock').requestPointerLock());
</script></body></html>"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def window(tmp_path_factory):
    chrome = find_chrome(os.environ.get("PANEL6_CHROME", ""))
    if not chrome:
        pytest.skip("Chrome не найден")
    old = {k: os.environ.get(k) for k in ("PANEL6_HOME", "PANEL6_HEADLESS")}
    os.environ["PANEL6_HOME"] = str(tmp_path_factory.mktemp("p6home"))
    os.environ["PANEL6_HEADLESS"] = "1"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    win = ChromeWindow(1, port=free_port())
    win.launch(chrome, f"http://127.0.0.1:{srv.server_address[1]}/", None)
    deadline = time.time() + 40
    while time.time() < deadline and win.refresh() != "ready":
        time.sleep(0.3)
    assert win.connected, "Chrome не запустился"
    for _ in range(50):
        if win.evaluate("document.readyState === 'complete' && !!window.__log"):
            break
        time.sleep(0.1)
    # сразу после запуска размер окна без экрана ещё меняется — ждём, пока успокоится
    last, stable_since = None, time.time()
    while time.time() - stable_since < 0.6 and time.time() < deadline + 10:
        size = win.page_size()
        if size != last:
            last, stable_since = size, time.time()
        time.sleep(0.1)
    yield win
    win.close_browser()
    srv.shutdown()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def page_log(win, clear=True):
    log = win.evaluate("window.__log.slice()")
    if clear:
        win.evaluate("window.__log.length = 0")
    return log


def lock_center(win):
    w, h = win.page_size()
    return (750 / w, 300 / h)


def run(win, steps, **kw):
    macro = normalize_macro({"name": "тест", "steps": steps})
    runner = MacroRunner(win, macro, policy="keep", **kw)
    runner.run()
    assert runner.state == "done", runner.reason
    return runner


def test_macro_reaches_page(window):
    page_log(window)
    run(window, [
        {"t": "click", "x": 0.1, "y": 0.1},
        {"t": "click", "x": 0.1, "y": 0.1, "count": 2, "delay": 50},
        {"t": "key", "code": "ShiftLeft", "key": "Shift", "vk": 16, "action": "down"},
        {"t": "key", "code": "KeyW", "key": "W", "vk": 87, "action": "press", "ms": 100},
        {"t": "key", "code": "ShiftLeft", "key": "Shift", "vk": 16, "action": "up"},
        {"t": "wheel", "x": 0.2, "y": 0.2, "dy": 120},
    ])
    time.sleep(0.3)  # колесо браузер доставляет странице асинхронно
    log = page_log(window)
    types = [e["t"] for e in log]
    assert types.count("click") == 3 and "dblclick" in types and "wheel" in types
    assert all(e["trusted"] for e in log)
    w_down = [e for e in log if e["t"] == "keydown" and e["code"] == "KeyW"]
    assert w_down and w_down[0]["shift"], "Shift должен быть зажат при нажатии W"
    w, h = window.page_size()
    first = next(e for e in log if e["t"] == "click")
    assert abs(first["x"] - 0.1 * w) <= 1 and abs(first["y"] - 0.1 * h) <= 1


def test_camera_turn_under_pointer_lock(window):
    page_log(window)
    x, y = lock_center(window)
    run(window, [{"t": "click", "x": x, "y": y}, {"t": "look", "dx": 150, "dy": -40, "ms": 100, "delay": 300}])
    assert window.evaluate("!!document.pointerLockElement"), "захват курсора не включился"
    log = page_log(window)
    locked = [e for e in log if e["t"] == "mousemove" and e["locked"]]
    assert sum(e["mx"] for e in locked) == 150 and sum(e["my"] for e in locked) == -40
    window.evaluate("document.exitPointerLock()")


def test_recorder_turns_user_actions_into_steps(window):
    time.sleep(0.3)
    rec = Recorder(window)
    rec.start()
    w, h = window.page_size()
    # действия «пользователя»: клик, нажатие клавиши, захват курсора и поворот
    window.mouse("mouseMoved", 200, 300, button="none")
    window.mouse("mousePressed", 200, 300, buttons=1)
    window.mouse("mouseReleased", 200, 300)
    time.sleep(0.2)
    window.key("keyDown", {"key": "q", "code": "KeyQ", "windowsVirtualKeyCode": 81, "text": "q"})
    time.sleep(0.25)
    window.key("keyUp", {"key": "q", "code": "KeyQ", "windowsVirtualKeyCode": 81})
    window.mouse("mouseMoved", 750, 300, button="none")
    window.mouse("mousePressed", 750, 300, buttons=1)
    window.mouse("mouseReleased", 750, 300)
    time.sleep(0.3)
    for i in range(1, 11):
        window.mouse("mouseMoved", 750 + 10 * i, 300, button="none")
        time.sleep(0.01)
    time.sleep(0.2)
    steps = rec.stop()
    window.evaluate("document.exitPointerLock()")
    kinds = [s["t"] for s in steps]
    assert kinds[0] == "click"
    assert abs(steps[0]["x"] - 200 / w) < 0.005 and abs(steps[0]["y"] - 300 / h) < 0.005
    key = next(s for s in steps if s["t"] == "key")
    assert key["action"] == "press" and key["code"] == "KeyQ" and 150 <= key["ms"] <= 600
    looks = [s for s in steps if s["t"] == "look"]
    assert looks and sum(s["dx"] for s in looks) == 100
    assert rec.action_count() >= 3


def test_random_input_reaches_page(window):
    import random as _random

    page_log(window)
    macro = normalize_macro({"name": "случайный", "steps": [{
        "t": "random", "ms": 800, "min_ms": 40, "max_ms": 60, "kinds": ["click", "key", "wheel"],
        "keys": [{"code": "KeyW", "key": "w", "vk": 87}, {"code": "KeyD", "key": "d", "vk": 68}],
        "hold_min": 10, "hold_max": 30, "area": [0.0, 0.0, 0.5, 0.5]}]})
    runner = MacroRunner(window, macro, policy="keep", rng=_random.Random(7))
    runner.run()
    assert runner.state == "done", runner.reason
    time.sleep(0.3)
    log = page_log(window)
    w, h = window.page_size()
    clicks = [e for e in log if e["t"] == "click"]
    keys_down = [e["code"] for e in log if e["t"] == "keydown"]
    keys_up = [e["code"] for e in log if e["t"] == "keyup"]
    assert clicks and all(e["x"] <= w / 2 + 1 and e["y"] <= h / 2 + 1 for e in clicks)
    assert set(keys_down) <= {"KeyW", "KeyD"} and sorted(keys_down) == sorted(keys_up)
    assert len(clicks) + len(keys_down) + sum(1 for e in log if e["t"] == "wheel") >= 8


def test_kill_ends_chrome_with_all_its_processes(tmp_path, monkeypatch):
    """Kill switch: копия Chrome завершается сразу вместе со всеми своими процессами."""
    import psutil

    chrome = find_chrome(os.environ.get("PANEL6_CHROME", ""))
    if not chrome:
        pytest.skip("Chrome не найден")
    monkeypatch.setenv("PANEL6_HOME", str(tmp_path))
    monkeypatch.setenv("PANEL6_HEADLESS", "1")
    w = ChromeWindow(1, port=free_port())
    w.launch(chrome, "about:blank", None)
    end = time.time() + 40
    while w.refresh() != "ready" and time.time() < end:
        time.sleep(0.2)
    assert w.connected
    browser = psutil.Process(w.pid)
    procs = [browser, *browser.children(recursive=True)]
    assert len(procs) > 1
    started = time.perf_counter()
    assert w.kill() is True
    took = time.perf_counter() - started
    _gone, alive = psutil.wait_procs(procs, timeout=5)
    assert not alive and took < 1.0
    assert w.kill() is False and not w.debug_alive()  # больше нечего закрывать
