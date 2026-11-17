#!/bin/bash
# Probe 2 on Linux (cross-platform Plan A, Task 6): run a11y_probe.py in a private session.
#
# Everything the probe touches is private to this run, so it can never reach the desktop the
# script is started from (QA SA-1, CR-11, SA-4):
#   - a D-Bus session bus that cannot auto-start anything (a11y-session.conf has no service
#     folders, so the Qt app cannot wake the desktop's portal or keyring services);
#   - an X display from xvfb-run -a: a free display number and an auth cookie, so it is never
#     an existing server and no other client can connect;
#   - a runtime folder of its own: the AT-SPI bus launcher makes its socket under
#     $XDG_RUNTIME_DIR, and must never replace the desktop's accessibility bus socket;
#   - the AT-SPI registry started by hand (on a desktop systemd starts it; this session has none).
#
#   linux_a11y_session.sh <python with PySide6 and pyatspi> <out dir> [probe script name]
#
# The python is usually a venv made with --system-site-packages (pyatspi is a system package).
set -u
here=$(dirname "$(readlink -f "$0")")
if [ "${1:-}" != "--inner" ]; then
	# A sentinel argument, not an environment variable, marks the inner run: an inherited
	# variable could skip the private session (QA SA-5).
	exec timeout 180 dbus-run-session --config-file="$here/a11y-session.conf" -- \
		xvfb-run -a -s "-screen 0 1024x768x24 -nolisten tcp" "$0" --inner "$@"
fi
shift
python=$1 out=$2 probe=${3:-a11y_probe.py}      # third argument: another probe in this folder
runtime=$(mktemp -d)
export XDG_RUNTIME_DIR=$runtime QT_QPA_PLATFORM=xcb
# The desktop's accessibility bus address would make the toolkit and the reader join it.
unset WAYLAND_DISPLAY AT_SPI_BUS_ADDRESS
/usr/libexec/at-spi-bus-launcher --launch-immediately &
bus=$!
sleep 1
/usr/libexec/at-spi2-registryd &
registry=$!
sleep 1
"$python" "$here/$probe" --out "$out"
status=$?
kill "$registry" "$bus" 2>/dev/null
wait 2>/dev/null
rm -rf "$runtime"
exit "$status"
