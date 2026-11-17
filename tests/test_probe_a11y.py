"""probes/xplat/a11y_probe.py's platform-free parts (plan A Task 6): object-name matching and the
shared drive() sequence, run against a fake reader. The real readers run on Linux by hand and on
the Windows and macOS CI runners.

Loaded under its package-style name so mutmut can match the mutants to these tests."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "probes.xplat.a11y_probe",
    Path(__file__).resolve().parents[1] / "probes" / "xplat" / "a11y_probe.py")
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    monkeypatch.setattr(probe, "SETTLE_S", 0)
    monkeypatch.setattr(probe, "POLL_S", 0)


def test_id_matches_whole_ids_and_the_last_dotted_part_only():
    assert probe.id_matches("shipNameEdit", "shipNameEdit")
    assert probe.id_matches("QApplication.standinDialog.shipNameEdit", "shipNameEdit")
    assert not probe.id_matches("QApplication.standinDialog.shipNameEditX", "shipNameEdit")
    assert not probe.id_matches("myshipNameEdit", "shipNameEdit")
    assert not probe.id_matches(None, "shipNameEdit")
    assert not probe.id_matches("", "shipNameEdit")


def fake_reader(methods=("works",), tree=probe.TREE_NAMES):
    """A reader over an in-memory 'dialog'. Each select method is named for what it does:
    "works" changes the combo, "quiet" returns True and changes nothing (what all three real
    runs saw), "raises" fails."""
    state = {"text": "", "combo": "GTF Ulysses", "pressed": False, "tried": []}
    ids = {"edit": "App.dlg.shipNameEdit", "combo": "App.dlg.shipClassCombo",
           "tree": "App.dlg.eventTree", "ok": "App.dlg.okButton"}

    def check(pid, node=None, expect=None):
        # A strict fake: the probe must hand back the pid and the node it was given.
        assert pid == 1234, pid
        assert expect is None or node.h == expect, node

    def nodes(pid):
        check(pid)
        return [SimpleNamespace(h=k, name=k, ident=v) for k, v in ids.items()] + [
            SimpleNamespace(h=None, name=n, ident="App.dlg.eventTree") for n in tree]

    def method(kind):
        def run(pid, combo, item):
            check(pid, combo, "combo")
            state["tried"].append(kind)
            if kind == "raises":
                raise RuntimeError("no Selection interface")
            if kind == "works":
                state["combo"] = item
            return True
        return kind, run

    def press(node):
        check(1234, node, "ok")
        state["pressed"] = True
        return True

    def set_text(node, text):
        check(1234, node, "edit")
        state["text"] = text

    def read_text(node):
        check(1234, node, "edit")
        return state["text"]

    def combo_text(pid):
        check(pid)
        return state["combo"]

    return state, SimpleNamespace(
        id_property="accessible-id", nodes=nodes, press=press,
        select_methods=[method(k) for k in methods], combo_text=combo_text,
        set_text=set_text, read_text=read_text)


def test_drive_reports_every_step_when_the_layer_works():
    state, reader = fake_reader()
    r = probe.new_result()
    probe.drive(reader, 1234, r)
    assert (r["object_name_exposed"], r["set_text_ok"], r["combo_select_ok"],
            r["tree_walk_ok"], r["press_ok"]) == (True, True, True, True, True)
    assert r["object_name_value"] == "App.dlg.shipNameEdit"
    assert r["object_name_property"] == "accessible-id"
    assert r["select_method"] == "works"
    assert (state["text"], state["combo"], state["pressed"]) == (
        probe.NEW_NAME, probe.NEW_CLASS, True)


def test_a_method_that_returns_but_changes_nothing_is_a_false_with_its_return_noted():
    _, reader = fake_reader(methods=("quiet",))
    r = probe.new_result()
    probe.drive(reader, 1234, r)
    assert r["combo_select_ok"] is False and r["select_method"] is None
    assert "select (quiet) returned True; combo shows 'GTF Ulysses'" in r["notes"]


def test_methods_are_tried_in_order_until_one_works():
    state, reader = fake_reader(methods=("raises", "quiet", "works", "never"))
    r = probe.new_result()
    probe.drive(reader, 1234, r)
    assert state["tried"] == ["raises", "quiet", "works"]
    assert r["select_method"] == "works"
    assert any(n.startswith("select (raises): RuntimeError: no Selection interface")
               for n in r["notes"])
    assert r["press_ok"]                               # later steps still ran


def test_the_tree_needs_both_items():
    _, reader = fake_reader(tree=("Event 1",))
    r = probe.new_result()
    probe.drive(reader, 1234, r)
    assert r["tree_walk_ok"] is False


def test_first_walk_waits_for_the_tree_to_fill_in():
    _, reader = fake_reader()
    full, calls = reader.nodes, []
    reader.nodes = lambda pid: calls.append(pid) or ([] if len(calls) < 3 else full(pid))
    assert probe.find_by_id(probe.first_walk(reader, 1234, []), probe.EDIT) is not None
    assert len(calls) == 3


def test_drive_without_the_widgets_reports_nothing_found(monkeypatch):
    monkeypatch.setattr(probe, "READY_WAIT_S", 0.05)
    _, reader = fake_reader()
    reader.nodes = lambda _pid: []
    r = probe.new_result()
    probe.drive(reader, 1234, r)
    assert not any(r[k] for k in ("object_name_exposed", "set_text_ok", "combo_select_ok",
                                  "tree_walk_ok", "press_ok"))
    assert "0 accessible elements under the stand-in" in r["notes"]


def test_judge_values_compares_what_qt_saw_with_what_was_set():
    r = probe.new_result()
    probe.judge_values({"ship_name": probe.NEW_NAME, "ship_class": "GTF Ulysses"}, r)
    assert (r["qt_saw_name"], r["qt_saw_class"]) == (True, False)
    r = probe.new_result()
    probe.judge_values(None, r)                        # values.json could not be read
    assert (r["qt_saw_name"], r["qt_saw_class"], r["values_after_ok"]) == (False, False, None)


def test_every_failing_step_is_noted_under_its_own_name():
    state, reader = fake_reader(methods=("works",))
    first = reader.nodes

    def broken(*_args):
        raise RuntimeError("layer said no")

    calls = []
    # The first walk succeeds (so the widgets are found); every later call fails.
    reader.nodes = lambda pid: calls.append(1) or (first(pid) if len(calls) == 1 else broken())
    reader.set_text = reader.read_text = reader.press = reader.combo_text = broken
    reader.select_methods = [("m", broken)]
    r = probe.new_result()
    probe.drive(reader, 1234, r)
    for label in ("set text", "read text", "select (m)", "combo text", "rewalk", "press"):
        assert f"{label}: RuntimeError: layer said no" in r["notes"], label
    assert not any(r[k] for k in ("set_text_ok", "combo_select_ok", "tree_walk_ok", "press_ok"))
    assert "ids found: shipNameEdit=yes, shipClassCombo=yes, eventTree=yes, okButton=yes" in (
        r["notes"])
    assert state["pressed"] is False


def test_ids_found_names_the_missing_widgets():
    _, reader = fake_reader()
    full = reader.nodes
    reader.nodes = lambda pid: [n for n in full(pid)
                                if not n.ident.endswith((".eventTree", ".okButton"))]
    r = probe.new_result()
    probe.drive(reader, 1234, r)
    assert "ids found: shipNameEdit=yes, shipClassCombo=yes, eventTree=no, okButton=no" in (
        r["notes"])


def test_first_walk_gives_up_with_an_empty_list(monkeypatch):
    monkeypatch.setattr(probe, "READY_WAIT_S", 0)
    _, reader = fake_reader()
    assert probe.first_walk(reader, 1234, []) == []


def test_the_result_carries_exactly_the_planned_fields():
    r = probe.new_result()
    assert set(r) == {"os", "qt_version", "object_name_exposed", "object_name_property",
                      "object_name_value", "set_text_ok", "combo_select_ok", "select_method",
                      "tree_walk_ok", "press_ok", "values_after_ok", "qt_saw_name",
                      "qt_saw_class", "needs_permission", "notes"}
    assert not any(r[k] for k in r if k != "os")


def test_judge_values_sees_a_class_qt_took():
    values = {"ship_name": "x", "ship_class": probe.NEW_CLASS}
    r = probe.new_result()
    probe.judge_values(values, r)
    assert (r["qt_saw_name"], r["qt_saw_class"], r["values_after_ok"]) == (False, True, values)


def test_a_failing_first_walk_is_noted_and_retried():
    _, reader = fake_reader()
    full, calls = reader.nodes, []

    def flaky(pid):
        calls.append(pid)
        if len(calls) == 1:
            raise RuntimeError("bus not ready")
        return full(pid)

    reader.nodes = flaky
    r = probe.new_result()
    probe.drive(reader, 1234, r)
    assert "walk: RuntimeError: bus not ready" in r["notes"]
    assert r["object_name_exposed"] is True
