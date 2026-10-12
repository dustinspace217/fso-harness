import pytest

from fsoctl import bridge_install, paths, scope


@pytest.fixture(autouse=True)
def no_running_scopes(monkeypatch):
    """Unit tier: never ask the real systemd (an engine test elsewhere may be running)."""
    monkeypatch.setattr(scope, "active_units", lambda: [])


def test_install_copies_and_stamps(tmp_runtime, tmp_path):
    r = bridge_install.install(tmp_path)
    target = tmp_path / paths.BRIDGE_MOD
    assert r["updated"] is True
    assert (target / "data" / "scripts" / "fsoharness.lua").exists()
    assert (target / ".harness-version").read_text() == r["version"]


def test_install_is_noop_when_current(tmp_runtime, tmp_path):
    bridge_install.install(tmp_path)
    assert bridge_install.install(tmp_path)["updated"] is False


def test_install_replaces_stale_bridge(tmp_runtime, tmp_path):
    bridge_install.install(tmp_path)
    target = tmp_path / paths.BRIDGE_MOD
    (target / ".harness-version").write_text("old")
    # A unique marker, not a common word: the shipped Lua's own comments may say "stale".
    (target / "data" / "scripts" / "fsoharness.lua").write_text("-- FSOH-TAMPERED-7f3a")
    (target / "leftover.txt").write_text("x")
    r = bridge_install.install(tmp_path)
    assert r["updated"] is True
    assert "FSOH-TAMPERED-7f3a" not in (target / "data" / "scripts" / "fsoharness.lua").read_text()
    assert not (target / "leftover.txt").exists()
