import os

import pytest

from fsoctl import compare
from tests.engine_helpers import MISSION, MODS

STEPS = [{"wait_for": {"kind": "mission_time", "value": 5, "timeout": 120}},
         {"snapshot": "t5"}]
LAUNCH = {"mods": MODS, "mission": MISSION}


@pytest.mark.engine
def test_same_build_twice_differs_only_by_drift(engine_env, tmp_runtime):
    # No fixed timestep: two runs of one build drift a little. Positions and hull may differ;
    # the mission state, the ship list, goals and events should not, this early in SM1-01.
    _, fso = engine_env
    # Drift moves ships on screen too, so the image tolerance matches the 0.2 bound below.
    r = compare.compare(str(fso), str(fso), LAUNCH, STEPS,
                        {"pos": 1e9, "hull": 1e9, "image": 0.2})
    snap = r["snapshots"]["t5"]
    assert snap["state_diffs"] == [] and r["states_identical"] and snap["ships_truncated"] is False
    assert r["identical_within_tolerance"], r["not_identical_because"]
    assert 0 <= snap["image"]["mean_abs_diff"] < 0.2


@pytest.mark.vulkan
def test_opengl_vs_vulkan(engine_env, tmp_runtime):
    if not os.environ.get("VK_ICD_FILENAMES"):
        pytest.skip("set VK_ICD_FILENAMES to your Vulkan driver's ICD file")
    _, fso = engine_env
    r = compare.renderer_compare(str(fso), LAUNCH, STEPS, {"pos": 1e9, "hull": 1e9})
    assert r["renderers"] == ["opengl", "vulkan"]
    snap = r["snapshots"]["t5"]
    assert snap["state_diffs"] == []
    print("opengl vs vulkan MAE", snap["image"]["mean_abs_diff"])
    assert snap["image"]["mean_abs_diff"] < 0.2
