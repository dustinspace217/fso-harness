import struct
import subprocess
import zlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VENV_PY = REPO / "mcp" / ".venv" / "bin" / "python"


def _tiny_png(path: Path) -> None:
    """Write a valid 1x1 PNG."""
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    raw = b"\x00\x00\x00\x00"
    head = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + head + chunk(b"IDAT", zlib.compress(raw))
                     + chunk(b"IEND", b""))


def test_mcp_server_maps_tools_to_fsoctl(tmp_path):
    if not VENV_PY.exists():
        pytest.skip("run mcp/setup.sh first (creates mcp/.venv)")
    png = tmp_path / "s.png"
    _tiny_png(png)
    r = subprocess.run([str(VENV_PY), str(REPO / "tests" / "mcp_check.py"), str(png)],
                       capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.startswith("OK")
