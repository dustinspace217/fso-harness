"""The stop request on real Windows (plan B Task 10): Ctrl-Break sent to a real `fsoctl`
command's process group ends it through the CLI's handler, with exit code 143, the code the
Linux SIGTERM path gives."""
import ctypes
import signal
import subprocess
import sys
import time

import pytest

# An fsoctl command with one extra verb that waits in short sleeps, as the real verbs do.
COMMAND = r"""
import sys, time
from fsoctl import cli

def wait(args):
    print("waiting", flush=True)
    try:
        for _ in range(300):
            time.sleep(0.1)
        return {}
    finally:
        print("cleanup ran", flush=True)

cli.VERBS["wait-a-while"] = ("test verb", lambda p: None, wait)
raise SystemExit(cli.main(["wait-a-while"]))
"""


def test_ctrl_break_ends_a_real_fsoctl_command_through_its_cleanup():
    if not ctypes.windll.kernel32.GetConsoleCP():
        pytest.skip("this process has no console; Ctrl-Break cannot be sent from it")
    proc = subprocess.Popen([sys.executable, "-c", COMMAND], stdout=subprocess.PIPE, text=True,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        assert proc.stdout.readline().strip() == "waiting"
        started = time.monotonic()
        proc.send_signal(signal.CTRL_BREAK_EVENT)
        rest, _ = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
    assert proc.returncode == 143
    assert "cleanup ran" in rest
    assert time.monotonic() - started < 10
