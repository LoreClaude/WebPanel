import pytest

pytest.importorskip("PySide6")

from panel6.controller import SlotHistory  # noqa: E402


def test_history_back_and_cycle():
    h = SlotHistory(6)
    assert h.step(+1) == 1 and h.step(-1) == 6
    h.visit(2)
    h.visit(5)
    assert h.back() == 2
    assert h.step(+1) == 6
    h.visit(6)
    assert h.step(+1) == 1
    assert h.step(-1) == 5
    h.visit(6)
    assert h.back() == 5


def test_history_skips_disabled_slots():
    enabled = [1, 3, 4, 7]
    h = SlotHistory(lambda: enabled)
    assert h.step(+1) == 1 and h.step(-1) == 7
    h.visit(1)
    assert h.step(+1) == 3
    h.visit(4)
    assert h.step(+1) == 7 and h.step(-1) == 3
    h.visit(7)
    assert h.step(+1) == 1
    enabled.remove(4)  # предыдущее окно выключили
    assert h.back() is None
    enabled.remove(7)  # текущее тоже
    assert h.step(+1) == 1 and h.step(-1) == 3
    enabled.clear()
    assert h.step(+1) is None
