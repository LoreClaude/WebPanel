from panel6.recorder import events_to_steps


def ev(e, t, **kw):
    d = {"e": e, "t": t, "locked": False}
    d.update(kw)
    return d


def test_click_and_double_click():
    steps = events_to_steps([
        ev("down", 100, b=0, x=0.5, y=0.5), ev("up", 160, b=0, x=0.5, y=0.5),
        ev("down", 260, b=0, x=0.501, y=0.5), ev("up", 300, b=0, x=0.501, y=0.5),
        ev("down", 2000, b=2, x=0.2, y=0.3), ev("up", 2050, b=2, x=0.2, y=0.3),
    ])
    assert [(s["t"], s.get("count"), s.get("button")) for s in steps] == [("click", 2, "left"), ("click", 1, "right")]
    assert steps[0]["delay"] == 0 and steps[1]["delay"] == 1900


def test_click_with_tiny_jitter_is_still_click():
    steps = events_to_steps([
        ev("down", 0, b=0, x=0.5, y=0.5), ev("move", 40, bs=1, x=0.502, y=0.5, mx=1, my=0),
        ev("up", 80, b=0, x=0.502, y=0.5),
    ])
    assert [s["t"] for s in steps] == ["click"]


def test_drag_keeps_moves():
    steps = events_to_steps([
        ev("down", 0, b=0, x=0.1, y=0.1),
        ev("move", 10, bs=1, x=0.2, y=0.2, mx=5, my=5),
        ev("move", 20, bs=1, x=0.25, y=0.25, mx=5, my=5),   # слишком часто — пропускается
        ev("move", 60, bs=1, x=0.4, y=0.4, mx=5, my=5),
        ev("up", 500, b=0, x=0.5, y=0.5),
    ])
    assert [s["t"] for s in steps] == ["down", "move", "move", "up"]
    assert steps[-1]["x"] == 0.5 and steps[-1]["delay"] == 440


def test_hover_moves_are_ignored():
    steps = events_to_steps([ev("move", t, bs=0, x=0.1 * t / 100, y=0.5, mx=1, my=0) for t in range(0, 500, 20)])
    assert steps == []


def test_key_press_with_hold_time_and_overlap():
    steps = events_to_steps([
        ev("kd", 0, code="KeyW", key="w", vk=87), ev("ku", 1500, code="KeyW", key="w", vk=87),
        ev("kd", 1600, code="ShiftLeft", key="Shift", vk=16), ev("kd", 1650, code="KeyA", key="a", vk=65),
        ev("ku", 1900, code="ShiftLeft", key="Shift", vk=16), ev("ku", 2000, code="KeyA", key="a", vk=65),
    ])
    assert [(s["t"], s["action"], s["code"]) for s in steps] == [
        ("key", "press", "KeyW"), ("key", "down", "ShiftLeft"), ("key", "down", "KeyA"),
        ("key", "up", "ShiftLeft"), ("key", "up", "KeyA")]
    assert steps[0]["ms"] == 1500
    assert steps[1]["delay"] == 100  # после отпускания W


def test_camera_turn_under_pointer_lock():
    events = [ev("move", t, locked=True, bs=0, x=0.5, y=0.5, mx=3, my=-1) for t in range(0, 200, 10)]
    events.insert(5, ev("down", 45, locked=True, b=0, x=0.5, y=0.5))
    events.append(ev("up", 300, locked=True, b=0, x=0.5, y=0.5))
    steps = events_to_steps(events)
    kinds = [s["t"] for s in steps]
    assert kinds[0] == "look" and "down" in kinds and kinds[-1] == "up"
    looks = [s for s in steps if s["t"] == "look"]
    assert sum(s["dx"] for s in looks) == 60 and sum(s["dy"] for s in looks) == -20
    down = next(s for s in steps if s["t"] == "down")
    assert down["x"] is None  # при захвате курсора клик «на месте», без прыжка камеры


def test_stuck_keys_and_buttons_released_at_end():
    steps = events_to_steps([
        ev("kd", 0, code="KeyW", key="w", vk=87), ev("down", 100, b=0, x=0.5, y=0.5),
        ev("wheel", 200, dy=120, x=0.5, y=0.5),
    ])
    assert [(s["t"], s.get("action")) for s in steps][-2:] == [("key", "up"), ("up", None)]


def test_empty():
    assert events_to_steps([]) == []
