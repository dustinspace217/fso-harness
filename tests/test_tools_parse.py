from pathlib import Path

import pytest

from fsoctl import frame_stats, model_info
from fsoctl.errors import EXIT_ENGINE, HarnessError

DATA = Path(__file__).parent / "data"
PROFILE = (DATA / "profiling_sample.csv").read_text()   # real -profile_write_file output
POFSPEW = (DATA / "pofspew_sample.txt").read_text()     # real -pofspew output, first 3 models


def test_frame_stats_parse_real_profile():
    s = frame_stats.parse(PROFILE)
    assert s["frames"] == 311
    assert s["max_ms"] == pytest.approx(508.48, abs=0.01)     # the loading frame
    assert s["p50_ms"] == pytest.approx(42.693, abs=0.01)       # median of this real sample
    assert s["p50_ms"] <= s["p95_ms"] <= s["p99_ms"] <= s["max_ms"]


def test_frame_stats_skip_drops_loading_frames():
    assert frame_stats.parse(PROFILE, skip=5)["frames"] == 306
    assert frame_stats.parse(PROFILE, skip=5)["max_ms"] < 508


def test_frame_stats_empty_file_is_engine_error():
    with pytest.raises(HarnessError) as e:
        frame_stats.parse("")
    assert e.value.code == EXIT_ENGINE


def test_model_info_parse_real_pofspew():
    models = model_info.parse(POFSPEW)
    assert [m["name"] for m in models] == ["Asteroid-BlueLarge.pof", "Asteroid-BlueMedium.pof",
                                           models[2]["name"]]
    first = models[0]
    assert first["total_faces"] == 8906
    assert [(lod["lod"], lod["faces"], lod["destroyed"]) for lod in first["lods"]] == [
        (0, 6948, 0), (1, 1738, 0), (2, 220, 0)]
