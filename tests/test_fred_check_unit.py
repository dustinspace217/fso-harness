from pathlib import Path

from fsoctl import fred

# tesseract's reading (2x scaled) of qtFRED's real "Pre-save Error Check" dialog for a copy of
# SM1-01 whose event "Iota 1 Dock" names a missing ship "Ghost 9" (probe results section 10).
OCR = (Path(__file__).parent / "data" / "checker_ocr.txt").read_text()


def test_parse_checker_finds_the_entry():
    r = fred.parse_checker(OCR)
    assert r["count"] == 1
    assert len(r["entries"]) == 1
    e = r["entries"][0]
    assert e["severity"] == "error"
    assert "Iota 1 Dock" in e["text"] and "Invalid ship or wing name" in e["text"]
    assert e["bad_node"] == "Ghost 9"


def test_parse_checker_ignores_legend_and_footer():
    texts = " ".join(e["text"] for e in fred.parse_checker(OCR)["entries"])
    assert "Potential Issue" not in texts and "Fix and Save" not in texts


def test_parse_checker_severity_words():
    text = ("Warning: Ship \"A\" has no arrival cue\n"
            "Potential issue: Event \"B\" never fires\n"
            "Critical Error: no player start\n"
            "3 error(s) found.\n")
    r = fred.parse_checker(text)
    assert [e["severity"] for e in r["entries"]] == ["warning", "potential", "critical"]
    assert r["count"] == 3


def test_parse_checker_without_count_line_reports_none():
    assert fred.parse_checker("garbled text")["count"] is None
