"""probes/xplat/fred2_uia.py's platform-free parts (plan A Task 7): reading resource.h and
sorting AutomationIds against it.

Loaded under its package-style name so mutmut can match the mutants to these tests."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "probes.xplat.fred2_uia",
    Path(__file__).resolve().parents[1] / "probes" / "xplat" / "fred2_uia.py")
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)

RESOURCE_H = """//{{NO_DEPENDENCIES}}
#define IDD_SHIP_EDITOR                 176
#define IDC_SHIP_NAME                   1126
#define IDC_SHIP_CLASS                  1127
#define IDC_TWIN                        1127
#define ID_OTHER                        100
#define ID_ALIAS                        1126
#define IDD_ABOUTBOX                    100
#define IDC_NEGATIVE                    -7
#define _APS_NEXT_RESOURCE_VALUE        0x12F
#ifndef APSTUDIO_INVOKED
"""


def ids():
    """Built inside each test: mutmut imports a test file once, so a table built at import
    time would never run a mutant of resource_ids."""
    return probe.resource_ids(RESOURCE_H)


def ctl(aid, name="x"):
    return {"AutomationId": aid, "ControlType": 50000, "Name": name}


def test_resource_ids_maps_numbers_to_every_name_negatives_included():
    assert ids() == {176: ["IDD_SHIP_EDITOR"], 1126: ["IDC_SHIP_NAME", "ID_ALIAS"],
                     1127: ["IDC_SHIP_CLASS", "IDC_TWIN"], 100: ["ID_OTHER", "IDD_ABOUTBOX"],
                     -7: ["IDC_NEGATIVE"]}


def test_each_id_lands_in_its_bucket():
    # A non-numeric id first: skipping it must not end the walk.
    controls = [ctl("TitleBar"), ctl("1126", "Name"), ctl("-1", "Ship name:"), ctl("1", "OK"),
                ctl("1001"), ctl("100", "Exit"), ctl(""), ctl("1127", "Class")]
    verdict, buckets, notes = probe.compare_ids(controls, ids())
    assert buckets == {"fred2": [1126, 1127], "windows": [-1, 1], "combo_child": [1001],
                       "other": [100]}
    assert verdict is False                            # 100 is unexplained
    assert notes[0] == "AutomationId 1126 ('Name'): fred2 IDC_SHIP_NAME"
    assert notes[4] == "AutomationId 100 ('Exit'): other (resource.h: ID_OTHER, IDD_ABOUTBOX)"
    assert notes[5] == "AutomationId 1127 ('Class'): fred2 IDC_SHIP_CLASS, IDC_TWIN"


def test_a_ship_editor_shaped_dialog_is_a_match():
    # Its own controls plus IDC_STATIC labels, OK/Cancel and a dropdown's children (QA CR-2).
    controls = [ctl("1126"), ctl("1127"), ctl("-1"), ctl("-1"), ctl("1"), ctl("2"),
                ctl("1000"), ctl("1001"), ctl("65535")]
    verdict, buckets, _ = probe.compare_ids(controls, ids())
    assert verdict is True
    assert buckets["fred2"] == [1126, 1127] and buckets["other"] == []


def test_windows_ids_alone_are_not_a_match():
    # The first CI run's SDL message box: nothing from FRED2's resources.
    verdict, _, _ = probe.compare_ids([ctl("-2"), ctl("1", "OK")], ids())
    assert verdict is False


def test_no_verdict_without_numeric_ids():
    verdict, buckets, notes = probe.compare_ids([ctl(""), ctl("TitleBar")], ids())
    assert verdict is None
    assert not any(buckets.values())
    assert notes == ["no numeric AutomationId to compare"]


def test_numeric_id_accepts_one_leading_minus_and_ascii_digits_only():
    assert [probe.numeric_id(x) for x in ("100", "-2", "", None, "--2", "2-", "a1", "١٢")] == [
        100, -2, None, None, None, None, None, None]
