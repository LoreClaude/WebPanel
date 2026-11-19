"""Настройки панели: хранятся в %APPDATA%\\Panel6\\config.json."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

BOOSTEROID_URL = "https://cloud.boosteroid.com/"
WINDOW_COUNT = 5        # окон Chrome по умолчанию
MAX_WINDOWS = 20        # сколько окон Chrome можно задать в настройках
MAX_EXTRA = 10          # сколько окон «рабочий стол / программа» можно задать в настройках


def chrome_window(i: int) -> dict:
    """Окно Chrome по умолчанию: у первых пяти клавиши Num1–Num5, у остальных клавиша не назначена."""
    return {"name": f"Окно {i}", "url": BOOSTEROID_URL, "hotkey": f"Num{i}" if i <= 5 else "", "enabled": True}


def extra_window(i: int) -> dict:
    """Окно «рабочий стол или программа» по умолчанию; i — его номер среди таких окон (с 1)."""
    return {
        "name": "Рабочий стол" if i == 1 else f"Рабочий стол {i}",
        "hotkey": "Num6" if i == 1 else "",
        "enabled": True,
        "mode": "desktop",          # desktop | window
        "window_title": "",         # часть заголовка окна для режима window
        "desktop_strategy": "offscreen",  # offscreen | minimize
    }


DEFAULTS: dict = {
    "chrome_path": "",
    "game_monitor": 0,
    "panel_monitor": 1,
    # окна Chrome: сколько угодно (до MAX_WINDOWS), каждое можно выключить
    "windows": [chrome_window(i) for i in range(1, WINDOW_COUNT + 1)],
    # рабочий стол и окна программ: идут после окон Chrome, их тоже сколько угодно (до MAX_EXTRA)
    "extra_windows": [extra_window(1)],
    "hotkeys": {
        "grid": "Num0", "back": "Num9", "next": "Num8", "prev": "Num7",
        "macro_toggle": "NumAdd",   # старт/пауза основного макроса текущего окна
        "stop_all": "NumSub",       # аварийная остановка всех макросов
        "record": "NumMul",         # начать/закончить запись макроса в текущем окне
    },
    "active_preset": "",
    # что делать с макросом, когда ты сам действуешь в его окне: pause | keep | stop
    "macro_user_input": {"mode": "pause", "idle_sec": 1.0},
    # метка поверх окна игры: номер окна и состояние макросов
    "badge": {"enabled": True, "corner": "top_right"},
    # kill switch: закрыть окна Chrome, когда меняется подключение к интернету (VPN включили, выключили, сменили)
    # allowed_ips — внешние IP, с которых можно запускать окна (через запятую; пусто — любой)
    "kill_switch": {"enabled": True, "public_ip": True, "interval_sec": 5.0, "allowed_ips": "185.163.47.154"},
}


def app_dir() -> Path:
    if os.environ.get("PANEL6_HOME"):
        path = Path(os.environ["PANEL6_HOME"])
    else:
        path = Path(os.environ.get("APPDATA") or Path.home()) / "Panel6"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return app_dir() / "config.json"


def _merge(default, loaded):
    """Накладывает сохранённые значения на значения по умолчанию, сохраняя структуру."""
    if isinstance(default, dict) and isinstance(loaded, dict):
        out = {}
        for key, value in default.items():
            out[key] = _merge(value, loaded[key]) if key in loaded else copy.deepcopy(value)
        return out
    if isinstance(default, list) and isinstance(loaded, list):
        return [_merge(d, loaded[i]) if i < len(loaded) else copy.deepcopy(d) for i, d in enumerate(default)]
    if default is None or loaded is None:
        return loaded if loaded is not None else default
    if isinstance(default, float) and isinstance(loaded, int) and not isinstance(loaded, bool):
        return float(loaded)
    return loaded if isinstance(loaded, type(default)) else copy.deepcopy(default)


def _merge_list(loaded, make, limit: int, default_len: int) -> list[dict]:
    """Список окон любой длины (0..limit): каждое окно дополняется значениями по умолчанию."""
    if not isinstance(loaded, list):
        return [make(i) for i in range(1, default_len + 1)]
    items = [x for x in loaded if isinstance(x, dict)][:limit]
    return [_merge(make(i), x) for i, x in enumerate(items, start=1)]


def load(path: Path | None = None) -> dict:
    path = path or config_path()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        loaded = {}
    if not isinstance(loaded, dict):
        loaded = {}
    cfg = _merge(DEFAULTS, loaded)
    cfg["windows"] = _merge_list(loaded.get("windows"), chrome_window, MAX_WINDOWS, WINDOW_COUNT)
    extra = loaded.get("extra_windows")
    if extra is None and isinstance(loaded.get("slot6"), dict):
        extra = [loaded["slot6"]]  # старый формат: одно «шестое окно»
    cfg["extra_windows"] = _merge_list(extra, extra_window, MAX_EXTRA, 1)
    return cfg


def save(cfg: dict, path: Path | None = None) -> None:
    path = path or config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def profile_dir(index: int) -> Path:
    """Папка профиля Chrome для окна Chrome номер index (с 1)."""
    path = app_dir() / "profiles" / f"w{index}"
    path.mkdir(parents=True, exist_ok=True)
    return path
