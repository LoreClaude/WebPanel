import json

import pytest

from panel6.presets import DEFAULT_NAME, PresetStore, file_name, normalize_preset


def macro(name, mid=None):
    return {"id": mid, "name": name, "steps": [{"t": "wait", "ms": 10}]}


def test_default_created_once(tmp_path):
    store = PresetStore(tmp_path)
    assert store.ensure_default() == DEFAULT_NAME
    assert store.ensure_default() == DEFAULT_NAME
    assert store.names() == [DEFAULT_NAME]


def test_save_load_roundtrip_and_window_macros(tmp_path):
    store = PresetStore(tmp_path)
    p = normalize_preset({"name": "Фарм", "macros": [macro("A", "m_1"), macro("B", "m_2")],
                          "window_macros": {"1": "m_2", "2": "m_missing"}})
    store.save(p)
    loaded = store.load("Фарм")
    assert [m["name"] for m in loaded["macros"]] == ["A", "B"]
    assert loaded["window_macros"] == {"1": "m_2", "2": "", "3": "", "4": "", "5": ""}


def test_duplicate_ids_are_fixed(tmp_path):
    p = normalize_preset({"macros": [macro("A", "m_1"), macro("B", "m_1")]})
    assert len({m["id"] for m in p["macros"]}) == 2


def test_create_copy_rename_delete(tmp_path):
    store = PresetStore(tmp_path)
    store.ensure_default()
    base = store.load(DEFAULT_NAME)
    base["macros"] = [macro("A", "m_1")]
    store.save(base)
    copy_name = store.create(DEFAULT_NAME, copy_from=store.load(DEFAULT_NAME))
    assert copy_name == f"{DEFAULT_NAME} (2)"
    assert store.load(copy_name)["macros"][0]["name"] == "A"
    renamed = store.rename(copy_name, "Событие")
    assert renamed == "Событие" and set(store.names()) == {DEFAULT_NAME, "Событие"}
    assert store.rename("Событие", "событие") == "событие"
    assert set(store.names()) == {DEFAULT_NAME, "событие"}
    store.delete("событие")
    assert store.names() == [DEFAULT_NAME]


def test_rename_to_existing_gets_suffix(tmp_path):
    store = PresetStore(tmp_path)
    store.create("A")
    store.create("B")
    assert store.rename("B", "A") == "A (2)"


def test_file_name_sanitized():
    assert file_name('a/b:c*?"<>|') == "a_b_c______.json"
    assert file_name("   ") == "набор.json"


def test_export_import(tmp_path):
    store = PresetStore(tmp_path / "s")
    store.save({"name": "Фарм", "macros": [macro("A", "m_1")]})
    dest = tmp_path / "out.json"
    store.export("Фарм", dest)
    assert json.loads(dest.read_text(encoding="utf-8"))["name"] == "Фарм"
    assert store.import_file(dest) == "Фарм (2)"
    bad = tmp_path / "bad.json"
    bad.write_text('{"macros": 5}', encoding="utf-8")
    with pytest.raises(ValueError):
        store.import_file(bad)
