import random

import pytest

from panel6.macros import MacroRunner, describe, normalize_macro, normalize_step
from test_macros import FakeClock, FakeTarget

W, A = {"code": "KeyW", "key": "w", "vk": 87}, {"code": "KeyA", "key": "a", "vk": 65}


def run_random(step, *, seed=1, activity=lambda: False, policy="keep", on_call=None):
    clock = FakeClock()
    target = FakeTarget(clock, on_call=on_call)
    macro = normalize_macro({"name": "случайный", "steps": [dict(step, t="random")]})
    runner = MacroRunner(target, macro, policy=policy, activity=activity, clock=clock.clock, sleep=clock.sleep,
                         rng=random.Random(seed))
    target.runner = runner
    runner.run()
    return runner, target, clock


def test_clicks_every_interval_inside_area():
    runner, t, clock = run_random({"ms": 1000, "min_ms": 50, "max_ms": 50, "kinds": ["click"],
                                   "area": [0.2, 0.2, 0.4, 0.6]})
    presses = [c for c in t.calls if c[2] == "mousePressed"]
    assert len(presses) == 20
    assert all(200 <= c[3] <= 400 and 100 <= c[4] <= 300 for c in presses)  # страница 1000×500
    assert runner.state == "done" and clock.t == pytest.approx(1.0, abs=0.06)


def test_only_selected_kinds_and_keys():
    _, t, _ = run_random({"ms": 2000, "min_ms": 50, "max_ms": 100, "kinds": ["key", "wheel"],
                          "keys": [W, A], "hold_min": 10, "hold_max": 20})
    kinds = {c[1] for c in t.calls}
    assert kinds == {"key", "wheel", "mouse"}
    # мышь только подводится к точке прокрутки — никаких нажатий кнопок
    assert {c[2] for c in t.calls if c[1] == "mouse"} == {"mouseMoved"}
    codes = {c[3] for c in t.calls if c[1] == "key"}
    assert codes == {"KeyW", "KeyA"}
    downs = [c[3] for c in t.calls if c[2] == "keyDown"]
    ups = [c[3] for c in t.calls if c[2] == "keyUp"]
    assert sorted(downs) == sorted(ups)
    wheels = [c[4] for c in t.calls if c[1] == "wheel"]
    assert all(30 <= abs(d) <= 120 for d in wheels)


def test_mouse_buttons_released_at_end_and_in_place_mode():
    runner, t, _ = run_random({"ms": 500, "min_ms": 50, "max_ms": 50, "kinds": ["down"], "area": None,
                               "buttons": ["left", "right"]})
    assert runner.held_buttons == set() and runner.buttons == 0
    assert all(c[2] != "mouseMoved" for c in t.calls), "на месте курсора мышь не двигается"
    pressed = {c[5] for c in t.calls if c[2] == "mousePressed"}
    released = {c[5] for c in t.calls if c[2] == "mouseReleased"}
    assert pressed == released == {"left", "right"}


def test_look_and_jiggle_stay_in_range():
    _, t, _ = run_random({"ms": 1000, "min_ms": 50, "max_ms": 50, "kinds": ["look", "move"], "area": None,
                          "look_dx": 40, "look_dy": 10})
    moves = [c for c in t.calls if c[2] == "mouseMoved"]
    assert moves
    xs, ys = [c[3] for c in moves], [c[4] for c in moves]
    # каждый сдвиг не больше заданного диапазона от предыдущей точки
    prev = (500.0, 250.0)
    for x, y in zip(xs, ys):
        assert abs(x - prev[0]) <= 40.01 and abs(y - prev[1]) <= 10.01
        prev = (x, y)


def test_random_pauses_when_user_acts():
    calls = {"n": 0}

    def activity():
        calls["n"] += 1
        return 5 <= calls["n"] <= 30  # ты вмешался ненадолго

    runner, t, clock = run_random({"ms": 300, "min_ms": 50, "max_ms": 50, "kinds": ["click"]},
                                  activity=activity, policy="pause")
    presses = [c for c in t.calls if c[2] == "mousePressed"]
    assert runner.state == "done"
    assert len(presses) == 6  # время паузы не съедает длительность случайного ввода
    assert clock.t > 0.3


def test_normalize_random():
    s = normalize_step({"t": "random", "kinds": ["key", "text", "click", "click"], "min_ms": 200, "max_ms": 20,
                        "area": [0.9, 0.8, 0.1, 0.2], "hold_min": 300, "hold_max": 100})
    assert s["kinds"] == ["click"]  # клавиш нет — «клавиша» убрана, «текст» не поддерживается
    assert (s["min_ms"], s["max_ms"]) == (20, 200)
    assert s["area"] == [0.1, 0.2, 0.9, 0.8]
    assert (s["hold_min"], s["hold_max"]) == (100, 300)
    s2 = normalize_step({"t": "random", "kinds": [], "keys": [W, W, {"bad": 1}]})
    assert s2["kinds"] == ["click"] and s2["keys"] == [W]
    assert normalize_step({"t": "random", "area": None})["area"] is None
    d = describe(normalize_step({"t": "random", "ms": 5000, "min_ms": 50, "max_ms": 50, "kinds": ["click", "key"],
                                 "keys": [W, A]}))
    assert d == "Случайный ввод 5000 мс, каждые 50 мс: клик, клавиша [W A]"
