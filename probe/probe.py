"""Пробник этапа 0: проверяет, можно ли управлять несколькими окнами Chrome в фоне.

Запускает N отдельных копий Chrome (у каждой своя папка профиля и свой порт
отладки) и через Chrome DevTools Protocol проверяет:
  * доходят ли клики, клавиши и движения мыши в окно, которое закрыто другим;
  * работает ли захват курсора (pointer lock) и движение мыши в нём;
  * обновляется ли картинка в скрытом и свёрнутом окне (снимки экрана);
  * сколько ресурсов съедает каждая копия Chrome.

Результаты пишутся в отчёт, который нужно прислать в чат.
"""

import base64
import datetime
import http.server
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import psutil
import websocket

VERSION = "0.1.0"
BASE_PORT = 9301
IS_WIN = platform.system() == "Windows"
HEADLESS = os.environ.get("PANEL6_HEADLESS") == "1"
APP_DIR = Path(os.environ.get("PANEL6_HOME") or (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Panel6Probe"))
SHOTS_DIR = APP_DIR / "shots"
BOOSTEROID_URL = "https://cloud.boosteroid.com/"

REPORT: list[str] = []

# Консоль Windows и перенаправленный вывод могут быть не в UTF-8 — кириллица не должна ронять программу.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def say(line: str = "") -> None:
    print(line, flush=True)
    REPORT.append(line)


# ---------------------------------------------------------------- тестовая страница

TEST_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Panel6 тест</title>
<style>
 body{margin:0;font:16px system-ui;background:#101820;color:#e8eef5;height:100vh;overflow:hidden}
 #info{position:absolute;left:20px;top:20px;font-size:22px}
 #counter{font:bold 64px monospace;color:#7fa3ff}
 #lock{position:absolute;left:600px;top:200px;width:300px;height:200px;background:#2350d8;
       display:grid;place-items:center;border-radius:12px;cursor:pointer;font-size:20px}
 #log{position:absolute;left:20px;bottom:20px;right:20px;height:200px;overflow:hidden;
      font:13px monospace;color:#97a5b4;white-space:pre}
</style></head><body>
<div id="info">Окно <b id="wid"></b><div id="counter">0</div><div id="state"></div></div>
<div id="lock">Клик = захват курсора</div>
<div id="log"></div>
<script>
 window.__log = []; window.__frames = 0;
 document.getElementById('wid').textContent = location.hash.slice(1);
 const logEl = document.getElementById('log');
 function rec(e){
   const r = {t:e.type, trusted:e.isTrusted, x:e.clientX|0, y:e.clientY|0, mx:e.movementX|0, my:e.movementY|0,
              key:e.key||'', locked:!!document.pointerLockElement, ts:Date.now()};
   __log.push(r);
   if (__log.length > 5000) __log.splice(0, 1000);
   if (e.type !== 'mousemove' || r.locked) logEl.textContent = (JSON.stringify(r) + '\\n' + logEl.textContent).slice(0, 4000);
 }
 ['mousedown','mouseup','click','keydown','keyup','mousemove','wheel'].forEach(t => addEventListener(t, rec, true));
 document.getElementById('lock').addEventListener('click', () => {
   try { const p = document.getElementById('lock').requestPointerLock(); if (p && p.catch) p.catch(e => __log.push({t:'pointerlockerror', msg:String(e)})); }
   catch (e) { __log.push({t:'pointerlockerror', msg:String(e)}); }
 });
 document.addEventListener('pointerlockchange', () => __log.push({t:'pointerlockchange', locked:!!document.pointerLockElement}));
 document.addEventListener('pointerlockerror', () => __log.push({t:'pointerlockerror'}));
 const st = document.getElementById('state');
 function loop(){ __frames++; document.getElementById('counter').textContent = __frames;
   st.textContent = 'hidden=' + document.hidden + ' focus=' + document.hasFocus(); requestAnimationFrame(loop); }
 requestAnimationFrame(loop);
 setInterval(() => { window.__timer = (window.__timer|0) + 1; }, 100);
</script></body></html>"""


def start_test_server() -> int:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = TEST_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


# ---------------------------------------------------------------- Chrome и CDP

def find_chrome() -> str:
    env = os.environ.get("PANEL6_CHROME")
    if env and Path(env).exists():
        return env
    candidates = []
    if IS_WIN:
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
            if base:
                candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for c in candidates:
        if c.exists():
            return str(c)
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    raise SystemExit("Не нашёл Chrome. Укажи путь в переменной PANEL6_CHROME.")


def http_json(port: int, path: str, timeout: float = 2.0):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class Cdp:
    """Минимальный синхронный клиент протокола отладки Chrome для одной вкладки."""

    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self.next_id = 0
        self.lock = threading.Lock()

    def call(self, method: str, **params):
        with self.lock:
            self.next_id += 1
            msg_id = self.next_id
            self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
            while True:
                msg = json.loads(self.ws.recv())
                if msg.get("id") == msg_id:
                    if "error" in msg:
                        raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                    return msg.get("result", {})

    def eval(self, expr: str):
        res = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return res.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


class Window:
    def __init__(self, index: int, chrome: str):
        self.index = index
        self.port = BASE_PORT + index
        self.profile = APP_DIR / "profiles" / f"p{index}"
        self.chrome = chrome
        self.proc = None
        self.cdp: Cdp | None = None
        self.window_id = None

    def launch(self, url: str) -> None:
        self.profile.mkdir(parents=True, exist_ok=True)
        args = [
            self.chrome,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
            "--disable-features=CalculateNativeWinOcclusion",
            "--autoplay-policy=no-user-gesture-required",
        ]
        if HEADLESS:
            args += ["--headless=new", "--window-size=1600,900", "--no-sandbox"]
        args.append(url)
        if not self.alive():
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.alive():
                break
            time.sleep(0.3)
        else:
            raise RuntimeError(f"Окно {self.index}: Chrome не ответил на порту {self.port}")
        self.attach()

    def alive(self) -> bool:
        try:
            http_json(self.port, "/json/version", 0.5)
            return True
        except Exception:
            return False

    def attach(self) -> None:
        deadline = time.time() + 15
        while time.time() < deadline:
            pages = [t for t in http_json(self.port, "/json/list") if t.get("type") == "page"]
            if pages:
                if self.cdp:
                    self.cdp.close()
                self.cdp = Cdp(pages[0]["webSocketDebuggerUrl"])
                self.cdp.call("Page.enable")
                try:
                    self.window_id = self.cdp.call("Browser.getWindowForTarget").get("windowId")
                except Exception:
                    self.window_id = None
                return
            time.sleep(0.3)
        raise RuntimeError(f"Окно {self.index}: нет вкладки")

    # --- управление окном
    def navigate(self, url: str) -> None:
        self.cdp.call("Page.navigate", url=url)

    def set_state(self, state: str) -> None:
        if self.window_id is None:
            return
        if state != "normal":
            # из fullscreen/minimized Chrome требует сначала вернуться в normal
            self.cdp.call("Browser.setWindowBounds", windowId=self.window_id, bounds={"windowState": "normal"})
            time.sleep(0.3)
        self.cdp.call("Browser.setWindowBounds", windowId=self.window_id, bounds={"windowState": state})

    def front(self) -> None:
        self.cdp.call("Page.bringToFront")

    def focus_emulation(self, on: bool = True) -> None:
        self.cdp.call("Emulation.setFocusEmulationEnabled", enabled=on)

    # --- ввод
    def click(self, x: float, y: float) -> None:
        for t in ("mousePressed", "mouseReleased"):
            self.cdp.call("Input.dispatchMouseEvent", type=t, x=x, y=y, button="left", buttons=1 if t == "mousePressed" else 0, clickCount=1)

    def move(self, x: float, y: float) -> None:
        self.cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, button="none")

    def key(self, key: str, code: str, vk: int, hold: float = 0.05) -> None:
        text = key if len(key) == 1 else ""
        self.cdp.call("Input.dispatchKeyEvent", type="keyDown", key=key, code=code, windowsVirtualKeyCode=vk, text=text)
        time.sleep(hold)
        self.cdp.call("Input.dispatchKeyEvent", type="keyUp", key=key, code=code, windowsVirtualKeyCode=vk)

    def screenshot(self, path: Path) -> tuple[bytes, float]:
        t0 = time.time()
        data = base64.b64decode(self.cdp.call("Page.captureScreenshot", format="png")["data"])
        dt = time.time() - t0
        path.write_bytes(data)
        return data, dt


# ---------------------------------------------------------------- проверки

def check(name: str, ok, details: str = "") -> None:
    mark = {True: "OK  ", False: "НЕТ ", None: "??  "}[ok]
    say(f"  [{mark}] {name}" + (f" — {details}" if details else ""))


def fps(w: Window, seconds: float = 1.5) -> float:
    a = w.cdp.eval("window.__frames") or 0
    time.sleep(seconds)
    b = w.cdp.eval("window.__frames") or 0
    return (b - a) / seconds


def autotest(windows: list[Window], test_url: str) -> None:
    say("")
    say(f"=== Автотест на тестовой странице ({datetime.datetime.now():%H:%M:%S}) ===")
    for w in windows:
        w.navigate(f"{test_url}#{w.index}")
    time.sleep(2.5)
    for w in windows:
        if not HEADLESS:
            w.set_state("maximized")
    time.sleep(1.0)

    for target in windows:
        others = [w for w in windows if w is not target]
        say(f"Окно {target.index} (сверху лежит окно {others[-1].index if others else '—'}):")
        if others:
            others[-1].front()
            time.sleep(1.2)
        target.focus_emulation(True)
        target.cdp.eval("window.__log.length = 0")

        info = target.cdp.eval("({hidden: document.hidden, focus: document.hasFocus(), vis: document.visibilityState})") or {}
        check("страница считает себя видимой", info.get("hidden") is False, f"visibilityState={info.get('vis')}, hasFocus={info.get('focus')}")
        f = fps(target)
        check("анимация идёт в закрытом окне", f > 20, f"{f:.0f} кадров/с")

        target.click(200, 300)
        target.key("w", "KeyW", 87, hold=0.3)
        target.cdp.call("Input.dispatchMouseEvent", type="mouseWheel", x=300, y=300, deltaX=0, deltaY=120)
        for i in range(10):
            target.move(300 + i * 10, 300)
        time.sleep(0.4)
        log = target.cdp.eval("window.__log") or []
        types = [e["t"] for e in log]
        trusted = all(e.get("trusted", True) for e in log if "trusted" in e)
        check("клик дошёл", "click" in types and "mousedown" in types, f"isTrusted={trusted}")
        check("клавиша дошла", "keydown" in types and "keyup" in types)
        check("колесо дошло", "wheel" in types)
        moves = [e for e in log if e["t"] == "mousemove"]
        check("движение мыши дошло", len(moves) >= 5, f"{len(moves)} событий, сумма movementX={sum(e['mx'] for e in moves)}")

        # захват курсора
        target.cdp.eval("window.__log.length = 0")
        target.click(750, 300)
        time.sleep(0.6)
        locked = bool(target.cdp.eval("!!document.pointerLockElement"))
        errs = [e for e in (target.cdp.eval("window.__log") or []) if e["t"] == "pointerlockerror"]
        check("захват курсора включился", locked, ("ошибка: " + str(errs[0].get("msg", ""))) if errs else "")
        if locked:
            target.cdp.eval("window.__log.length = 0")
            x = 750
            for i in range(10):
                x += 15
                target.move(x, 300)
            time.sleep(0.4)
            lm = [e for e in (target.cdp.eval("window.__log") or []) if e["t"] == "mousemove" and e.get("locked")]
            total = sum(e["mx"] for e in lm)
            check("движение мыши в захвате даёт поворот (movementX)", total != 0, f"{len(lm)} событий, сумма movementX={total} (ожидалось ≈150)")
            target.cdp.eval("document.exitPointerLock()")

        # снимки экрана
        a, dt1 = target.screenshot(SHOTS_DIR / f"w{target.index}_hidden_1.png")
        time.sleep(1.0)
        b, dt2 = target.screenshot(SHOTS_DIR / f"w{target.index}_hidden_2.png")
        check("снимок закрытого окна обновляется", a != b, f"снимок за {dt1 * 1000:.0f} и {dt2 * 1000:.0f} мс, {len(b) // 1024} КБ")

        # свёрнутое окно
        if not HEADLESS and target.window_id is not None:
            target.set_state("minimized")
            time.sleep(1.5)
            f = fps(target)
            target.cdp.eval("window.__log.length = 0")
            target.click(200, 300)
            time.sleep(0.3)
            got = "click" in [e["t"] for e in (target.cdp.eval("window.__log") or [])]
            try:
                c, _ = target.screenshot(SHOTS_DIR / f"w{target.index}_minimized.png")
                shot = f"снимок {len(c) // 1024} КБ"
            except Exception as e:  # noqa: BLE001
                shot = f"снимок не получился: {e}"
            check("свёрнутое окно: клик дошёл", got)
            check("свёрнутое окно: анимация идёт", f > 5, f"{f:.0f} кадров/с, {shot}")
            target.set_state("maximized")
            time.sleep(0.8)

    # переключение окон
    if len(windows) > 1 and not HEADLESS:
        say("Переключение окон (Page.bringToFront):")
        times = []
        for w in windows * 2:
            t0 = time.time()
            w.front()
            times.append((time.time() - t0) * 1000)
            time.sleep(0.3)
        check("окна переключаются", True, f"в среднем {sum(times) / len(times):.0f} мс; ПРОВЕРЬ ГЛАЗАМИ, что окна действительно всплывали поверх")
    say(f"Снимки сохранены в {SHOTS_DIR}")


def load_report(windows: list[Window]) -> None:
    say("")
    say("=== Нагрузка (Chrome, по окнам, замер 3 с) ===")
    groups: dict[int, list[psutil.Process]] = {w.index: [] for w in windows}
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [])
        except Exception:  # noqa: BLE001
            continue
        for w in windows:
            if str(w.profile) in cmd:
                groups[w.index].append(p)
    for procs in groups.values():
        for p in procs:
            try:
                p.cpu_percent(None)
            except Exception:  # noqa: BLE001
                pass
    time.sleep(3)
    total_cpu = total_mem = 0.0
    for idx, procs in groups.items():
        cpu = mem = 0.0
        for p in procs:
            try:
                cpu += p.cpu_percent(None)
                mem += p.memory_info().rss
            except Exception:  # noqa: BLE001
                pass
        cpu /= psutil.cpu_count() or 1
        total_cpu += cpu
        total_mem += mem
        say(f"  Окно {idx}: процессор {cpu:.1f}% от всего ЦП, память {mem / 2**20:.0f} МБ, процессов {len(procs)}")
    vm = psutil.virtual_memory()
    say(f"  Всего: процессор {total_cpu:.1f}%, память {total_mem / 2**30:.1f} ГБ; свободно ОЗУ {vm.available / 2**30:.1f} из {vm.total / 2**30:.1f} ГБ")


def keepalive(windows: list[Window], minutes: float, every: float, key: str) -> None:
    say("")
    say(f"=== Удержание активности: {minutes:g} мин, каждые {every:g} с, клавиша: {key or 'нет, только мышь'} ===")
    say("Окно, в котором ты сейчас играешь, тоже получает действия. Ctrl+C — остановить.")
    keymap = {"w": ("w", "KeyW", 87), "space": (" ", "Space", 32), "shift": ("Shift", "ShiftLeft", 16)}
    end = time.time() + minutes * 60
    n = 0
    last_shot = 0.0
    try:
        while time.time() < end:
            n += 1
            for w in windows:
                try:
                    w.focus_emulation(True)
                    w.move(640, 360)
                    w.move(650, 365)
                    w.move(640, 360)
                    if key in keymap:
                        w.key(*keymap[key], hold=0.1)
                except Exception as e:  # noqa: BLE001
                    say(f"  окно {w.index}: ошибка {e}")
            if time.time() - last_shot > 120:
                last_shot = time.time()
                stamp = datetime.datetime.now().strftime("%H%M%S")
                for w in windows:
                    try:
                        w.screenshot(SHOTS_DIR / f"keepalive_w{w.index}_{stamp}.png")
                    except Exception as e:  # noqa: BLE001
                        say(f"  окно {w.index}: снимок не получился: {e}")
                say(f"  {datetime.datetime.now():%H:%M:%S} снимки сохранены, циклов {n}")
            time.sleep(every)
    except KeyboardInterrupt:
        pass
    say(f"Готово: {n} циклов. Посмотри снимки keepalive_* — не выкинуло ли Boosteroid за неактивность.")


def manual(windows: list[Window]) -> None:
    idx = ask_int(f"Номер окна (1–{len(windows)})", 1, 1, len(windows))
    w = windows[idx - 1]
    what = input("Что отправить: c — клик, k — клавиша W (1 с), m — движение мыши вправо: ").strip().lower()
    x = ask_int("X (пиксели от левого края страницы)", 640, 0, 10000)
    y = ask_int("Y", 360, 0, 10000)
    delay = ask_int("Через сколько секунд (успеешь переключиться на другое окно)", 3, 0, 60)
    time.sleep(delay)
    w.focus_emulation(True)
    if what == "c":
        w.click(x, y)
    elif what == "k":
        w.key("w", "KeyW", 87, hold=1.0)
    else:
        for i in range(30):
            w.move(x + i * 5, y)
            time.sleep(0.01)
    say(f"Ручная отправка в окно {idx}: '{what}' в ({x},{y}) — отправлено. Отметь, сработало ли в игре.")


# ---------------------------------------------------------------- меню

def ask_int(prompt: str, default: int, lo: int, hi: int) -> int:
    raw = input(f"{prompt} [{default}]: ").strip()
    try:
        v = int(raw) if raw else default
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def save_report() -> Path:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    path = APP_DIR / f"report_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt"
    path.write_text("\n".join(REPORT), encoding="utf-8")
    return path


def header(chrome: str) -> None:
    say(f"Panel6 пробник {VERSION}")
    say(f"Система: {platform.platform()}, Python {platform.python_version()}")
    try:
        ver = subprocess.run([chrome, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        ver = ""
    say(f"Chrome: {chrome} {ver}")
    say(f"Папка пробника: {APP_DIR}")


def main() -> None:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    chrome = find_chrome()
    header(chrome)
    port = start_test_server()
    test_url = f"http://127.0.0.1:{port}/"

    if "--auto" in sys.argv:
        n = 2
        windows = [Window(i, chrome) for i in range(1, n + 1)]
        for w in windows:
            w.launch(f"{test_url}#{w.index}")
        autotest(windows, test_url)
        load_report(windows)
        print("Отчёт:", save_report())
        for w in windows:
            if w.proc:
                w.proc.terminate()
        return

    n = ask_int("Сколько окон Chrome запустить (1–5)", 2, 1, 5)
    windows = [Window(i, chrome) for i in range(1, n + 1)]
    for w in windows:
        w.launch(f"{test_url}#{w.index}")
        say(f"Окно {w.index}: порт {w.port}, профиль {w.profile}")

    menu = """
1 — Автотест на тестовой странице (клики, клавиши, захват курсора, скрытое и свёрнутое окно)
2 — Открыть Boosteroid во всех окнах (войди в аккаунты, запусти игру)
3 — Снимки экрана всех окон сейчас
4 — Удержание активности (проверка, выкидывает ли Boosteroid)
5 — Ручная отправка клика / клавиши / движения в окно
6 — Нагрузка на компьютер
7 — Сохранить отчёт и открыть папку
0 — Выход (окна Chrome останутся открытыми)"""
    while True:
        print(menu)
        choice = input("> ").strip()
        try:
            if choice == "1":
                autotest(windows, test_url)
            elif choice == "2":
                for w in windows:
                    w.navigate(BOOSTEROID_URL)
                say("Boosteroid открыт во всех окнах.")
            elif choice == "3":
                stamp = datetime.datetime.now().strftime("%H%M%S")
                for w in windows:
                    _, dt = w.screenshot(SHOTS_DIR / f"now_w{w.index}_{stamp}.png")
                    say(f"  окно {w.index}: снимок за {dt * 1000:.0f} мс")
            elif choice == "4":
                minutes = ask_int("Сколько минут", 20, 1, 600)
                every = ask_int("Раз в сколько секунд действие", 30, 2, 600)
                key = input("Клавиша вдобавок к движению мыши (w / space / shift / пусто): ").strip().lower()
                keepalive(windows, minutes, every, key)
            elif choice == "5":
                manual(windows)
            elif choice == "6":
                load_report(windows)
            elif choice == "7":
                path = save_report()
                print("Отчёт:", path)
                if IS_WIN:
                    os.startfile(APP_DIR)  # noqa: S606
            elif choice == "0":
                path = save_report()
                print("Отчёт:", path)
                return
        except (websocket.WebSocketException, ConnectionError, socket.error, RuntimeError) as e:
            say(f"Ошибка: {e}. Переподключаюсь к окнам…")
            for w in windows:
                try:
                    w.launch(f"{test_url}#{w.index}") if not w.alive() else w.attach()
                except Exception as e2:  # noqa: BLE001
                    say(f"  окно {w.index}: {e2}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        say(f"Неожиданная ошибка: {exc!r}")
        print("Отчёт:", save_report())
        if IS_WIN and "--auto" not in sys.argv:
            input("Нажми Enter, чтобы закрыть окно…")
        raise
