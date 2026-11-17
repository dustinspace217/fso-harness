"""probes/xplat/census.py's report reader (plan A QA TA-13): one row per test FILE, including
tests inside a class, and collection errors counted under "?"."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "census", Path(__file__).resolve().parents[1] / "probes" / "xplat" / "census.py")
census = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(census)

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.test_a" name="test_ok"/>
  <testcase classname="tests.test_a" name="test_bad"><failure message="boom">x</failure></testcase>
  <testcase classname="tests.test_a.TestGroup" name="test_in_class"/>
  <testcase classname="tests.test_b" name="test_skip"><skipped message="s"/></testcase>
  <testcase classname="tests.test_b" name="test_err"><error message="ImportError: x"/></testcase>
  <testcase classname="" name="tests/test_c.py"><error message="collection failed"/></testcase>
</testsuite></testsuites>
"""


def test_summarize_keys_by_file_and_counts_every_outcome(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text(JUNIT)
    rows = census.summarize(report)
    assert set(rows) == {"test_a", "test_b", "?"}
    assert {k: rows["test_a"][k] for k in ("passed", "failed")} == {"passed": 2, "failed": 1}
    assert rows["test_a"]["first"] == {"test_bad": "boom"}
    assert (rows["test_b"]["skipped"], rows["test_b"]["errored"]) == (1, 1)
    assert rows["?"]["errored"] == 1
