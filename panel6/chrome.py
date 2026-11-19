"""Окна Chrome: запуск отдельных копий с собственными профилями и управление ими по протоколу отладки."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import psutil
import websocket

from . import config, win32

log = logging.getLogger(__name__)
BASE_PORT = 9321

CHROME_FLAGS = [
    "--no-first-run",
    "--no-default-browser-check",
    # окна под другими окнами должны продолжать рисоваться и получать события
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-features=CalculateNativeWinOcclusion",
    "--autoplay-policy=no-user-gesture-required",
    # после kill switch Chrome не предлагает «восстановить страницы»
    "--hide-crash-restore-bubble",
]


def find_chrome(custom: str = "") -> str | None:
    if custom and Path(custom).exists():
        return custom
    env = os.environ.get("PANEL6_CHROME")
    if env and Path(env).exists():
        return env
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
        if base:
            p = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if p.exists():
                return str(p)
    for name in ("google-chrome", "chromium", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    return None


class CdpError(RuntimeError):
    pass


class Cdp:
    """Синхронный клиент протокола отладки для одной вкладки. Потокобезопасен."""

    def __init__(self, ws_url: str, timeout: float = 5.0):
        self.ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        self._id = 0
        self._lock = threading.Lock()

    def call(self, method: str, **params):
        with self._lock:
            self._id += 1
            mid = self._id
            self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
            while True:
                msg = json.loads(self.ws.recv())
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise CdpError(f"{method}: {msg['error'].get('message')}")
                    return msg.get("result", {})

    def eval(self, expr: str):
        res = self.call("Runtime.evaluate", expression=expr, returnByValue=True)
        return res.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


class ChromeWindow:
    """Одна копия Chrome (окна 1..5)."""

    def __init__(self, index: int, port: int | None = None):
        self.index = index
        self.port = port or BASE_PORT + index
        self.profile = config.profile_dir(index)
        self.cdp: Cdp | None = None
        self.hwnd: int | None = None
        self.pid: int | None = None
        self.needs_placement = False
        self.placement_rect: tuple[int, int, int, int] | None = None
        self._lock = threading.RLock()

    # ---------------------------------------------------------- состояние
    def debug_alive(self) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=0.3):
                return True
        except Exception:  # noqa: BLE001
            return False

    def browser_pid(self) -> int | None:
        needle = str(self.profile).lower()
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                cmd = p.info["cmdline"] or []
            except (psutil.Error, OSError):
                continue
            if not cmd or "chrome" not in (p.info["name"] or "").lower():
                continue
            joined = " ".join(cmd).lower()
            if needle in joined and "--type=" not in joined:
                return p.pid
        return None

    def refresh(self) -> str:
        """Обновляет pid/hwnd/подключение. Возвращает статус: off | starting | ready.

        Медленные проверки идут без блокировки, чтобы не задерживать макросы в этом окне.
        """
        if not self.debug_alive():
            with self._lock:
                self._drop()
            if self.pid is None or not psutil.pid_exists(self.pid):
                self.pid = self.browser_pid()
            return "starting" if self.pid else "off"
        if self.pid is None or not psutil.pid_exists(self.pid):  # поиск процесса только при необходимости
            self.pid = self.browser_pid()
        if self.pid and not win32.is_window(self.hwnd):
            self.hwnd = win32.chrome_window_for_pid(self.pid)
        with self._lock:
            if self.cdp is None:
                try:
                    self._attach()
                except Exception as e:  # noqa: BLE001
                    log.info("окно %s: не подключился: %s", self.index, e)
                    return "starting"
        if self.needs_placement and self.placement_rect:
            try:
                self.place_fullscreen(*self.placement_rect)
                self.needs_placement = False
            except Exception as e:  # noqa: BLE001
                log.warning("окно %s: не удалось развернуть: %s", self.index, e)
        return "ready"

    @property
    def connected(self) -> bool:
        return self.cdp is not None

    def _attach(self) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=2) as r:
            targets = json.loads(r.read().decode("utf-8"))
        pages = [t for t in targets if t.get("type") == "page" and not t.get("url", "").startswith("devtools://")]
        if not pages:
            raise CdpError("нет вкладки")
        self.cdp = Cdp(pages[0]["webSocketDebuggerUrl"])
        # страница считает себя активной, даже когда окно не в фокусе
        self.cdp.call("Emulation.setFocusEmulationEnabled", enabled=True)

    def _drop(self) -> None:
        if self.cdp:
            self.cdp.close()
        self.cdp = None

    def call(self, method: str, **params):
        """Вызов протокола с одним переподключением при обрыве."""
        with self._lock:
            for attempt in (1, 2):
                if self.cdp is None:
                    self._attach()
                try:
                    return self.cdp.call(method, **params)
                except (websocket.WebSocketException, OSError) as e:
                    self._drop()
                    if attempt == 2:
                        raise CdpError(str(e)) from e
        return None

    # ---------------------------------------------------------- запуск
    def launch(self, chrome: str, url: str, rect: tuple[int, int, int, int] | None) -> None:
        """Запускает копию Chrome (если ещё не запущена) и разворачивает её на весь экран в rect."""
        self.placement_rect = rect
        self.needs_placement = rect is not None
        if self.debug_alive():
            return
        args = [chrome, f"--remote-debugging-port={self.port}", f"--user-data-dir={self.profile}", *CHROME_FLAGS]
        if rect:
            x, y, w, h = rect
            args += [f"--window-position={x},{y}", f"--window-size={w},{h}"]
        if os.environ.get("PANEL6_HEADLESS") == "1":  # для автотестов
            args += ["--headless=new", "--window-size=1280,720"]
            if not sys.platform.startswith("win"):
                args.append("--no-sandbox")
        args.append(url)
        log.info("запуск окна %s: %s", self.index, " ".join(args))
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def place_fullscreen(self, x: int, y: int, w: int, h: int) -> None:
        wid = self.call("Browser.getWindowForTarget")["windowId"]
        state = self.call("Browser.getWindowBounds", windowId=wid)["bounds"].get("windowState")
        if state != "normal":
            self.call("Browser.setWindowBounds", windowId=wid, bounds={"windowState": "normal"})
        self.call("Browser.setWindowBounds", windowId=wid, bounds={"left": x, "top": y, "width": w, "height": h})
        self.call("Browser.setWindowBounds", windowId=wid, bounds={"windowState": "fullscreen"})

    # ---------------------------------------------------------- ввод
    def page_size(self) -> tuple[float, float]:
        v = self.call("Runtime.evaluate", expression="[innerWidth, innerHeight]", returnByValue=True)
        w, h = v["result"]["value"]
        return float(w), float(h)

    def mouse(self, kind: str, x: float, y: float, button: str = "left", clicks: int = 1, buttons: int = 0,
              modifiers: int = 0) -> None:
        self.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, button=button, buttons=buttons, clickCount=clicks,
                  modifiers=modifiers)

    def wheel(self, x: float, y: float, dy: float, modifiers: int = 0) -> None:
        self.call("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y, deltaX=0, deltaY=dy, modifiers=modifiers)

    def insert_text(self, text: str) -> None:
        self.call("Input.insertText", text=text)

    def screenshot(self) -> bytes:
        """Снимок страницы в PNG (работает и для окна под другими окнами)."""
        import base64

        return base64.b64decode(self.call("Page.captureScreenshot", format="png")["data"])

    def evaluate(self, expr: str):
        res = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return res.get("result", {}).get("value")

    def kill(self) -> bool:
        """Сразу завершает эту копию Chrome со всеми её процессами (kill switch). True — было что закрывать.

        Без блокировки окна: макрос может как раз ждать ответа Chrome, а закрыть нужно немедленно.
        """
        pid = self.pid if self.pid and psutil.pid_exists(self.pid) else self.browser_pid()
        if not pid:
            return False
        try:
            browser = psutil.Process(pid)
            procs = [browser, *browser.children(recursive=True)]  # список детей — пока процесс браузера жив
        except psutil.Error:
            return False
        for p in procs:  # сначала браузер: иначе он успел бы перезапустить сетевой процесс
            try:
                p.kill()
            except psutil.Error:
                pass
        self.pid = None
        self.hwnd = None
        log.warning("окно %s закрыто kill switch (процессов: %s)", self.index, len(procs))
        return True

    def close_browser(self) -> None:
        try:
            self.call("Browser.close")
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            self._drop()

    def key(self, kind: str, params: dict) -> None:
        p = dict(params)
        if kind == "keyUp":
            p.pop("text", None)
        self.call("Input.dispatchKeyEvent", type=kind, **p)
