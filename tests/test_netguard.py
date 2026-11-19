import sys
import time

import pytest

from panel6.netguard import NetGuard, NetState, _parse_ip, check_network, parse_allowed, route_source

VPN, LAN = "10.2.0.2", "192.168.1.10"
NAMES = {VPN: "ProtonVPN", LAN: "Ethernet"}


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def seq(values):
    """Отдаёт значения по очереди, потом повторяет последнее."""
    it, last = iter(values), [None]

    def f():
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return last[0]
    return f


class FakeWatch:
    """Вместо уведомлений Windows: тест сам «сообщает» о смене сети."""
    last = None

    def __init__(self, callback):
        self.callback = callback
        FakeWatch.last = self

    def close(self):
        pass


def run_guard(**kw):
    fired, states = [], []
    kw.setdefault("poll_s", 0.01)
    kw.setdefault("probe6", lambda: None)
    g = NetGuard(fired.append, states.append, iface=lambda ip: NAMES.get(ip, ""), watch=FakeWatch, **kw)
    g.start()
    return g, fired, states


def test_parse_ip():
    assert _parse_ip("fl=123\nh=1.1.1.1\nip=203.0.113.5\nts=1\n") == "203.0.113.5"  # ответ Cloudflare
    assert _parse_ip("198.51.100.7\n") == "198.51.100.7"                           # ответ ipify
    assert _parse_ip("<html>ошибка</html>") is None


def test_route_source_does_not_need_network():
    ip = route_source()  # UDP connect ничего не отправляет
    assert ip is None or ip.count(".") == 3


def test_vpn_off_fires_once_with_names():
    g, fired, states = run_guard(check_public=False, probe=seq([VPN, VPN, LAN]))
    try:
        assert wait_for(lambda: fired)
        time.sleep(0.1)
    finally:
        g.stop()
    assert fired == ["сменилось подключение: ProtonVPN 10.2.0.2 → Ethernet 192.168.1.10"]
    assert states[0].text() == "ProtonVPN 10.2.0.2" and states[-1].text() == "Ethernet 192.168.1.10"


def test_lost_internet_fires():
    g, fired, _ = run_guard(check_public=False, probe=seq([VPN, None]))
    try:
        assert wait_for(lambda: fired)
    finally:
        g.stop()
    assert fired == ["сменилось подключение: ProtonVPN 10.2.0.2 → нет подключения к интернету"]


def test_public_ip_change_fires_and_service_failures_do_not():
    g, fired, _ = run_guard(check_public=True, public_every_s=0.01, probe=lambda: VPN,
                            fetch=seq(["203.0.113.5", None, "203.0.113.5", "198.51.100.7"]))
    try:
        assert wait_for(lambda: fired)
        time.sleep(0.1)
    finally:
        g.stop()
    assert fired == ["сменился внешний IP: 203.0.113.5 → 198.51.100.7"]
    assert g.state.text() == "ProtonVPN 10.2.0.2 · внешний IP 198.51.100.7"


def test_vpn_off_is_reported_once_not_also_as_new_public_ip():
    """Выключили VPN: сменились и маршрут, и внешний IP — окна закрываются один раз."""
    route = {"ip": VPN}
    public = {VPN: "203.0.113.5", LAN: "198.51.100.7"}
    g, fired, _ = run_guard(check_public=True, public_every_s=0.01, probe=lambda: route["ip"],
                            fetch=lambda: public[route["ip"]])
    try:
        assert wait_for(lambda: g.state.public == "203.0.113.5")
        route["ip"] = LAN
        assert wait_for(lambda: g.state.public == "198.51.100.7")
        time.sleep(0.1)
    finally:
        g.stop()
    assert fired == ["сменилось подключение: ProtonVPN 10.2.0.2 · внешний IP 203.0.113.5 → Ethernet 192.168.1.10"]


ALLOWED = ["185.163.47.154"]


def test_allowed_ip_quiet_and_marked():
    g, fired, _ = run_guard(check_public=True, public_every_s=0.01, probe=lambda: VPN, allowed=ALLOWED,
                            fetch=lambda: "185.163.47.154")
    try:
        assert wait_for(lambda: g.state.allowed is True)
        time.sleep(0.1)
    finally:
        g.stop()
    assert fired == [] and g.state.text() == "ProtonVPN 10.2.0.2 · внешний IP 185.163.47.154 ✓"


def test_foreign_ip_fires_once_even_without_change():
    """Запустили панель (или окна были открыты) не с того IP — окна закрываются сразу, но один раз."""
    g, fired, _ = run_guard(check_public=False, public_every_s=0.01, probe=lambda: VPN, allowed=ALLOWED,
                            fetch=lambda: "203.0.113.5")  # список разрешённых включает проверку IP сам
    try:
        assert wait_for(lambda: fired)
        time.sleep(0.1)
    finally:
        g.stop()
    assert fired == ["внешний IP 203.0.113.5 не из разрешённых (185.163.47.154)"]
    assert g.state.allowed is False and g.state.text().endswith("✗ не разрешён")


def test_change_to_foreign_ip_is_marked():
    g, fired, _ = run_guard(check_public=True, public_every_s=0.01, probe=lambda: VPN, allowed=ALLOWED,
                            fetch=seq(["185.163.47.154", "203.0.113.5"]))
    try:
        assert wait_for(lambda: fired)
    finally:
        g.stop()
    assert fired == ["сменился внешний IP: 185.163.47.154 → 203.0.113.5 (не из разрешённых)"]


def test_ipv6_route_change_fires_but_temporary_address_rotation_does_not():
    names = {VPN: "ProtonVPN", "2001:db8::1": "Ethernet", "2001:db8::2": "Ethernet", "fd00::2": "ProtonVPN"}
    v6 = {"ip": "fd00::2"}
    fired = []
    g = NetGuard(fired.append, check_public=False, poll_s=0.01, probe=lambda: VPN, probe6=lambda: v6["ip"],
                 iface=lambda ip: names.get(ip, ""), watch=FakeWatch)
    g.start()
    try:
        time.sleep(0.05)
        assert fired == [] and not g.state.leak6  # IPv6 тоже идёт через VPN
        v6["ip"] = "2001:db8::1"                  # IPv6 пошёл мимо VPN
        assert wait_for(lambda: fired)
        assert g.state.leak6 and "IPv6 идёт мимо, через Ethernet" in g.state.text()
        v6["ip"] = "2001:db8::2"                  # Windows сменила временный адрес — подключение то же
        time.sleep(0.1)
    finally:
        g.stop()
    assert len(fired) == 1 and fired[0].startswith("сменилось подключение: ProtonVPN 10.2.0.2 → ProtonVPN 10.2.0.2")


def test_windows_notification_checks_at_once():
    """Уведомление Windows будит проверку сразу, не дожидаясь опроса по таймеру."""
    route = {"ip": VPN}
    g, fired, _ = run_guard(check_public=False, poll_s=30, probe=lambda: route["ip"])
    try:
        route["ip"] = LAN
        started = time.perf_counter()
        FakeWatch.last.callback()
        assert wait_for(lambda: fired, timeout=1)
        assert time.perf_counter() - started < 0.5
    finally:
        g.stop()


def test_notification_rechecks_public_ip_at_once():
    """VPN сменил сервер, не роняя туннель: маршрут тот же, внешний IP проверяется сразу после уведомления."""
    public = {"ip": "185.163.47.154"}
    g, fired, _ = run_guard(check_public=True, public_every_s=60, probe=lambda: VPN, fetch=lambda: public["ip"])
    try:
        assert wait_for(lambda: g.state.public == "185.163.47.154")
        public["ip"] = "203.0.113.5"
        FakeWatch.last.callback()
        assert wait_for(lambda: fired, timeout=1)
    finally:
        g.stop()
    assert fired == ["сменился внешний IP: 185.163.47.154 → 203.0.113.5"]


def test_parse_allowed():
    assert parse_allowed("185.163.47.154, 1.2.3.4;1.2.3.4  x.y") == (["185.163.47.154", "1.2.3.4"], ["x.y"])
    assert parse_allowed("") == ([], [])


@pytest.mark.parametrize("state, ip, ok, text", [
    (NetState(VPN, "ProtonVPN"), "185.163.47.154", True, "185.163.47.154"),
    (NetState(VPN, "ProtonVPN"), "203.0.113.5", False, "внешний IP 203.0.113.5, а разрешён только 185.163.47.154"),
    (NetState(VPN, "ProtonVPN"), None, False, "не удалось узнать внешний IP"),
    (NetState(None), "185.163.47.154", False, "нет подключения к интернету"),
    (NetState(VPN, "ProtonVPN", "Ethernet"), "185.163.47.154", False, "IPv6 идёт мимо ProtonVPN — через Ethernet"),
])
def test_check_network_before_launch(state, ip, ok, text):
    verdict = check_network(ALLOWED, fetch=lambda: ip, route=lambda: state)
    assert verdict[0] is ok and verdict[1].startswith(text)


def test_check_network_notices_change_during_check():
    states = iter([NetState(VPN, "ProtonVPN"), NetState(LAN, "Ethernet")])
    assert check_network(ALLOWED, fetch=lambda: "185.163.47.154", route=lambda: next(states)) == (
        False, "подключение сменилось во время проверки")


@pytest.mark.skipif(sys.platform != "win32", reason="уведомления о смене сети есть только в Windows")
def test_windows_network_watch_registers():
    from panel6.win32 import NetworkWatch

    w = NetworkWatch(lambda: None)
    try:
        assert len(w.handles) == 3  # маршруты, адреса, интерфейсы
    finally:
        w.close()
    assert w.handles == []
