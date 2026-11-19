import json

import pytest

from panel6 import config
from panel6.geometry import Rect, fit, grid_cells, to_page
from panel6.keys import HotkeyError, MOD_ALT, MOD_CONTROL, cdp_key, format_hotkey, parse_hotkey


def test_parse_hotkey_numpad_and_mods():
    assert parse_hotkey("Num1") == (0, 0x61)
    assert parse_hotkey("ctrl+alt+F5") == (MOD_CONTROL | MOD_ALT, 0x74)
    assert parse_hotkey("Alt+q") == (MOD_ALT, ord("Q"))


@pytest.mark.parametrize("bad", ["", "Hyper+1", "Ctrl+", "Num10"])
def test_parse_hotkey_errors(bad):
    with pytest.raises(HotkeyError):
        parse_hotkey(bad)


def test_format_roundtrip():
    for text in ["Num0", "Ctrl+Shift+A", "Alt+F12", "NumAdd", "Ctrl+Alt+Shift+Win+Space"]:
        assert format_hotkey(*parse_hotkey(text)) == text


@pytest.mark.parametrize("name, vk, code", [
    ("-", 0xBD, "Minus"), ("=", 0xBB, "Equal"), ("[", 0xDB, "BracketLeft"), ("]", 0xDD, "BracketRight"),
    (";", 0xBA, "Semicolon"), ("'", 0xDE, "Quote"), (",", 0xBC, "Comma"), (".", 0xBE, "Period"),
    ("/", 0xBF, "Slash"), ("\\", 0xDC, "Backslash"), ("`", 0xC0, "Backquote"),
])
def test_punctuation_keys(name, vk, code):
    assert parse_hotkey(name) == (0, vk)
    assert format_hotkey(*parse_hotkey("Ctrl+" + name)) == "Ctrl+" + name
    assert cdp_key(vk)["code"] == code and cdp_key(vk)["key"] == name


def test_cdp_key():
    assert cdp_key(0x57, "w") == {"key": "w", "code": "KeyW", "windowsVirtualKeyCode": 0x57, "text": "w"}
    assert cdp_key(0x57, "W")["key"] == "W"
    assert cdp_key(0x0D)["code"] == "Enter" and "text" not in cdp_key(0x0D)
    assert cdp_key(0x20)["text"] == " "
    assert cdp_key(0x64)["code"] == "Numpad4"
    assert cdp_key(0x6D)["code"] == "NumpadSubtract"
    assert cdp_key(0xBA, "ж")["key"] == "ж"  # знак клавиши в русской раскладке
    assert cdp_key(0xFF) is None


def test_grid_cells_cover_area():
    cells = grid_cells(1920, 1080, gap=10, margin=10)
    assert len(cells) == 6
    assert cells[0].x == 10 and cells[0].y == 10
    last = cells[-1]
    assert abs(last.x + last.w - 1910) < 1e-6 and abs(last.y + last.h - 1070) < 1e-6


def test_fit_keeps_aspect_and_centers():
    r = fit(Rect(0, 0, 400, 400), 1600, 900)
    assert r.w == 400 and r.h == 225 and r.y == pytest.approx(87.5)


def test_to_page_maps_corners():
    thumb = Rect(100, 50, 320, 180)
    assert to_page(100, 50, thumb, 2560, 1440) == (0, 0)
    assert to_page(260, 140, thumb, 2560, 1440) == (1280, 720)
    assert to_page(99, 50, thumb, 2560, 1440) is None


def test_config_merge_keeps_new_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"windows": [{"name": "Фарм", "hotkey": "Alt+1"}], "hotkeys": {"grid": "F9"},
                                "game_monitor": "bad"}), encoding="utf-8")
    cfg = config.load(path)
    assert cfg["windows"][0]["name"] == "Фарм"
    assert cfg["windows"][0]["url"] == config.BOOSTEROID_URL
    assert len(cfg["windows"]) == 1 and cfg["windows"][0]["enabled"] is True  # число окон — как сохранено
    assert cfg["hotkeys"]["grid"] == "F9" and cfg["hotkeys"]["back"] == "Num9"
    assert cfg["game_monitor"] == 0
    config.save(cfg, path)
    assert config.load(path) == cfg


def test_config_broken_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{oops", encoding="utf-8")
    assert config.load(path) == config.DEFAULTS


def test_default_hotkeys_are_unique():
    cfg = config.DEFAULTS
    keys = [w["hotkey"] for w in cfg["windows"] + cfg["extra_windows"]] + list(cfg["hotkeys"].values())
    combos = [parse_hotkey(k) for k in keys]
    assert len(set(combos)) == len(combos)


def test_config_migrates_slot6_and_keeps_window_counts(tmp_path):
    path = tmp_path / "config.json"
    old = {"windows": [{"name": f"W{i}", "hotkey": f"Num{i}", "url": "u"} for i in range(1, 6)],
           "slot6": {"name": "Steam", "hotkey": "Num6", "mode": "window", "window_title": "Steam"}}
    path.write_text(json.dumps(old), encoding="utf-8")
    cfg = config.load(path)
    assert "slot6" not in cfg and len(cfg["windows"]) == 5
    assert cfg["extra_windows"] == [{"name": "Steam", "hotkey": "Num6", "enabled": True, "mode": "window",
                                     "window_title": "Steam", "desktop_strategy": "offscreen"}]
    cfg["windows"] += [config.chrome_window(6), config.chrome_window(7)]
    cfg["windows"][1]["enabled"] = False
    cfg["extra_windows"].append(config.extra_window(2))
    config.save(cfg, path)
    again = config.load(path)
    assert again == cfg and len(again["windows"]) == 7 and again["windows"][1]["enabled"] is False
    assert again["windows"][6]["hotkey"] == ""  # после пятого окна клавиша не назначена

    path.write_text(json.dumps({"windows": [{}] * 50, "extra_windows": []}), encoding="utf-8")
    cfg = config.load(path)
    assert len(cfg["windows"]) == config.MAX_WINDOWS and cfg["extra_windows"] == []
