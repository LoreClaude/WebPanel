"""Горячие клавиши: разбор строк вида "Ctrl+Alt+Num1" и перевод клавиш для протокола Chrome."""

from __future__ import annotations

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}

VK_NAMES: dict[str, int] = {}
for _i in range(10):
    VK_NAMES[f"Num{_i}"] = 0x60 + _i
    VK_NAMES[str(_i)] = 0x30 + _i
for _i in range(1, 25):
    VK_NAMES[f"F{_i}"] = 0x6F + _i
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    VK_NAMES[_c] = ord(_c)
VK_NAMES.update({
    "NumMul": 0x6A, "NumAdd": 0x6B, "NumSub": 0x6D, "NumDecimal": 0x6E, "NumDiv": 0x6F,
    "Space": 0x20, "Tab": 0x09, "Enter": 0x0D, "Esc": 0x1B, "Backspace": 0x08,
    "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
    "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28,
    "Pause": 0x13, "ScrollLock": 0x91, "`": 0xC0,
    # клавиши со знаками основной клавиатуры; в русской раскладке те же физические клавиши
    "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
    "\\": 0xDC,
})
VK_TO_NAME = {vk: name for name, vk in VK_NAMES.items()}
_LOWER = {name.lower(): vk for name, vk in VK_NAMES.items()}


class HotkeyError(ValueError):
    pass


def parse_hotkey(text: str) -> tuple[int, int]:
    """'Ctrl+Num1' -> (модификаторы, виртуальный код клавиши)."""
    parts = [p.strip() for p in (text or "").split("+") if p.strip()]
    if not parts:
        raise HotkeyError("пустое сочетание")
    mods = 0
    for p in parts[:-1]:
        m = _MODS.get(p.lower())
        if m is None:
            raise HotkeyError(f"неизвестный модификатор «{p}»")
        mods |= m
    vk = _LOWER.get(parts[-1].lower())
    if vk is None:
        raise HotkeyError(f"неизвестная клавиша «{parts[-1]}»")
    return mods, vk


def format_hotkey(mods: int, vk: int) -> str:
    names = []
    if mods & MOD_CONTROL:
        names.append("Ctrl")
    if mods & MOD_ALT:
        names.append("Alt")
    if mods & MOD_SHIFT:
        names.append("Shift")
    if mods & MOD_WIN:
        names.append("Win")
    names.append(VK_TO_NAME.get(vk, f"VK{vk:02X}"))
    return "+".join(names)


# --- виртуальный код Windows -> поля key/code для Input.dispatchKeyEvent

_SPECIAL = {
    0x08: ("Backspace", "Backspace"), 0x09: ("Tab", "Tab"), 0x0D: ("Enter", "Enter"),
    0x10: ("Shift", "ShiftLeft"), 0x11: ("Control", "ControlLeft"), 0x12: ("Alt", "AltLeft"),
    0x1B: ("Escape", "Escape"), 0x20: (" ", "Space"), 0x21: ("PageUp", "PageUp"),
    0x22: ("PageDown", "PageDown"), 0x23: ("End", "End"), 0x24: ("Home", "Home"),
    0x25: ("ArrowLeft", "ArrowLeft"), 0x26: ("ArrowUp", "ArrowUp"), 0x27: ("ArrowRight", "ArrowRight"),
    0x28: ("ArrowDown", "ArrowDown"), 0x2D: ("Insert", "Insert"), 0x2E: ("Delete", "Delete"),
    0xA0: ("Shift", "ShiftLeft"), 0xA1: ("Shift", "ShiftRight"), 0xA2: ("Control", "ControlLeft"),
    0xA3: ("Control", "ControlRight"), 0xA4: ("Alt", "AltLeft"), 0xA5: ("Alt", "AltRight"),
    0xBA: (";", "Semicolon"), 0xBB: ("=", "Equal"), 0xBC: (",", "Comma"), 0xBD: ("-", "Minus"),
    0xBE: (".", "Period"), 0xBF: ("/", "Slash"), 0xC0: ("`", "Backquote"), 0xDB: ("[", "BracketLeft"),
    0xDC: ("\\", "Backslash"), 0xDD: ("]", "BracketRight"), 0xDE: ("'", "Quote"),
    0x6A: ("*", "NumpadMultiply"), 0x6B: ("+", "NumpadAdd"), 0x6D: ("-", "NumpadSubtract"),
    0x6E: (".", "NumpadDecimal"), 0x6F: ("/", "NumpadDivide"),
}

_PRINTABLE_OEM = {0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF, 0xC0, 0xDB, 0xDC, 0xDD, 0xDE}


def cdp_key(vk: int, text: str = "") -> dict | None:
    """Параметры для Input.dispatchKeyEvent по виртуальному коду клавиши; None — клавиша неизвестна."""
    if 0x41 <= vk <= 0x5A:
        ch = chr(vk)
        key, code = (text if text and text.isprintable() else ch.lower()), "Key" + ch
    elif 0x30 <= vk <= 0x39:
        key, code = (text if text and text.isprintable() else chr(vk)), "Digit" + chr(vk)
    elif 0x60 <= vk <= 0x69:
        key, code = str(vk - 0x60), f"Numpad{vk - 0x60}"
    elif 0x70 <= vk <= 0x87:
        key = code = f"F{vk - 0x6F}"
    elif vk in _SPECIAL:
        key, code = _SPECIAL[vk]
        if vk in _PRINTABLE_OEM and len(text) == 1 and text.isprintable():
            key = text  # знак текущей раскладки, как у букв
    else:
        return None
    params = {"key": key, "code": code, "windowsVirtualKeyCode": vk}
    if len(key) == 1:
        params["text"] = key
    return params
