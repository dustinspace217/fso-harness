"""The launcher's port test as Windows runs it (plan B Task 7, Step 2)."""
import subprocess
import sys

import pytest

from fsoctl import launcher
from fsoctl.platform import win_procs

# Loopback: binding every address makes Windows Firewall ask the user about the program.
BINDER = ("import socket, sys, time\n"
          "s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
          "s.bind(('127.0.0.1', 0))\n"
          "print(s.getsockname()[1], flush=True)\n"
          "time.sleep(60)\n")


@pytest.mark.host_ports        # the real port_free, not the unit tier's stub (conftest)
def test_a_port_another_process_holds_is_not_free():
    other = subprocess.Popen([sys.executable, "-c", BINDER], stdout=subprocess.PIPE, text=True)
    try:
        port = int(other.stdout.readline())
        assert launcher.port_free("udp", port) is False
        assert win_procs.socket_owner_pids("udp", port) == {other.pid}
    finally:
        other.kill()
        other.wait(timeout=30)
    assert launcher.port_free("udp", port) is True
