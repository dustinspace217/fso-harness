import json

import pytest

from fsoctl import session, shadowdir
from fsoctl.errors import EXIT_REFUSED, HarnessError


def _fake_fs2(tmp_path):
    fs2 = tmp_path / "FS2"
    (fs2 / "data").mkdir(parents=True)
    (fs2 / "MVPS").mkdir()
    (fs2 / "root_fs2.vp").write_bytes(b"VPVP")
    (fs2 / "sexps.html").write_text("user's old file")
    return fs2


@pytest.mark.symlinks
def test_make_links_folders_and_vp_files_only(tmp_path):
    fs2 = _fake_fs2(tmp_path)
    sh = shadowdir.make(fs2, tmp_path / "shadow")
    assert sorted(p.name for p in sh.iterdir()) == ["MVPS", "data", "fso_harness_bridge",
                                                     "root_fs2.vp"]
    assert all((sh / n).is_symlink() for n in ("MVPS", "data", "root_fs2.vp"))
    # The bridge link exists before the bridge is installed (launch installs it later).
    assert (sh / "fso_harness_bridge").is_symlink() and not (sh / "fso_harness_bridge").exists()


@pytest.mark.symlinks
def test_engine_output_lands_in_the_shadow_not_fs2path(tmp_path):
    # A loose file is never linked, so an engine writing sexps.html in its working folder
    # cannot follow a link into the user's game folder (probe results section 0).
    fs2 = _fake_fs2(tmp_path)
    sh = shadowdir.make(fs2, tmp_path / "shadow")
    (sh / "sexps.html").write_text("new docs")
    assert (fs2 / "sexps.html").read_text() == "user's old file"
    assert shadowdir.collect(sh, ["sexps.html", "missing.txt"]) == {"sexps.html": sh / "sexps.html"}


def test_old_session_files_without_cwd_still_load(tmp_runtime):
    s = session.new_session(kind="game", binary="/b", fs2path="/g", mods=[], mission=None,
                            renderer="opengl", width=1, height=1, switches={}, allow_eval=False,
                            extra_args=[])
    p = session.run_dir(s) / "session.json"
    d = json.loads(p.read_text())
    d.pop("cwd", None)
    p.write_text(json.dumps(d))
    assert session.load(s.id).cwd is None


def test_an_account_that_may_not_create_links_gets_a_clear_refusal(tmp_path, monkeypatch):
    # Windows without Developer Mode: os.symlink fails with error 1314 ("a required privilege
    # is not held"). Seen in the VM, 2026-11-17: api_docs, frame_stats and replay then ended
    # in a bare OSError.
    def no_privilege(self, target, target_is_directory=False):
        error = OSError(22, "A required privilege is not held by the client")
        error.winerror = 1314
        raise error
    monkeypatch.setattr(shadowdir.Path, "symlink_to", no_privilege)
    with pytest.raises(HarnessError) as e:
        shadowdir.make(_fake_fs2(tmp_path), tmp_path / "shadow")
    assert e.value.code == EXIT_REFUSED
    assert "links" in e.value.message and "Developer Mode" in e.value.hint


def test_any_other_link_failure_is_not_dressed_up(tmp_path, monkeypatch):
    def broken(self, target, target_is_directory=False):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(shadowdir.Path, "symlink_to", broken)
    with pytest.raises(OSError) as e:
        shadowdir.make(_fake_fs2(tmp_path), tmp_path / "shadow")
    assert not isinstance(e.value, HarnessError) and e.value.errno == 28


def test_links_to_folders_say_so_even_when_the_folder_is_not_there_yet(tmp_path, monkeypatch):
    # Windows has two kinds of link. A link made to a path that does not exist yet becomes a
    # FILE link unless told otherwise, and the bridge mod's folder is linked before launch
    # installs it: the link would never work as a folder.
    made = {}

    def record(self, target, target_is_directory=False):
        made[self.name] = target_is_directory
    monkeypatch.setattr(shadowdir.Path, "symlink_to", record)
    shadowdir.make(_fake_fs2(tmp_path), tmp_path / "shadow")
    assert made == {"MVPS": True, "data": True, "root_fs2.vp": False,
                    "fso_harness_bridge": True}
