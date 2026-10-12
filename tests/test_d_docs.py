"""Group D fix-wave tests for api_docs (QA SF-8), with the engine faked.

The trailer checks run on the real files a release engine wrote (docs/research/scripting.lua and
sexps.html, 2026-10-12); scripting.json has no saved sample, so its check uses a small document
shaped like doc_json.cpp's output (an object with "version" and "elements").
"""
import fcntl
import json
import threading
from pathlib import Path

import pytest

from fsoctl import docs, launcher
from fsoctl.errors import EXIT_ENGINE, HarnessError

RESEARCH = Path(__file__).parent.parent / "docs" / "research"
LUA = (RESEARCH / "scripting.lua").read_bytes()
HTML = (RESEARCH / "sexps.html").read_bytes()
JSON = json.dumps({"version": 2, "actions": [], "elements": []}, indent=2).encode()
GOOD = {"sexps.html": HTML, "scripting.lua": LUA, "scripting.json": JSON}


@pytest.mark.parametrize("name", list(GOOD))
def test_real_output_is_complete(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(GOOD[name])
    assert docs.incomplete_reason(p) is None


@pytest.mark.parametrize("name", list(GOOD))
def test_cut_off_output_is_incomplete(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(GOOD[name][: len(GOOD[name]) // 2])
    assert docs.incomplete_reason(p)


@pytest.mark.parametrize("name", list(GOOD))
def test_empty_output_is_incomplete(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"")
    assert "empty" in docs.incomplete_reason(p)


# ---- the whole verb, engine faked -------------------------------------------------------------

@pytest.fixture
def engine(tmp_path, tmp_runtime, monkeypatch):
    """FS2PATH, a binary to hash, a private cache, and a fake engine that writes the docs."""
    fs2 = tmp_path / "fs2"
    (fs2 / "data").mkdir(parents=True)
    monkeypatch.setenv("FS2PATH", str(fs2))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    exe = tmp_path / "fs2_open"
    exe.write_bytes(b"engine")
    monkeypatch.setattr(docs, "_SETTLE_SEC", 0.01)
    box = {"calls": [], "cwd": None, "on_launch": dict(GOOD), "on_quit": {}}

    def launch(req):
        box["calls"].append("launch")
        box["cwd"] = Path(req.cwd)
        for name, data in box["on_launch"].items():
            (box["cwd"] / name).write_bytes(data)
        return {"id": "s00000001"}

    def quit_(sid, timeout=30.0):
        box["calls"].append("quit")
        for name, data in box["on_quit"].items():
            (box["cwd"] / name).write_bytes(data)
        return {"id": sid, "status": "stopped"}
    monkeypatch.setattr(launcher, "launch", launch)
    monkeypatch.setattr(launcher, "quit", quit_)
    monkeypatch.setattr(launcher, "stop", lambda sid: box["calls"].append("stop"))
    box["exe"] = str(exe)
    return box


def test_generates_then_serves_from_a_matching_manifest(engine):
    first = docs.api_docs(engine["exe"])
    second = docs.api_docs(engine["exe"])
    assert first["cached"] is False and second["cached"] is True
    assert engine["calls"].count("launch") == 1 and "quit" in engine["calls"]
    manifest = json.loads((Path(first["dir"]) / "manifest.json").read_text())
    assert manifest["files"] == {n: len(d) for n, d in GOOD.items()}


def test_files_are_collected_after_the_clean_quit(engine):
    # The engine has only part of scripting.lua on disk until it exits; the copy must be whole.
    engine["on_launch"]["scripting.lua"] = LUA[:1000]
    engine["on_quit"]["scripting.lua"] = LUA
    r = docs.api_docs(engine["exe"])
    assert Path(r["files"]["scripting.lua"]).read_bytes() == LUA
    assert engine["calls"].index("quit") > engine["calls"].index("launch")


def test_incomplete_output_is_an_error_and_is_never_cached(engine):
    engine["on_launch"]["sexps.html"] = HTML[:5000]
    with pytest.raises(HarnessError) as e:
        docs.api_docs(engine["exe"])
    assert e.value.code == EXIT_ENGINE and "sexps.html" in e.value.message and e.value.hint
    assert not docs.cache_dir(engine["exe"], []).exists()   # nothing half-written in the cache


def test_a_cache_whose_sizes_do_not_match_its_manifest_is_regenerated(engine):
    first = docs.api_docs(engine["exe"])
    Path(first["files"]["scripting.lua"]).write_bytes(LUA[:100])   # damaged after caching
    again = docs.api_docs(engine["exe"])
    assert again["cached"] is False and engine["calls"].count("launch") == 2
    assert Path(again["files"]["scripting.lua"]).read_bytes() == LUA


def test_a_cache_without_a_manifest_is_regenerated(engine):
    # A cache written by the old code: three files and no manifest.
    dest = docs.cache_dir(engine["exe"], [])
    dest.mkdir(parents=True)
    for name, data in GOOD.items():
        (dest / name).write_bytes(data)
    assert docs.api_docs(engine["exe"])["cached"] is False


# ---- fix wave 2, Y-10: publication is locked per cache key, rechecked, and cleaned up ----------

def test_waits_for_a_concurrent_generation_and_serves_its_result(engine):
    dest = docs.cache_dir(engine["exe"], [])
    dest.parent.mkdir(parents=True, exist_ok=True)
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(docs.api_docs(engine["exe"])))
    with open(docs._key_lock_path(dest), "a") as lock:   # another fsoctl is generating this key
        fcntl.flock(lock, fcntl.LOCK_EX)
        t.start()
        t.join(0.5)
        assert t.is_alive(), "api_docs did not wait for the other generation"
        dest.mkdir()                       # the other process publishes a valid cache...
        for name, data in GOOD.items():
            (dest / name).write_bytes(data)
        (dest / docs.MANIFEST).write_text(json.dumps({"files": {n: len(d)
                                                                for n, d in GOOD.items()}}))
    t.join(10)                             # ...and released the lock when its file closed
    assert not t.is_alive()
    assert out["cached"] is True and engine["calls"] == []   # rechecked, never launched


def test_a_failed_publication_leaves_no_temp_folder(engine, monkeypatch):
    real_copy = docs.shutil.copy2

    def copy2(src, dst):
        if str(dst).endswith("scripting.json"):
            raise OSError(28, "No space left on device")
        return real_copy(src, dst)
    monkeypatch.setattr(docs.shutil, "copy2", copy2)
    with pytest.raises(OSError):
        docs.api_docs(engine["exe"])
    parent = docs.cache_dir(engine["exe"], []).parent
    assert [p.name for p in parent.iterdir() if ".tmp-" in p.name] == []
