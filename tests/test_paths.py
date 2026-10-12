import os
import stat

import pytest

from fsoctl import paths
from fsoctl.errors import EXIT_NOT_FOUND, EXIT_USAGE, HarnessError


def test_runtime_root_uses_override_and_is_private(tmp_runtime):
    root = paths.runtime_root()
    assert root == tmp_runtime
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700


def test_session_dir_rejects_path_tricks(tmp_runtime):
    with pytest.raises(HarnessError) as e:
        paths.session_dir("../escape")
    assert e.value.code == EXIT_NOT_FOUND and e.value.message and e.value.hint
    assert paths.session_dir("s0123abcd").parent == tmp_runtime


def test_fs2path_requires_env(monkeypatch):
    monkeypatch.delenv("FS2PATH", raising=False)
    with pytest.raises(HarnessError) as e:
        paths.fs2path()
    assert e.value.code == EXIT_USAGE
    assert "FS2PATH" in e.value.message and e.value.hint


def test_channel_dir_is_under_first_mod(tmp_path):
    assert paths.channel_dir(tmp_path) == tmp_path / "fso_harness_bridge" / "data" / "cache"


def test_bridge_source_exists_in_package():
    assert (paths.bridge_source() / "data" / "tables").is_dir()
