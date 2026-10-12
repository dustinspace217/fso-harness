import json

from fsoctl import session, shadowdir


def _fake_fs2(tmp_path):
    fs2 = tmp_path / "FS2"
    (fs2 / "data").mkdir(parents=True)
    (fs2 / "MVPS").mkdir()
    (fs2 / "root_fs2.vp").write_bytes(b"VPVP")
    (fs2 / "sexps.html").write_text("user's old file")
    return fs2


def test_make_links_folders_and_vp_files_only(tmp_path):
    fs2 = _fake_fs2(tmp_path)
    sh = shadowdir.make(fs2, tmp_path / "shadow")
    assert sorted(p.name for p in sh.iterdir()) == ["MVPS", "data", "fso_harness_bridge",
                                                     "root_fs2.vp"]
    assert all((sh / n).is_symlink() for n in ("MVPS", "data", "root_fs2.vp"))
    # The bridge link exists before the bridge is installed (launch installs it later).
    assert (sh / "fso_harness_bridge").is_symlink() and not (sh / "fso_harness_bridge").exists()


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
