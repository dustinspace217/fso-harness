"""win_procs and the lock primitive against real Windows (plan B Tasks 1 and 2)."""
import os
import socket
import subprocess
import sys

from fsoctl.platform import win_locks, win_procs


def test_this_process_is_alive_has_a_start_time_and_lists_its_modules():
    me = os.getpid()
    assert win_procs.pid_alive(me) and not win_procs.gone(me)
    start = win_procs.pid_start(me)
    assert isinstance(start, int) and start > 0 and win_procs.pid_start(me) == start
    libraries = win_procs.loaded_libraries(me).lower()
    assert "python" in libraries and "kernel32.dll" in libraries


def test_an_ended_process_is_gone_even_while_a_handle_to_it_is_held():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])     # Popen keeps a handle open
    start = win_procs.pid_start(proc.pid)
    proc.wait(timeout=30)
    assert win_procs.pid_alive(proc.pid) is False and win_procs.gone(proc.pid) is True
    assert start is not None
    # ...and it still answers with its start time. This is why "is the recorded runner
    # alive" asks pid_alive first and never trusts a matching start time alone
    # (launcher._runner_known_alive).
    assert win_procs.pid_start(proc.pid) == start
    assert win_procs.in_group(proc.pid, "fso-harness-no-such-job") is False


def test_pid_ended_tells_a_finished_process_and_an_unused_pid_from_a_running_one():
    # the cleanup rule that ends a session with no runner acts on this answer alone
    assert win_procs.pid_ended(os.getpid()) is False
    proc = subprocess.Popen([sys.executable, "-c", "pass"])     # Popen keeps a handle open
    pid = proc.pid
    proc.wait(timeout=30)
    assert win_procs.pid_ended(pid) is True                     # ended, handle still held
    del proc                                                    # the last handle goes
    # no process has this number: Windows pids are multiples of 4, and this one is not
    assert win_procs.pid_ended(0x7FFFFFF1) is True


def test_the_owner_of_a_bound_udp_port_is_found():
    # Loopback, here and below: binding every address makes Windows Firewall ask the user.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        assert win_procs.socket_owner_pids("udp", port) == {os.getpid()}
    assert os.getpid() not in win_procs.socket_owner_pids("udp", port)


def test_a_port_held_by_an_ipv6_only_socket_has_an_owner():
    # `netstat -ano -p UDP` does not list this socket (IPv4 sockets only); the engine's
    # socket is IPv6 dual stack, so a port held this way is not free for it.
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s.bind(("::1", 0))
        port = s.getsockname()[1]
        assert win_procs.socket_owner_pids("udp", port) == {os.getpid()}


def test_the_owner_of_a_listening_tcp_port_is_found():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        assert win_procs.socket_owner_pids("tcp", port) == set()      # bound, not listening
        s.listen(1)
        assert win_procs.socket_owner_pids("tcp", port) == {os.getpid()}


def test_a_lock_held_on_one_handle_is_refused_on_another(tmp_path):
    with open(tmp_path / "x.lock", "a") as first, open(tmp_path / "x.lock", "a") as second:
        assert win_locks.lock_exclusive(first) is True
        assert win_locks.lock_exclusive(second) is False
        win_locks.unlock(first)
        assert win_locks.lock_exclusive(second) is True
        win_locks.unlock(second)
