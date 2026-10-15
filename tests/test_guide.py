"""Unit tier for fsoctl/guide.py (the `guide` verb that serves docs/AGENT-GUIDE.md sections)."""
import argparse

import pytest

from fsoctl import guide
from fsoctl.errors import EXIT_NOT_FOUND, EXIT_USAGE, HarnessError

SAMPLE = """# Title

intro line

## Alpha recipe
a1
a2

## Beta things
b1
## Fly a mission
f1
"""


def test_sections_lists_level_two_headings_in_order():
    assert guide.sections(SAMPLE) == ["Alpha recipe", "Beta things", "Fly a mission"]


def test_section_text_runs_to_the_next_heading():
    assert guide.section_text(SAMPLE, "Alpha recipe") == "## Alpha recipe\na1\na2"
    assert guide.section_text(SAMPLE, "Beta things") == "## Beta things\nb1"


def test_section_text_last_section_runs_to_the_end():
    assert guide.section_text(SAMPLE, "Fly a mission") == "## Fly a mission\nf1"


def test_match_exact_any_case_wins_over_substring():
    names = ["Fly", "Fly a mission"]
    assert guide.match(names, "fly") == "Fly"


def test_match_unique_substring():
    assert guide.match(guide.sections(SAMPLE), "beta") == "Beta things"


def test_match_ambiguous_is_a_usage_error_listing_candidates():
    with pytest.raises(HarnessError) as e:
        guide.match(["Fly a mission", "Fly in formation"], "fly")
    assert e.value.code == EXIT_USAGE
    assert "several" in str(e.value) and "Fly in formation" in e.value.hint


def test_match_none_is_a_usage_error_listing_all():
    with pytest.raises(HarnessError) as e:
        guide.match(["Alpha recipe", "Beta things"], "zeta")
    assert e.value.code == EXIT_USAGE
    assert "no section" in str(e.value) and "Alpha recipe; Beta things" in e.value.hint


def test_verb_on_the_real_guide_lists_and_returns_fly_a_mission():
    listed = guide._guide(argparse.Namespace(section=None))
    assert "Fly a mission" in listed["sections"]
    got = guide._guide(argparse.Namespace(section="fly a"))
    assert got["section"] == "Fly a mission"
    assert got["text"].startswith("## Fly a mission") and "examples/pilot/" in got["text"]


def test_verb_reports_a_missing_guide(monkeypatch, tmp_path):
    monkeypatch.setattr(guide, "GUIDE", tmp_path / "nope.md")
    with pytest.raises(HarnessError) as e:
        guide._guide(argparse.Namespace(section=None))
    assert e.value.code == EXIT_NOT_FOUND
