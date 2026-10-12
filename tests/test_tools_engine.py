from pathlib import Path

import pytest

from fsoctl import docs, frame_stats, model_info, paths
from tests.engine_helpers import MISSION, MODS

pytestmark = pytest.mark.engine


def test_api_docs_generates_then_caches(engine_env, tmp_runtime, tmp_path, monkeypatch):
    _, fso = engine_env
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    user_file = paths.fs2path() / "sexps.html"
    before = user_file.stat().st_mtime_ns if user_file.exists() else None
    first = docs.api_docs(str(fso), MODS)
    second = docs.api_docs(str(fso), MODS)
    assert first["cached"] is False and second["cached"] is True
    assert "--- @class" in Path(first["files"]["scripting.lua"]).read_text()
    for path in first["files"].values():   # every file passed its ending check (QA SF-8)
        assert docs.incomplete_reason(Path(path)) is None, path
    assert (Path(first["dir"]) / docs.MANIFEST).is_file()
    after = user_file.stat().st_mtime_ns if user_file.exists() else None
    assert after == before   # output went to the shadow folder, never into FS2PATH


def test_model_info_reports_a_known_ship(engine_env, tmp_runtime):
    _, fso = engine_env
    r = model_info.model_info(MODS, name_filter="Ulysses", binary=str(fso))
    assert r["complete"] is True, r["exit_info"]   # -pofspew ends with a clean exit (rc 0)
    assert r["count"] >= 1
    m = r["models"][0]
    assert m["total_faces"] > 0 and m["lods"] and m["lods"][0]["faces"] > 0


def test_frame_stats_for_a_mission(engine_env, tmp_runtime):
    _, fso = engine_env
    r = frame_stats.frame_stats(MODS, MISSION, seconds=5, binary=str(fso))
    assert r["frames"] > 10 and 0 < r["p50_ms"] <= r["p95_ms"] <= r["max_ms"]
    # The window (QA SF-10) is a part of the whole run, and the engine quit cleanly.
    assert r["window_method"] == frame_stats.WINDOW_METHOD
    assert r["whole_run"]["frames"] > r["frames"]
    assert r["clean_exit"] is True, r["exit_info"]
