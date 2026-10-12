from pathlib import Path

import pytest

from fsoctl import logs
from fsoctl.errors import EXIT_USAGE, HarnessError

# Real lines from debug engine runs on 2026-10-12 (WARNING/Assert/Error and the look-alike noise),
# plus a LUA ERROR line in the format osapi/dialogs.cpp builds.
SAMPLE = (Path(__file__).parent / "data" / "fs2_open_sample.log").read_text().splitlines()


def test_problems_classifies_each_real_format():
    kinds = [p["kind"] for p in logs.problems(SAMPLE)]
    assert kinds == ["warning", "lua", "assert", "error", "warning"]


def test_multi_line_warning_is_joined():
    # A table warning's quoted text holds a newline (real debug-engine output, 2026-10-12).
    last = logs.problems(SAMPLE)[-1]
    assert "No Such Laser" in last["text"] and last["text"].startswith('WARNING: "zzz-shp.tbm')


def test_look_alike_noise_is_not_a_problem():
    texts = " | ".join(p["text"] for p in logs.problems(SAMPLE))
    assert "shadowed" not in texts and "clipEnabled" not in texts and "PXO" not in texts


def test_problems_carry_line_numbers():
    first = logs.problems(SAMPLE)[0]
    assert first["line"] == 3 and "PSPEW" in first["text"]


def test_filter_and_tail():
    assert logs.filter_lines(SAMPLE, tail=3, grep=None) == SAMPLE[-3:]
    assert logs.filter_lines(SAMPLE, tail=100, grep=r"Mission loaded") == [SAMPLE[-1]]


def test_bad_regex():
    with pytest.raises(HarnessError) as e:
        logs.filter_lines(SAMPLE, tail=10, grep="(")
    assert e.value.code == EXIT_USAGE
