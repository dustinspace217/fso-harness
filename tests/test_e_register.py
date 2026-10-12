"""mcp/register.sh must bake the harness's environment into the Claude Code registration.

WHY: Claude Code starts a stdio MCP server with its OWN process environment (plus the settings
`env` block), read from the 2.1.296 binary: the server env is process.env unless
CLAUDE_CODE_MCP_ALLOWLIST_ENV is set, in which case only HOME/LOGNAME/PATH/SHELL/TERM/USER pass.
A session started from the desktop or a service has no FS2PATH or FSO_BIN, so
every tool would fail unless registration captured them with `-e` (review finding, 2026-10-12).

`claude` is replaced on PATH by a script that records its arguments, so nothing is registered.
"""
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "mcp" / "register.sh"


@pytest.fixture
def fake_claude(tmp_path):
    """A `claude` on PATH that writes its argv (one per line) to a file; returns that file."""
    log = tmp_path / "claude-argv.txt"
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{log}"\n')
    exe.chmod(0o755)
    venv_python = REPO / "mcp" / ".venv" / "bin" / "python"
    if not venv_python.exists():
        pytest.skip("mcp/.venv missing (run mcp/setup.sh)")
    return log, exe.parent


def _run(path_dir: Path, env_extra: dict) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("FS2PATH", "FSO_BIN", "FSO_BIN_DEBUG", "QTFRED_BIN")}
    env["PATH"] = f"{path_dir}:{env['PATH']}"
    env.update(env_extra)
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True,
                          timeout=30, check=False)


def test_register_bakes_the_harness_variables_into_the_registration(fake_claude):
    log, path_dir = fake_claude
    r = _run(path_dir, {"FS2PATH": "/games/FS2", "FSO_BIN": "/opt/fso/fs2_open",
                        "QTFRED_BIN": "/opt/fso/qtfred"})
    assert r.returncode == 0, r.stderr
    argv = log.read_text().splitlines()
    assert "-e" in argv and "FS2PATH=/games/FS2" in argv and "FSO_BIN=/opt/fso/fs2_open" in argv
    assert "QTFRED_BIN=/opt/fso/qtfred" in argv
    assert not any(a.startswith("FSO_BIN_DEBUG=") for a in argv)   # unset stays unset
    assert argv[-2:] == [str(REPO / "mcp" / ".venv" / "bin" / "python"),
                         str(REPO / "mcp" / "server.py")]


def test_register_refuses_without_the_two_required_variables(fake_claude):
    log, path_dir = fake_claude
    r = _run(path_dir, {"FS2PATH": "/games/FS2"})           # FSO_BIN missing
    assert r.returncode != 0 and "FSO_BIN" in r.stdout + r.stderr
    assert not log.exists()                                  # claude was never called
