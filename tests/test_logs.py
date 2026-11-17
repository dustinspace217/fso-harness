from pathlib import Path

import pytest

from fsoctl import logs, session, validate
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


# ---- CR2-2: the streaming scan read() uses gives what problems()/filter_lines() give ------------

def _text_cases():
    """Logs that stress the streaming split: the real sample, CR LF endings, a warning cut across
    chunks, problems past PROBLEMS_MAX, a lone CR, odd bytes, a missing final newline."""
    sample = "\n".join(SAMPLE) + "\n"
    many = "".join(f'WARNING: "w{i} spans\nmore\nlines" at x.cpp:{i}\nError: e{i}\n'
                   for i in range(40))
    return [sample, sample.replace("\n", "\r\n"), many, many.replace("\n", "\r\n"),
            'WARNING: "never closed\n' + "\n".join("x" * 7 for _ in range(9)),
            "a\rb\r\nc\n\nError: tail without newline", "Error: \udcff bad byte\n"]


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 64, 1 << 20])
@pytest.mark.parametrize("case", range(7))
def test_streaming_scan_equals_the_whole_text_scan(tmp_path, monkeypatch, chunk, case):
    text = _text_cases()[case]
    raw = text.encode("utf-8", errors="surrogateescape")
    path = tmp_path / "log"
    path.write_bytes(raw)
    monkeypatch.setattr(logs, "_CHUNK", chunk)
    want_lines = raw.decode(errors="replace").splitlines()
    with open(path, "rb") as f:
        got_lines = list(logs._lines(f, len(raw)))
    assert got_lines == want_lines
    found = logs.problems(want_lines)
    for keep, pat in ((3, None), (5000, None), (4, logs.re.compile("Error"))):
        assert logs.scan(iter(want_lines), keep, pat) == (
            logs.filter_lines(want_lines, keep, pat.pattern if pat else None),
            found[:logs.PROBLEMS_MAX], len(found))


def test_a_line_longer_than_the_cap_is_split_not_held(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "_CHUNK", 4)
    monkeypatch.setattr(logs, "_LINE_MAX", 10)
    path = tmp_path / "log"
    path.write_bytes(b"x" * 40)
    with open(path, "rb") as f:
        got = list(logs._lines(f, 40))
    assert "".join(got) == "x" * 40 and max(len(x) for x in got) <= 14


def _log_session(tmp_runtime, text: str):
    s = session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[],
                            mission=None, renderer="opengl", width=320, height=240, switches={},
                            allow_eval=False, extra_args=[])
    (session.run_dir(s) / "engine.log").write_text(text)
    return s


def test_read_counts_problems_over_the_whole_log(tmp_runtime, monkeypatch):
    monkeypatch.setattr(logs, "_CHUNK", 64)                # many chunks
    s = _log_session(tmp_runtime, "".join(f"Error: e{i}\nnoise\n" for i in range(300)))
    out = logs.read(s.id, source="engine", tail=2)
    assert out["problems_total"] == 300 and len(out["problems"]) == logs.PROBLEMS_MAX
    assert out["lines"] == ["Error: e299", "noise"] and "truncated_to_last_bytes" not in out


def test_read_past_the_hard_limit_says_it_counted_only_the_end(tmp_runtime, monkeypatch):
    monkeypatch.setattr(logs, "_READ_MAX", 100)
    s = _log_session(tmp_runtime, "".join(f"Error: e{i:03d}\n" for i in range(300)))
    out = logs.read(s.id, source="engine")
    assert out["truncated_to_last_bytes"] == 100
    assert out["problems_total"] < 300 and out["lines"][-1] == "Error: e299"
    assert out["lines"][0].startswith("Error: e")         # the cut first line was dropped
    assert validate._findings(s.id)["log_truncated_to_last_bytes"] == 100
