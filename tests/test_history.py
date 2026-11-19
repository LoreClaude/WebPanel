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
