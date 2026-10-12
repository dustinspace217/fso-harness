"""Group D fix-wave tests: logs, scratch, vp, screenshot names (QA finding ids in comments)."""
import struct
from pathlib import Path

import pytest

from fsoctl import launcher, logs, scratch, session, vp, xinput
from fsoctl.errors import EXIT_REFUSED, EXIT_USAGE, HarnessError
from tests.test_vp import write_vp


def _s():
    """Create a minimal game session record in the test's runtime root."""
    return session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[],
                               mission=None, renderer="opengl", width=1, height=1, switches={},
                               allow_eval=False, extra_args=[])


# ---- CR-16: logs caps the problems list -------------------------------------------------------

def test_log_problems_are_capped_with_a_total(tmp_runtime):
    s = _s()
    lines = [f'WARNING: "w{i}" at x.cpp:{i}' for i in range(60)]
    (session.run_dir(s) / "engine.log").write_text("\n".join(lines) + "\n")
    r = logs.read(s.id, source="engine")
    assert len(r["problems"]) == logs.PROBLEMS_MAX == 50
    assert r["problems_total"] == 60
    assert r["problems"][0]["text"].startswith('WARNING: "w0"')   # the FIRST ones are kept


def test_log_problems_under_the_cap_are_all_kept(tmp_runtime):
    s = _s()
    (session.run_dir(s) / "engine.log").write_text('Error: boom\n')
    r = logs.read(s.id, source="engine")
    assert r["problems_total"] == 1 and len(r["problems"]) == 1


# ---- MU-7: scratch list_all needs the stamp ---------------------------------------------------

def test_scratch_list_skips_a_pattern_named_folder_without_stamp(tmp_path, monkeypatch):
    monkeypatch.setenv("FS2PATH", str(tmp_path))
    (tmp_path / f"{scratch.PREFIX}abcdef").mkdir()        # right name shape, no stamp
    assert scratch.list_all() == {"mods": []}


# ---- MU-2: hostile-archive bounds -------------------------------------------------------------

def _raw(path: Path, payload: bytes, entries: list[tuple[int, int, str]],
         magic: bytes = b"VPVP") -> None:
    """Write a VP with exactly `entries` (offset, size, name) and the given magic, unchecked."""
    with open(path, "wb") as f:
        f.write(struct.pack("<4siii", magic, 2, 16 + len(payload), len(entries)))
        f.write(payload)
        for off, size, name in entries:
            f.write(struct.pack("<ii32si", off, size, name.encode("latin-1"), 0))


def test_vp_bad_magic_with_sane_count_is_refused(tmp_path):
    arc = tmp_path / "t.vp"
    _raw(arc, b"abcd", [(16, 4, "a.tbl")], magic=b"XXXX")
    with pytest.raises(HarnessError) as e:
        vp.read_index(arc)
    assert e.value.code == EXIT_USAGE and "magic" in e.value.message and e.value.hint


def test_vp_empty_archive_is_valid(tmp_path):
    arc = tmp_path / "t.vp"
    _raw(arc, b"", [])
    assert vp.read_index(arc) == []


def test_vp_entry_ending_exactly_at_eof_is_valid(tmp_path):
    # payload sits at 16..20; the directory follows it, so put the entry's data LAST instead:
    # write the archive, then check an entry whose end equals the file size.
    arc = tmp_path / "t.vp"
    entry_size = 4
    dir_size = 1 * 44
    total = 16 + dir_size + entry_size
    with open(arc, "wb") as f:
        f.write(struct.pack("<4siii", b"VPVP", 2, 16, 1))
        f.write(struct.pack("<ii32si", 16 + dir_size, entry_size, b"end.tbl", 0))
        f.write(b"WXYZ")
    assert arc.stat().st_size == total
    [e] = vp.read_index(arc)
    assert e["offset"] + e["size"] == total
    assert vp.read_file(arc, "end.tbl") == b"WXYZ"


def test_vp_entry_one_byte_past_eof_is_refused(tmp_path):
    arc = tmp_path / "t.vp"
    dir_size = 44
    with open(arc, "wb") as f:
        f.write(struct.pack("<4siii", b"VPVP", 2, 16, 1))
        f.write(struct.pack("<ii32si", 16 + dir_size, 5, b"end.tbl", 0))   # claims 5, has 4
        f.write(b"WXYZ")
    with pytest.raises(HarnessError) as e:
        vp.read_index(arc)
    assert e.value.code == EXIT_USAGE and "outside" in e.value.message


# ---- SA-9: vp extract does not overwrite unless asked -----------------------------------------

FILES = {"data/missions/m.fs2": b"$Name: from archive\n"}


def test_vp_extract_refuses_to_overwrite(tmp_path):
    arc = tmp_path / "t.vp"
    write_vp(arc, FILES)
    target = tmp_path / "out" / "data" / "missions" / "m.fs2"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"the user's own mission")
    with pytest.raises(HarnessError) as e:
        vp.extract(str(arc), "*", str(tmp_path / "out"))
    assert e.value.code == EXIT_REFUSED and "overwrite" in e.value.hint
    assert target.read_bytes() == b"the user's own mission"


def test_vp_extract_overwrite_lists_what_it_replaced(tmp_path):
    arc = tmp_path / "t.vp"
    write_vp(arc, {**FILES, "data/tables/a.tbl": b"table"})
    target = tmp_path / "out" / "data" / "missions" / "m.fs2"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    r = vp.extract(str(arc), "*", str(tmp_path / "out"), overwrite=True)
    assert r["overwritten"] == ["data/missions/m.fs2"]
    assert target.read_bytes() == FILES["data/missions/m.fs2"]


def test_vp_extract_copies_in_chunks(tmp_path, monkeypatch):
    # An entry bigger than one chunk arrives whole, and no single read asks for more than a chunk.
    arc = tmp_path / "t.vp"
    big = bytes(range(256)) * 20   # 5120 bytes
    write_vp(arc, {"data/big.bin": big})
    monkeypatch.setattr(vp, "COPY_CHUNK", 1000)
    sizes: list[int] = []
    real_open = open

    class Spy:
        """Wrap a file object and record the size of every read() on it."""
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

        def read(self, n=-1):
            sizes.append(n)
            return self.f.read(n)

        def __getattr__(self, name):
            return getattr(self.f, name)

    def spy_open(path, mode="r", *a, **kw):
        """Spy only on binary reads of the archive."""
        f = real_open(path, mode, *a, **kw)
        return Spy(f) if Path(path) == arc and "r" in mode else f
    monkeypatch.setattr(vp, "open", spy_open, raising=False)
    r = vp.extract(str(arc), "*", str(tmp_path / "out"))
    assert r["overwritten"] == []
    assert (tmp_path / "out" / "data" / "big.bin").read_bytes() == big
    copy_reads = [n for n in sizes if n != -1]
    assert copy_reads and max(copy_reads) <= 1000


# ---- CR-7: concurrent screenshots never share a file name -------------------------------------

def test_two_screenshots_get_distinct_names_even_before_either_is_written(tmp_runtime,
                                                                          monkeypatch):
    s = _s()
    monkeypatch.setattr(launcher, "require_running", lambda sid: s)
    monkeypatch.setattr(xinput, "window_id", lambda sess: "0x1")

    def fake_x(sess, *args):
        """Pretend to be ImageMagick: write nothing for `import`, report a size for identify."""
        return "10 10" if args[0] == "identify" else ""
    monkeypatch.setattr(xinput, "_x", fake_x)
    a = xinput.screenshot(s.id)["path"]
    b = xinput.screenshot(s.id)["path"]
    assert a != b
