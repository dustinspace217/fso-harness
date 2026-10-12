"""Shared pytest fixtures.

tmp_runtime: every unit test that touches sessions gets its own private runtime root and state
folder, so tests never see each other's sessions or the user's real ones, and never write the
real audit log.
fresh_scope_cache (autouse): launcher's per-process scope cache starts empty in every test.
engine_env: engine-tier tests run only when the real game folder and engine binary are configured.
"""
import os
from pathlib import Path

import pytest

from fsoctl import launcher


@pytest.fixture
def tmp_runtime(tmp_path, monkeypatch):
    """Point the harness runtime root at a private temp dir for one test; return that dir.

    Also points the persistent state folder (the audit log) at tmp_path/"state", so no unit test
    appends to the user's real ~/.local/state/fso-harness/audit.log.
    """
    root = tmp_path / "rt"
    monkeypatch.setenv("FSO_HARNESS_RUNTIME", str(root))
    monkeypatch.setenv("FSO_HARNESS_STATE", str(tmp_path / "state"))
    return root


@pytest.fixture(autouse=True)
def fresh_scope_cache():
    """Clear launcher's in-process scope cache before every test (QA TA2-10), so one test's
    cached "scope active" answer can never reach another test."""
    launcher._scope_seen.clear()


@pytest.fixture
def engine_env():
    """Return (fs2path, fso_bin) or skip when the real engine isn't configured."""
    fs2 = os.environ.get("FS2PATH")
    fso = os.environ.get("FSO_BIN")
    if not fs2 or not fso or not Path(fso).exists() or not Path(fs2).is_dir():
        pytest.skip("FS2PATH and FSO_BIN must point at a game dir and an engine binary")
    return Path(fs2), Path(fso)
