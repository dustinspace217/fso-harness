import os
import struct
from pathlib import Path

import pytest

from fsoctl import vp
from fsoctl.errors import EXIT_NOT_FOUND, EXIT_REFUSED, EXIT_USAGE, HarnessError


def write_vp(path: Path, files: dict[str, bytes],
             raw_names: tuple[tuple[str, bytes], ...] = ()) -> None:
    """Write a Volition VP archive: header, file data, then the directory (nested folders).

    files maps "dir/sub/name" -> bytes; raw_names adds top-level entries with any name (used to
    plant hostile names such as "../../evil").
    """
    data = bytearray()
    entries = []  # (offset, size, name)
    tree: dict = {}
    for p, b in files.items():
        node = tree
        *dirs, name = p.split("/")
        for d in dirs:
            node = node.setdefault(d, {})
        node[name] = b

    def walk(node):
        for name, val in node.items():
            if isinstance(val, dict):
                entries.append((0, 0, name))
                walk(val)
                entries.append((0, 0, ".."))
            else:
                entries.append((16 + len(data), len(val), name))
                data.extend(val)
    walk(tree)
    for name, b in raw_names:
        entries.append((16 + len(data), len(b), name))
        data.extend(b)
    diroff = 16 + len(data)
    with open(path, "wb") as f:
        f.write(struct.pack("<4siii", b"VPVP", 2, diroff, len(entries)))
        f.write(data)
        for off, size, name in entries:
            f.write(struct.pack("<ii32si", off, size, name.encode("latin-1"), 0))


FILES = {"data/tables/a.tbl": b"#Ships\n$Name: X\n#End\n", "data/missions/m.fs2": b"$Name: M\n"}


def test_index_lists_files_with_sizes(tmp_path):
    arc = tmp_path / "t.vp"
    write_vp(arc, FILES)
    idx = {e["path"]: e["size"] for e in vp.read_index(arc)}
    assert idx == {"data/tables/a.tbl": len(FILES["data/tables/a.tbl"]),
                   "data/missions/m.fs2": len(FILES["data/missions/m.fs2"])}


def test_extract_matching_files_only(tmp_path):
    arc = tmp_path / "t.vp"
    write_vp(arc, FILES)
    r = vp.extract(str(arc), "*.tbl", str(tmp_path / "out"))
    assert r["files"] == ["data/tables/a.tbl"]
    out = tmp_path / "out" / "data" / "tables" / "a.tbl"
    assert out.read_bytes() == FILES["data/tables/a.tbl"]
    assert not (tmp_path / "out" / "data" / "missions").exists()


def test_read_file_is_case_insensitive(tmp_path):
    arc = tmp_path / "t.vp"
    write_vp(arc, FILES)
    assert vp.read_file(arc, "DATA/MISSIONS/M.FS2") == FILES["data/missions/m.fs2"]
    with pytest.raises(HarnessError) as e:
        vp.read_file(arc, "data/missions/none.fs2")
    assert e.value.code == EXIT_NOT_FOUND


def test_path_escape_is_refused(tmp_path):
    arc = tmp_path / "t.vp"
    write_vp(arc, FILES, raw_names=(("../../evil", b"x"),))
    with pytest.raises(HarnessError) as e:
        vp.extract(str(arc), "*", str(tmp_path / "out"))
    assert e.value.code == EXIT_REFUSED
    assert not (tmp_path / "evil").exists() and not (tmp_path.parent / "evil").exists()


@pytest.mark.parametrize("damage", ["header", "directory"])
def test_damaged_archive_is_usage_error(tmp_path, damage):
    arc = tmp_path / "t.vp"
    write_vp(arc, FILES)
    raw = arc.read_bytes()
    arc.write_bytes(raw[:8] if damage == "header" else raw[:-20])
    with pytest.raises(HarnessError) as e:
        vp.read_index(arc)
    assert e.value.code == EXIT_USAGE


@pytest.mark.engine
def test_real_retail_archive_lists_ships_table():
    fs2 = os.environ.get("FS2PATH")
    if not fs2 or not (Path(fs2) / "root_fs2.vp").exists():
        pytest.skip("FS2PATH with root_fs2.vp needed")
    names = [f["path"] for f in vp.list_files(str(Path(fs2) / "root_fs2.vp"), "*.tbl")["files"]]
    assert "data/tables/ships.tbl" in names


def _write_raw_entries(path: Path, payload: bytes, entries: list[tuple[int, int, str]]) -> None:
    """Write a VP whose directory holds exactly `entries` (offset, size, name), unchecked."""
    diroff = 16 + len(payload)
    with open(path, "wb") as f:
        f.write(struct.pack("<4siii", b"VPVP", 2, diroff, len(entries)))
        f.write(payload)
        for off, size, name in entries:
            f.write(struct.pack("<ii32si", off, size, name.encode("latin-1"), 0))


@pytest.mark.parametrize("entry", [
    (16, -1, "neg.tbl"),            # negative size: read(-1) would read to end of file
    (-5, 4, "negoff.tbl"),          # negative offset
    (16, 10_000, "past_end.tbl"),   # claims bytes the file does not have
])
def test_entries_outside_the_file_are_rejected(tmp_path, entry):
    # Found by a background security review: signed sizes could slip under the extraction cap.
    arc = tmp_path / "bad.vp"
    _write_raw_entries(arc, b"abcd", [entry])
    with pytest.raises(HarnessError) as e:
        vp.read_index(arc)
    assert e.value.code == EXIT_USAGE


def test_read_file_has_a_size_cap(tmp_path):
    arc = tmp_path / "t.vp"
    write_vp(arc, {"data/missions/big.fs2": b"x" * 2048})
    with pytest.raises(HarnessError) as e:
        vp.read_file(arc, "data/missions/big.fs2", max_bytes=1024)
    assert e.value.code == EXIT_REFUSED
