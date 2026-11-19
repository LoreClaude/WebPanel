"""Настройки панели: хранятся в %APPDATA%\\Panel6\\config.json."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

BOOSTEROID_URL = "https://cloud.boosteroid.com/"
WINDOW_COUNT = 5

DEFAULTS: dict = {
    "chrome_path": "",
    "game_monitor": 0,
    "panel_monitor": 1,
    "windows": [
        {"name": f"Окно {i}", "url": BOOSTEROID_URL, "hotkey": f"Num{i}"} for i in range(1, WINDOW_COUNT + 1)
    ],
    "slot6": {
        "name": "Рабочий стол",
        "hotkey": "Num6",
        "mode": "desktop",          # desktop | window
        "window_title": "",         # часть заголовка окна для режима window
        "desktop_strategy": "offscreen",  # offscreen | minimize
    },
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
    # kill switch: закрыть окна 1–5, когда меняется подключение к интернету (VPN включили, выключили, сменили)
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


def load(path: Path | None = None) -> dict:
    path = path or config_path()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        loaded = {}
    return _merge(DEFAULTS, loaded)


def save(cfg: dict, path: Path | None = None) -> None:
    path = path or config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def profile_dir(index: int) -> Path:
    """Папка профиля Chrome для окна index (1..5)."""
    path = app_dir() / "profiles" / f"w{index}"
    path.mkdir(parents=True, exist_ok=True)
    return path
