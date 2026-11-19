"""Обёртки над Win32 и DWM. На других системах функции ничего не делают (для тестов)."""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes

log = logging.getLogger(__name__)
IS_WIN = sys.platform == "win32"

if IS_WIN:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")

    HWND = wintypes.HWND
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, HWND, wintypes.LPARAM)

    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [HWND]
    user32.IsWindow.argtypes = [HWND]
    user32.IsIconic.argtypes = [HWND]
    user32.GetWindowTextLengthW.argtypes = [HWND]
    user32.GetWindowTextW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetForegroundWindow.restype = HWND
    user32.SetForegroundWindow.argtypes = [HWND]
    user32.BringWindowToTop.argtypes = [HWND]
    user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
    user32.SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.GetShellWindow.restype = HWND
    user32.RegisterHotKey.argtypes = [HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    user32.UnregisterHotKey.argtypes = [HWND, ctypes.c_int]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), HWND, wintypes.UINT, wintypes.UINT]
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_void_p]
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    class LASTINPUTINFO(ctypes.Structure):  # noqa: N801
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
    kernel32.GetTickCount.restype = wintypes.DWORD

    class DWM_THUMBNAIL_PROPERTIES(ctypes.Structure):  # noqa: N801
        _fields_ = [
            ("dwFlags", wintypes.DWORD),
            ("rcDestination", wintypes.RECT),
            ("rcSource", wintypes.RECT),
            ("opacity", ctypes.c_ubyte),
            ("fVisible", wintypes.BOOL),
            ("fSourceClientAreaOnly", wintypes.BOOL),
        ]

    HTHUMBNAIL = ctypes.c_void_p
    dwmapi.DwmRegisterThumbnail.argtypes = [HWND, HWND, ctypes.POINTER(HTHUMBNAIL)]
    dwmapi.DwmRegisterThumbnail.restype = ctypes.c_long
    dwmapi.DwmUnregisterThumbnail.argtypes = [HTHUMBNAIL]
    dwmapi.DwmUnregisterThumbnail.restype = ctypes.c_long
    dwmapi.DwmUpdateThumbnailProperties.argtypes = [HTHUMBNAIL, ctypes.POINTER(DWM_THUMBNAIL_PROPERTIES)]
    dwmapi.DwmUpdateThumbnailProperties.restype = ctypes.c_long
    dwmapi.DwmQueryThumbnailSourceSize.argtypes = [HTHUMBNAIL, ctypes.POINTER(wintypes.SIZE)]
    dwmapi.DwmQueryThumbnailSourceSize.restype = ctypes.c_long

if IS_WIN:
    iphlpapi = ctypes.WinDLL("iphlpapi")
    # VOID callback(PVOID CallerContext, PMIB_..._ROW Row, MIB_NOTIFICATION_TYPE NotificationType)
    NET_CHANGE_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int)
    _NET_NOTIFY = ("NotifyRouteChange2", "NotifyUnicastIpAddressChange", "NotifyIpInterfaceChange")
    for _name in _NET_NOTIFY:
        _fn = getattr(iphlpapi, _name)
        _fn.argtypes = [ctypes.c_ushort, NET_CHANGE_CALLBACK, ctypes.c_void_p, ctypes.c_ubyte,
                        ctypes.POINTER(wintypes.HANDLE)]
        _fn.restype = wintypes.DWORD
    iphlpapi.CancelMibChangeNotify2.argtypes = [wintypes.HANDLE]
    iphlpapi.CancelMibChangeNotify2.restype = wintypes.DWORD

SW_RESTORE, SW_SHOWMINNOACTIVE = 9, 7
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
HWND_TOPMOST = -1
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x2


# ------------------------------------------------------------------ окна

def window_title(hwnd: int) -> str:
    if not IS_WIN:
        return ""
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def window_class(hwnd: int) -> str:
    if not IS_WIN:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_pid(hwnd: int) -> int:
    if not IS_WIN:
        return 0
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    if not IS_WIN:
        return (0, 0, 0, 0)
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def is_window(hwnd: int | None) -> bool:
    return bool(IS_WIN and hwnd and user32.IsWindow(hwnd))


def top_windows() -> list[int]:
    """Видимые окна верхнего уровня с заголовком, в порядке Z (сверху вниз)."""
    if not IS_WIN:
        return []
    found: list[int] = []

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def chrome_window_for_pid(pid: int) -> int | None:
    """Главное окно браузера с указанным процессом (самое большое видимое Chrome_WidgetWin_1)."""
    best, best_area = None, 0
    for hwnd in top_windows():
        if window_pid(hwnd) != pid or window_class(hwnd) != "Chrome_WidgetWin_1":
            continue
        _, _, w, h = window_rect(hwnd)
        if w * h > best_area:
            best, best_area = hwnd, w * h
    return best


def find_window_by_title(part: str, exclude_pids: set[int] = frozenset()) -> int | None:
    part = (part or "").strip().lower()
    if not part:
        return None
    for hwnd in top_windows():
        if part in window_title(hwnd).lower() and window_pid(hwnd) not in exclude_pids:
            return hwnd
    return None


def activate(hwnd: int) -> bool:
    """Выводит окно на передний план, обходя запрет Windows на смену активного окна."""
    if not is_window(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    fg = user32.GetForegroundWindow()
    cur = kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(fg_thread and fg_thread != cur and user32.AttachThreadInput(cur, fg_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(cur, fg_thread, False)
    if user32.GetForegroundWindow() != hwnd:
        # Нажатие Alt снимает блокировку смены переднего окна
        user32.keybd_event(VK_MENU, 0, 0, None)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, None)
        user32.SetForegroundWindow(hwnd)
    ok = user32.GetForegroundWindow() == hwnd
    if not ok:
        log.warning("не удалось вывести окно %s на передний план", hwnd)
    return ok


def move_window(hwnd: int, x: int, y: int) -> None:
    if is_window(hwnd):
        user32.SetWindowPos(hwnd, None, int(x), int(y), 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)


def keep_on_top(hwnd: int) -> None:
    """Поднимает окно над всеми, не делая его активным (окно игры не теряет фокус и захват курсора)."""
    if is_window(hwnd):
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def minimize_no_activate(hwnd: int) -> None:
    if is_window(hwnd):
        user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)


def restore(hwnd: int) -> None:
    if is_window(hwnd) and user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)


def is_minimized(hwnd: int) -> bool:
    return bool(is_window(hwnd) and user32.IsIconic(hwnd))


def show_desktop_shell() -> None:
    if IS_WIN:
        shell = user32.GetShellWindow()
        if shell:
            user32.SetForegroundWindow(shell)


def foreground_window() -> int | None:
    if not IS_WIN:
        return None
    return user32.GetForegroundWindow() or None


def last_input_age_ms() -> int:
    """Сколько миллисекунд назад была настоящая мышь или клавиатура (действия макросов сюда не попадают)."""
    if not IS_WIN:
        return 10**9
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 10**9
    return (kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF


def virtual_screen_right() -> int:
    if not IS_WIN:
        return 10000
    return user32.GetSystemMetrics(76) + user32.GetSystemMetrics(78)  # SM_XVIRTUALSCREEN + SM_CXVIRTUALSCREEN


# ------------------------------------------------------------------ DWM-миниатюры

class Thumbnail:
    """Живая миниатюра окна-источника, нарисованная DWM внутри окна-приёмника."""

    def __init__(self, dest: int, src: int):
        self.dest, self.src, self.handle = dest, src, None
        if IS_WIN:
            h = HTHUMBNAIL()
            hr = dwmapi.DwmRegisterThumbnail(dest, src, ctypes.byref(h))
            if hr == 0:
                self.handle = h
            else:
                log.warning("DwmRegisterThumbnail: 0x%08X", hr & 0xFFFFFFFF)

    @property
    def ok(self) -> bool:
        return self.handle is not None

    def source_size(self) -> tuple[int, int]:
        if not self.ok:
            return (0, 0)
        s = wintypes.SIZE()
        if dwmapi.DwmQueryThumbnailSourceSize(self.handle, ctypes.byref(s)) != 0:
            return (0, 0)
        return s.cx, s.cy

    def update(self, x: int, y: int, w: int, h: int, visible: bool = True) -> None:
        if not self.ok:
            return
        p = DWM_THUMBNAIL_PROPERTIES()
        p.dwFlags = 0x1 | 0x4 | 0x8 | 0x10  # назначение, прозрачность, видимость, только клиентская область
        p.rcDestination = wintypes.RECT(int(x), int(y), int(x + w), int(y + h))
        p.opacity = 255
        p.fVisible = bool(visible)
        p.fSourceClientAreaOnly = True
        dwmapi.DwmUpdateThumbnailProperties(self.handle, ctypes.byref(p))

    def close(self) -> None:
        if self.ok:
            dwmapi.DwmUnregisterThumbnail(self.handle)
            self.handle = None


# ------------------------------------------------------------------ глобальные горячие клавиши

class HotkeyThread(threading.Thread):
    """Регистрирует горячие клавиши в своём потоке и вызывает callback(name) при нажатии.

    bindings: {имя: (модификаторы, vk)}. Ошибки регистрации — в self.failed.
    """

    def __init__(self, bindings: dict[str, tuple[int, int]], callback):
        super().__init__(daemon=True, name="hotkeys")
        self.bindings = bindings
        self.callback = callback
        self.failed: list[str] = []
        self.ready = threading.Event()
        self.thread_id = 0

    def run(self) -> None:
        if not IS_WIN:
            self.ready.set()
            return
        self.thread_id = kernel32.GetCurrentThreadId()
        ids = {}
        for i, (name, (mods, vk)) in enumerate(self.bindings.items(), start=1):
            if user32.RegisterHotKey(None, i, mods | 0x4000, vk):
                ids[i] = name
            else:
                self.failed.append(name)
        self.ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in ids:
                try:
                    self.callback(ids[msg.wParam])
                except Exception:  # noqa: BLE001
                    log.exception("ошибка обработчика горячей клавиши")
        for i in ids:
            user32.UnregisterHotKey(None, i)

    def stop(self) -> None:
        if IS_WIN and self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        self.join(timeout=2)


# ------------------------------------------------------------------ уведомления о смене сети

class NetworkWatch:
    """Windows сообщает о смене маршрутов, адресов и сетевых интерфейсов сразу — callback() вызывается из её потока.

    Нужен kill switch: проверка начинается в тот же миг, а не по таймеру. На других системах ничего не делает.
    """

    def __init__(self, callback):
        self.handles: list = []
        self._cb = None
        if not IS_WIN:
            return
        self._cb = NET_CHANGE_CALLBACK(lambda _ctx, _row, _kind: callback())  # ссылка держится, пока подписка жива
        for name in _NET_NOTIFY:
            handle = wintypes.HANDLE()
            err = getattr(iphlpapi, name)(0, self._cb, None, 0, ctypes.byref(handle))  # 0 = AF_UNSPEC: IPv4 и IPv6
            if err:
                log.warning("%s: ошибка %s", name, err)
            else:
                self.handles.append(handle)

    def close(self) -> None:
        # ctypes отпускает GIL на время вызова, поэтому идущий callback может доработать — без взаимной блокировки
        for handle in self.handles:
            iphlpapi.CancelMibChangeNotify2(handle)
        self.handles = []
