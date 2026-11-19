"""Наборы настроек: свои макросы и основной макрос для каждого окна.

Каждый набор — отдельный файл в %APPDATA%\\Panel6\\presets, его можно скопировать или отправить другу.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from .macros import normalize_macro

log = logging.getLogger(__name__)
DEFAULT_NAME = "Основной"
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def normalize_preset(data: dict, name: str | None = None) -> dict:
    macros, seen = [], set()
    for m in data.get("macros") or []:
        if not isinstance(m, dict):
            continue
        m = normalize_macro(m)
        while m["id"] in seen:  # одинаковые id после ручного редактирования файла
            m = normalize_macro(dict(m, id=None))
        seen.add(m["id"])
        macros.append(m)
    ids = {m["id"] for m in macros}
    wm = data.get("window_macros") or {}
    window_macros = {str(i): (wm.get(str(i)) if wm.get(str(i)) in ids else "") for i in range(1, 6)}
    return {"name": str(name or data.get("name") or DEFAULT_NAME).strip() or DEFAULT_NAME,
            "macros": macros, "window_macros": window_macros}


def file_name(name: str) -> str:
    base = _BAD_CHARS.sub("_", name).strip(" .") or "набор"
    return base[:80] + ".json"


class PresetStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self.root / file_name(name)

    def names(self) -> list[str]:
        out = []
        for p in sorted(self.root.glob("*.json")):
            try:
                out.append(str(json.loads(p.read_text(encoding="utf-8")).get("name") or p.stem))
            except (OSError, ValueError):
                log.warning("набор %s не читается", p)
        return sorted(set(out), key=str.lower)

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def load(self, name: str) -> dict:
        try:
            data = json.loads(self._path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return normalize_preset(data, name)

    def save(self, preset: dict) -> None:
        preset = normalize_preset(preset)
        path = self._path(preset["name"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(preset, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def ensure_default(self) -> str:
        names = self.names()
        if names:
            return names[0]
        self.save({"name": DEFAULT_NAME})
        return DEFAULT_NAME

    def unique_name(self, base: str) -> str:
        name, n = base, 2
        while self.exists(name):
            name, n = f"{base} ({n})", n + 1
        return name

    def create(self, name: str, copy_from: dict | None = None) -> str:
        name = self.unique_name(name.strip() or "Новый набор")
        data = dict(copy_from or {}, name=name)
        self.save(data)
        return name

    def rename(self, old: str, new: str) -> str:
        new = new.strip()
        if not new or new == old:
            return old
        # «Фарм» -> «фарм»: на Windows это тот же файл, имя не должно получить «(2)»
        same_file = file_name(new).lower() == file_name(old).lower()
        if not same_file:
            new = self.unique_name(new)
        preset = self.load(old)
        preset["name"] = new
        old_path = self._path(old)
        if same_file:
            old_path.unlink(missing_ok=True)
            self.save(preset)
        else:
            self.save(preset)
            old_path.unlink(missing_ok=True)
        return new

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)

    def export(self, name: str, dest: Path) -> None:
        Path(dest).write_text(json.dumps(self.load(name), ensure_ascii=False, indent=2), encoding="utf-8")

    def import_file(self, src: Path) -> str:
        data = json.loads(Path(src).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("macros", []), list):
            raise ValueError("это не файл набора Panel6")
        name = self.unique_name(str(data.get("name") or Path(src).stem))
        self.save(dict(data, name=name))
        return name
