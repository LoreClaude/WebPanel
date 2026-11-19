"""Kill switch: следит за подключением к интернету и сообщает, когда оно сменилось или стало чужим.

Признаки:
- маршрут в интернет: через какой локальный адрес и подключение система сейчас отправила бы пакет (VPN включили,
  выключили, переподключили, сменили Wi-Fi на кабель, связь пропала). Проверка запускается сразу по уведомлению
  Windows о смене маршрутов и адресов (и ещё 10 раз в секунду на всякий случай) и ничего не отправляет в сеть.
  Chrome сам узнаёт о смене сети с задержкой, поэтому окна закрываются раньше, чем страницы начнут переподключаться;
- IPv6: если он идёт через другое подключение, чем IPv4, — это утечка мимо VPN;
- внешний IP по ответу сервиса в интернете: смена сервера VPN с тем же адресом туннеля, IP не из разрешённых.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
import urllib.request
from dataclasses import dataclass, replace
from typing import Callable

import psutil

from . import win32

log = logging.getLogger(__name__)

ROUTE_PROBE = ("1.1.1.1", 53)  # UDP connect только спрашивает систему о маршруте, пакетов не уходит
ROUTE_PROBE6 = ("2606:4700:4700::1111", 53)
IP_SERVICES = ("https://1.1.1.1/cdn-cgi/trace", "https://api.ipify.org")


def route_source() -> str | None:
    """Локальный IPv4-адрес, через который система сейчас ходит в интернет; None — маршрута нет."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(ROUTE_PROBE)
            return s.getsockname()[0]
    except OSError:
        return None


def route_source6() -> str | None:
    """То же для IPv6. Teredo и 6to4 не считаются: Windows выбирает их только когда IPv4 нет вовсе."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
            s.connect(ROUTE_PROBE6)
            ip = s.getsockname()[0].split("%")[0]
    except OSError:
        return None
    try:
        addr = ipaddress.IPv6Address(ip)
    except ValueError:
        return None
    if addr.is_link_local or addr.teredo or addr.sixtofour:
        return None
    return ip


def interface_of(ip: str | None) -> str:
    """Название подключения Windows с этим адресом: Ethernet, Wi-Fi, ProtonVPN…"""
    if ip:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        try:
            for name, addrs in psutil.net_if_addrs().items():
                if any(a.family == family and a.address.split("%")[0] == ip for a in addrs):
                    return name
        except OSError:
            pass
    return ""


def _parse_ip(text: str) -> str | None:
    for line in text.splitlines():
        value = line.split("=", 1)[1] if line.startswith("ip=") else line
        try:
            return str(ipaddress.IPv4Address(value.strip()))
        except ValueError:
            continue
    return None


def public_ip(timeout: float = 4.0) -> str | None:
    """Внешний IPv4 по ответу сервиса; None — ни один сервис не ответил."""
    for url in IP_SERVICES:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                ip = _parse_ip(r.read(4096).decode("ascii", "replace"))
        except Exception as e:  # noqa: BLE001
            log.info("внешний IP через %s: %s", url, e)
            continue
        if ip:
            return ip
    return None


def parse_allowed(text: str) -> tuple[list[str], list[str]]:
    """«185.163.47.154, 1.2.3.4» -> (верные IPv4, ошибочные записи)."""
    good, bad = [], []
    for part in text.replace(";", " ").replace(",", " ").split():
        try:
            ip = str(ipaddress.IPv4Address(part))
        except ValueError:
            bad.append(part)
            continue
        if ip not in good:
            good.append(ip)
    return good, bad


@dataclass(frozen=True)
class NetState:
    local: str | None            # адрес маршрута в интернет (IPv4)
    iface: str = ""              # название подключения
    iface6: str = ""             # через какое подключение идёт IPv6 ("" — IPv6 в интернет нет)
    public: str | None = None    # внешний IPv4
    allowed: bool | None = None  # внешний IP из разрешённых (None — список не задан или IP ещё не известен)

    @property
    def leak6(self) -> bool:
        return bool(self.local and self.iface6 and self.iface6 != self.iface)

    def route(self) -> tuple:
        return self.local, self.iface, self.iface6

    def text(self) -> str:
        if not self.local:
            return "нет подключения к интернету"
        out = f"{self.iface or 'подключение'} {self.local}"
        if self.public:
            out += f" · внешний IP {self.public}"
            out += {True: " ✓", False: " ✗ не разрешён", None: ""}[self.allowed]
        if self.leak6:
            out += f" · ⚠ IPv6 идёт мимо, через {self.iface6}"
        return out


def current_route(probe=route_source, probe6=route_source6, iface=interface_of) -> NetState:
    local = probe()
    v6 = probe6()
    return NetState(local, iface(local), iface(v6) if v6 else "")


def check_network(allowed: list[str], fetch=public_ip, route: Callable[[], NetState] = current_route) -> tuple[bool, str]:
    """Можно ли сейчас запускать окна: внешний IP из разрешённых и IPv6 не идёт мимо VPN.

    Возвращает (да/нет, внешний IP или причина отказа).
    """
    state = route()
    if not state.local:
        return False, "нет подключения к интернету"
    if state.leak6:
        return False, (f"IPv6 идёт мимо {state.iface} — через {state.iface6}. Включи в VPN защиту от утечки IPv6 "
                       f"или отключи IPv6 у подключения «{state.iface6}»")
    ip = fetch()
    if not ip:
        return False, "не удалось узнать внешний IP"
    if ip not in allowed:
        return False, f"внешний IP {ip}, а разрешён только {', '.join(allowed)}"
    if route().route() != state.route():
        return False, "подключение сменилось во время проверки"
    return True, ip


class NetGuard:
    """Наблюдение в фоновом потоке. on_change(что случилось) вызывается прямо из него — чтобы окна закрылись сразу,
    без очереди интерфейса. on_state(NetState) — для строки в панели.

    Все решения принимает один поток: Windows только будит его, запрос внешнего IP идёт отдельно и только приносит
    результат, а ответ, пришедший после смены маршрута, отбрасывается — так одна смена VPN не срабатывает дважды.
    """

    def __init__(self, on_change: Callable[[str], None], on_state: Callable[[NetState], None] = lambda s: None, *,
                 check_public: bool = True, public_every_s: float = 5.0, poll_s: float = 0.1,
                 allowed: list[str] | None = None,
                 probe: Callable[[], str | None] = route_source, probe6: Callable[[], str | None] = route_source6,
                 iface: Callable[[str | None], str] = interface_of, fetch: Callable[[], str | None] = public_ip,
                 clock: Callable[[], float] = time.monotonic, watch=win32.NetworkWatch):
        self.on_change = on_change
        self.on_state = on_state
        self.check_public = check_public or bool(allowed)  # со списком разрешённых IP проверять его обязательно
        self.public_every_s = public_every_s
        self.poll_s = poll_s
        self.allowed = list(allowed or [])
        self.probe, self.probe6, self.iface, self.fetch, self.clock = probe, probe6, iface, fetch, clock
        self.make_watch = watch
        self.state = NetState(None)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._watch = None
        self._v6: tuple[str | None, str] = (None, "")      # последний адрес IPv6 и его подключение
        self._epoch = 0                                     # растёт при смене маршрута
        self._result: tuple[str | None, int] | None = None  # (внешний IP, эпоха запроса) от потока запроса
        self._fetching = False

    def start(self) -> None:
        self._set(self._route())
        self._watch = self.make_watch(self._wake.set)
        self._thread = threading.Thread(target=self._run, daemon=True, name="netguard")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._watch:
            self._watch.close()
        if self._thread:
            self._thread.join(1)

    def wake(self) -> None:
        """Проверить прямо сейчас (так же будит уведомление Windows)."""
        self._wake.set()

    def _route(self) -> NetState:
        local = self.probe()
        v6 = self.probe6()
        if v6 != self._v6[0]:  # временные адреса IPv6 меняются сами — сравниваем подключение, а не адрес
            self._v6 = (v6, self.iface(v6) if v6 else "")
        iface = self.state.iface if local == self.state.local and self.state.iface else self.iface(local)
        return NetState(local, iface, self._v6[1])

    def _set(self, state: NetState) -> None:
        self.state = state
        try:
            self.on_state(state)
        except Exception:  # noqa: BLE001
            log.exception("on_state")

    def _fire(self, what: str) -> None:
        log.warning("kill switch: %s", what)
        try:
            self.on_change(what)
        except Exception:  # noqa: BLE001
            log.exception("on_change")

    def _fetch_public(self, epoch: int) -> None:
        try:
            ip = self.fetch()
        except Exception:  # noqa: BLE001
            ip = None
        self._result = (ip, epoch)
        self._fetching = False

    def _run(self) -> None:
        known: str | None = None   # внешний IP текущего подключения
        next_fetch = 0.0
        burst_until = 0.0          # после уведомления Windows внешний IP проверяется раз в секунду
        while True:
            notified = self._wake.wait(self.poll_s)
            if self._stop.is_set():
                return
            self._wake.clear()
            if notified:
                # сеть что-то меняла, а маршрут мог остаться прежним: VPN сменил сервер, не роняя туннель
                burst_until = self.clock() + 3.0
                next_fetch = min(next_fetch, self.clock())
            route = self._route()
            if route.route() != self.state.route():
                old = self.state
                self._epoch += 1   # внешний IP нового подключения станет новой точкой отсчёта
                known, next_fetch = None, 0.0
                self._set(route)
                self._fire(f"сменилось подключение: {old.text()} → {route.text()}")
                continue
            result, self._result = self._result, None
            if result and result[1] == self._epoch and result[0]:
                ip = result[0]
                ok = (ip in self.allowed) if self.allowed else None
                state = replace(self.state, public=ip, allowed=ok)
                if known and ip != known:
                    old_ip = self.state.public
                    self._set(state)
                    self._fire(f"сменился внешний IP: {old_ip} → {ip}" + (" (не из разрешённых)" if ok is False else ""))
                elif known is None and ok is False:
                    self._set(state)
                    self._fire(f"внешний IP {ip} не из разрешённых ({', '.join(self.allowed)})")
                elif state != self.state:
                    self._set(state)
                known = ip
            if self.check_public and not self._fetching and self.clock() >= next_fetch:
                self._fetching = True
                every = min(1.0, self.public_every_s) if self.clock() < burst_until else self.public_every_s
                next_fetch = self.clock() + every
                threading.Thread(target=self._fetch_public, args=(self._epoch,), daemon=True,
                                 name="netguard-ip").start()
