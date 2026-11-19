import pytest

from panel6.macros import (
    MacroRunner, check_blocks, clone_macro, delay_text, describe, drop_short_pauses, jitter_pauses, merge_looks,
    normalize_macro, normalize_step, repeat_for, scale_pauses,
)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeTarget:
    """Записывает вызовы вместо настоящего окна Chrome."""

    def __init__(self, clock, size=(1000.0, 500.0), on_call=None):
        self.calls = []
        self.clock = clock
        self.size = size
        self.on_call = on_call

    def _log(self, *item):
        self.calls.append((round(self.clock.t, 3),) + item)
        if self.on_call:
            self.on_call(self)

    def page_size(self):
        return self.size

    def mouse(self, kind, x, y, button="left", clicks=1, buttons=0, modifiers=0):
        self._log("mouse", kind, round(x, 2), round(y, 2), button, clicks, buttons, modifiers)

    def wheel(self, x, y, dy, modifiers=0):
        self._log("wheel", x, y, dy)

    def key(self, kind, params):
        self._log("key", kind, params.get("code"), params.get("text"), params.get("modifiers", 0))

    def insert_text(self, text):
        self._log("text", text)

    def kinds(self):
        return [c[1:3] for c in self.calls]


def run(steps, *, count=1, gap_ms=0, policy="keep", activity=lambda: False, on_call=None, size=(1000.0, 500.0)):
    clock = FakeClock()
    target = FakeTarget(clock, size=size, on_call=on_call)
    macro = normalize_macro({"name": "т", "steps": steps})
    runner = MacroRunner(target, macro, count=count, gap_ms=gap_ms, policy=policy, activity=activity,
                         clock=clock.clock, sleep=clock.sleep)
    if on_call:
        target.runner = runner
    runner.run()
    return runner, target, clock


def test_click_uses_page_fractions():
    _, t, _ = run([{"t": "click", "x": 0.5, "y": 0.2}])
    assert t.calls == [
        (0.0, "mouse", "mouseMoved", 500.0, 100.0, "none", 1, 0, 0),
        (0.0, "mouse", "mousePressed", 500.0, 100.0, "left", 1, 1, 0),
        (0.0, "mouse", "mouseReleased", 500.0, 100.0, "left", 1, 0, 0),
    ]


def test_double_click_counts_and_right_button():
    _, t, _ = run([{"t": "click", "x": 0.1, "y": 0.1, "button": "right", "count": 2}])
    presses = [c for c in t.calls if c[2] in ("mousePressed", "mouseReleased")]
    assert [(c[2], c[5], c[6], c[7]) for c in presses] == [
        ("mousePressed", "right", 1, 2), ("mouseReleased", "right", 1, 0),
        ("mousePressed", "right", 2, 2), ("mouseReleased", "right", 2, 0),
    ]


def test_click_in_place_does_not_move_cursor():
    _, t, _ = run([{"t": "click", "x": None, "y": None}])
    assert [c[2] for c in t.calls] == ["mousePressed", "mouseReleased"]
    assert t.calls[0][3:5] == (500.0, 250.0)  # центр страницы


def test_delays_and_key_hold_time():
    runner, t, clock = run([
        {"t": "key", "code": "KeyW", "key": "w", "vk": 87, "action": "press", "ms": 300, "delay": 100},
        {"t": "wait", "ms": 1000},
        {"t": "text", "text": "gg", "delay": 50},
    ])
    assert t.calls[0][:4] == (0.1, "key", "keyDown", "KeyW") and t.calls[0][4] == "w"
    assert t.calls[1][:4] == (0.4, "key", "keyUp", "KeyW") and t.calls[1][4] is None
    assert t.calls[2] == (1.45, "text", "gg")
    assert runner.state == "done"


def test_loops_nested():
    _, t, _ = run([
        {"t": "loop", "times": 2},
        {"t": "loop", "times": 3},
        {"t": "click", "x": 0.5, "y": 0.5},
        {"t": "endloop"},
        {"t": "endloop"},
    ])
    assert sum(1 for c in t.calls if c[2] == "mousePressed") == 6


def test_unbalanced_blocks_do_not_hang():
    _, t, _ = run([{"t": "endloop"}, {"t": "loop", "times": 5}, {"t": "click", "x": 0.5, "y": 0.5}])
    assert sum(1 for c in t.calls if c[2] == "mousePressed") == 1
    assert check_blocks(normalize_macro({"steps": [{"t": "endloop"}, {"t": "loop"}]})["steps"]) == [
        "шаг 1: «конец блока» без начала", "шаг 2: блок повтора не закрыт"]


def test_repeat_count_and_gap():
    runner, t, clock = run([{"t": "click", "x": 0.5, "y": 0.5}], count=3, gap_ms=1000)
    press_times = [c[0] for c in t.calls if c[2] == "mousePressed"]
    assert press_times == [0.0, 1.0, 2.0]
    assert runner.iteration == 3


def test_forever_stops_and_releases_held_keys():
    def stop_after_presses(target):
        if sum(1 for c in target.calls if c[2] == "keyDown") >= 4:
            target.runner.stop()

    steps = [
        {"t": "key", "code": "ShiftLeft", "key": "Shift", "vk": 16, "action": "down"},
        {"t": "key", "code": "KeyW", "key": "w", "vk": 87, "action": "press", "ms": 100},
        {"t": "key", "code": "ShiftLeft", "key": "Shift", "vk": 16, "action": "up"},
    ]
    runner, t, _ = run(steps, count=0, on_call=stop_after_presses)
    assert runner.state == "stopped"
    # клавиша W нажата при зажатом Shift — модификатор 8
    w_down = [c for c in t.calls if c[2] == "keyDown" and c[3] == "KeyW"]
    assert w_down and w_down[0][5] == 8
    downs = [c[3] for c in t.calls if c[2] == "keyDown"]
    ups = [c[3] for c in t.calls if c[2] == "keyUp"]
    assert sorted(downs) == sorted(ups), "все нажатые клавиши должны быть отпущены"


def test_mouse_hold_released_on_stop():
    def stop_on_press(target):
        if target.calls[-1][2] == "mousePressed":
            target.runner.stop()

    runner, t, _ = run([{"t": "down", "x": 0.2, "y": 0.2}, {"t": "wait", "ms": 5000}], on_call=stop_on_press)
    assert runner.state == "stopped"
    assert t.calls[-1][2] == "mouseReleased" and t.calls[-1][7] == 0


def test_look_moves_relative_over_time():
    _, t, clock = run([{"t": "look", "dx": 100, "dy": -50, "ms": 100}])
    moves = [c for c in t.calls if c[2] == "mouseMoved"]
    assert len(moves) == 10
    assert moves[-1][3:5] == (600.0, 200.0)  # из центра (500, 250)
    assert moves[0][0] == pytest.approx(0.01) and moves[-1][0] == pytest.approx(0.1)


def test_drag_keeps_button_mask():
    _, t, _ = run([
        {"t": "down", "x": 0.1, "y": 0.1},
        {"t": "move", "x": 0.5, "y": 0.5},
        {"t": "up", "x": 0.9, "y": 0.9},
    ])
    assert [(c[2], c[7]) for c in t.calls] == [
        ("mouseMoved", 0), ("mousePressed", 1), ("mouseMoved", 1), ("mouseMoved", 1), ("mouseReleased", 0)]


def test_user_activity_pauses_then_resumes():
    state = {"n": 0}

    def activity():
        state["n"] += 1
        return state["n"] <= 3  # первые три проверки — пользователь действует

    runner, t, clock = run([{"t": "click", "x": 0.5, "y": 0.5}], policy="pause", activity=activity)
    assert runner.state == "done"
    assert t.calls[1][0] > 0  # клик случился только после паузы


def test_user_activity_stop_policy():
    runner, t, _ = run([{"t": "click", "x": 0.5, "y": 0.5}], policy="stop", activity=lambda: True)
    assert runner.state == "stopped" and t.calls == []
    assert "окне" in runner.reason


def test_error_in_target_reported():
    class Broken(FakeTarget):
        def mouse(self, *a, **k):
            raise RuntimeError("окно закрыто")

    clock = FakeClock()
    macro = normalize_macro({"name": "т", "steps": [{"t": "click", "x": 0.5, "y": 0.5}]})
    runner = MacroRunner(Broken(clock), macro, clock=clock.clock, sleep=clock.sleep)
    runner.run()
    assert runner.state == "error" and "окно закрыто" in runner.reason


def test_normalize_step_defaults_and_errors():
    s = normalize_step({"t": "click", "x": 2, "y": -1, "button": "bogus", "delay": -5})
    assert s == {"t": "click", "delay": 0, "x": 1.0, "y": 0.0, "button": "left", "count": 1}
    assert normalize_step({"t": "click", "x": 0.3})["x"] is None  # только одна координата -> «на месте»
    with pytest.raises(ValueError):
        normalize_step({"t": "fly"})
    with pytest.raises(ValueError):
        normalize_step({"t": "key"})


def test_normalize_macro_drops_bad_steps_and_overrides():
    m = normalize_macro({"name": "A", "steps": [{"t": "wait", "ms": 10}, {"t": "nope"}],
                         "windows": [1, 99, 3, 9, True], "overrides": {"3": {"count": 5}, "99": {"count": 1}},
                         "repeat": {"count": 2, "gap_ms": 100}})
    assert len(m["steps"]) == 1 and m["windows"] == [1, 3, 9] and set(m["overrides"]) == {"3"}
    assert repeat_for(m, 3) == (5, 100) and repeat_for(m, 1) == (2, 100)


def test_describe_and_clone():
    assert describe(normalize_step({"t": "key", "code": "KeyW", "key": "w", "ms": 120})) == "Клавиша W: нажать (120 мс)"
    assert describe(normalize_step({"t": "click", "x": 0.5, "y": 0.25})) == "Клик ЛКМ в (50.0%, 25.0%)"
    assert describe(normalize_step({"t": "look", "dx": 150, "dy": -20, "ms": 200})) == \
        "Поворот камеры: x +150, y -20 за 200 мс"
    m = normalize_macro({"name": "A", "hotkey": "Ctrl+1"})
    c = clone_macro(m)
    assert c["id"] != m["id"] and c["hotkey"] == "" and c["name"] == "A (копия)"


def test_endless_instant_macro_is_throttled():
    def stop_after(target):
        if sum(1 for c in target.calls if c[2] == "keyDown") >= 10:
            target.runner.stop()

    runner, t, clock = run([{"t": "key", "code": "KeyE", "key": "e", "vk": 69, "action": "press", "ms": 0}],
                           count=0, on_call=stop_after)
    downs = [c[0] for c in t.calls if c[2] == "keyDown"]
    assert len(downs) == 10 and downs[-1] >= 0.18  # не быстрее 50 повторов в секунду


def test_start_from_step_only_first_pass():
    clock = FakeClock()
    target = FakeTarget(clock)
    macro = normalize_macro({"name": "т", "steps": [{"t": "click", "x": 0.1, "y": 0.1},
                                                    {"t": "click", "x": 0.2, "y": 0.2},
                                                    {"t": "click", "x": 0.3, "y": 0.3}]})
    runner = MacroRunner(target, macro, count=2, start=2, clock=clock.clock, sleep=clock.sleep)
    assert runner.status()["index"] == 2  # ещё до старта видно, с какого шага пойдёт
    runner.run()
    xs = [c[3] for c in target.calls if c[2] == "mousePressed"]
    assert xs == [300.0, 100.0, 200.0, 300.0]  # первый проход с шага 3, второй — с начала


def test_status_follows_long_wait():
    clock = FakeClock()
    target = FakeTarget(clock)
    seen = []
    macro = normalize_macro({"name": "т", "steps": [{"t": "click", "x": 0.5, "y": 0.5},
                                                    {"t": "wait", "ms": 2000}]})
    runner = MacroRunner(target, macro, clock=clock.clock, sleep=clock.sleep,
                         on_status=lambda st: seen.append((round(clock.t, 2), st["state"], st["index"])))
    runner.run()
    during_wait = [i for t, state, i in seen if state == "running" and 0.2 <= t <= 1.9]
    assert during_wait and set(during_wait) == {1}  # всю паузу подсвечен шаг «Пауза»
    assert seen[-1][1:] == ("done", -1)


def test_scale_pauses():
    steps = [normalize_step({"t": "click", "x": 0.5, "y": 0.5, "delay": 300}),
             normalize_step({"t": "wait", "ms": 1000, "delay": 0}),
             normalize_step({"t": "key", "code": "KeyW", "key": "w", "ms": 400})]
    out, changed = scale_pauses(steps, 0.5)
    assert [s["delay"] for s in out] == [150, 0, 0] and out[1]["ms"] == 500
    assert out[2]["ms"] == 400 and changed == 2  # длительность нажатия клавиши не трогаем
    assert steps[0]["delay"] == 300  # исходные шаги не меняются


def test_drop_short_pauses_keeps_long_pause_of_removed_wait():
    steps = [normalize_step(s) for s in (
        {"t": "click", "x": 0.5, "y": 0.5, "delay": 30},
        {"t": "wait", "ms": 20, "delay": 500},
        {"t": "click", "x": 0.5, "y": 0.5, "delay": 10},
        {"t": "wait", "ms": 900},
    )]
    out, changed = drop_short_pauses(steps, 50)
    assert [(s["t"], s["delay"]) for s in out] == [("click", 0), ("click", 500), ("wait", 0)]
    assert out[2]["ms"] == 900 and changed == 3


def test_merge_looks_same_direction_only():
    steps = [normalize_step(s) for s in (
        {"t": "look", "dx": 10, "dy": 1, "ms": 50},
        {"t": "look", "dx": 12, "dy": 0, "ms": 50, "delay": 5},
        {"t": "look", "dx": 8, "dy": 2, "ms": 40, "delay": 10},
        {"t": "look", "dx": -5, "dy": 0, "ms": 50, "delay": 5},     # повернул обратно — отдельный шаг
        {"t": "key", "code": "KeyW", "key": "w", "ms": 100},
        {"t": "look", "dx": 3, "dy": 0, "ms": 50, "delay": 500},   # долгая пауза — тоже отдельно
    )]
    out, merged = merge_looks(steps)
    assert merged == 2 and [s["t"] for s in out] == ["look", "look", "key", "look"]
    assert (out[0]["dx"], out[0]["dy"], out[0]["ms"]) == (30.0, 3.0, 155)


def test_delay_range_normalize_and_text():
    s = normalize_step({"t": "wait", "ms": 0, "delay": 300, "delay_max": 100})
    assert (s["delay"], s["delay_max"]) == (100, 300) and delay_text(s) == "100–300 мс"  # границы выправлены
    same = normalize_step({"t": "wait", "ms": 0, "delay": 200, "delay_max": 200})
    assert "delay_max" not in same and delay_text(same) == "200 мс"  # равные границы — обычная пауза
    assert "delay_max" not in normalize_step({"t": "wait", "ms": 0, "delay": 50})


def test_random_delay_picked_each_time():
    import random

    clock = FakeClock()
    target = FakeTarget(clock)
    macro = normalize_macro({"name": "т", "steps": [{"t": "click", "x": 0.5, "y": 0.5, "delay": 100,
                                                     "delay_max": 300}]})
    runner = MacroRunner(target, macro, count=30, clock=clock.clock, sleep=clock.sleep, rng=random.Random(7))
    runner.run()
    times = [c[0] for c in target.calls if c[2] == "mousePressed"]
    gaps = [round(b - a, 3) for a, b in zip([0.0] + times, times)]
    assert len(gaps) == 30 and all(0.1 <= g <= 0.3 + 1e-9 for g in gaps)
    assert len(set(gaps)) > 10  # каждый раз своя пауза


def test_cleanup_keeps_delay_ranges():
    steps = [normalize_step(s) for s in (
        {"t": "click", "x": 0.5, "y": 0.5, "delay": 100, "delay_max": 300},
        {"t": "click", "x": 0.5, "y": 0.5, "delay": 10, "delay_max": 40},
        {"t": "look", "dx": 5, "dy": 0, "ms": 50},
        {"t": "look", "dx": 5, "dy": 0, "ms": 50, "delay": 0, "delay_max": 30},
    )]
    scaled, _ = scale_pauses(steps, 2)
    assert (scaled[0]["delay"], scaled[0]["delay_max"]) == (200, 600)
    dropped, _ = drop_short_pauses(steps, 50)
    assert (dropped[0]["delay"], dropped[0]["delay_max"]) == (100, 300)  # интервал длиннее 50 мс остался
    assert dropped[1]["delay"] == 0 and "delay_max" not in dropped[1]    # весь интервал короче — убран
    merged, n = merge_looks(steps)
    assert n == 0  # между поворотами случайная пауза — не склеиваем


def test_jitter_pauses():
    steps = [normalize_step(s) for s in (
        {"t": "click", "x": 0.5, "y": 0.5, "delay": 200},
        {"t": "click", "x": 0.5, "y": 0.5},
        {"t": "click", "x": 0.5, "y": 0.5, "delay": 100, "delay_max": 300},
    )]
    out, changed = jitter_pauses(steps, 50)
    assert (out[0]["delay"], out[0]["delay_max"]) == (100, 300)
    assert out[1] == steps[1]  # без паузы — без изменений
    assert (out[2]["delay"], out[2]["delay_max"]) == (100, 300) and changed == 1  # середина 200, ±50 % — то же
    out, _ = jitter_pauses(steps, 10)
    assert (out[2]["delay"], out[2]["delay_max"]) == (180, 220)
