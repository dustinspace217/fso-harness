#!/usr/bin/env bash
# Run the FSO engine once with a probe mod first in -mod, on a private Xvfb display and a
# private preferences folder, then print the probe's log.
#
# usage: run_probe.sh <seconds> <mission|-> [extra fso args...]
# env:   FS2PATH FSO_BIN [FSO_TEST_MODS] [PROBE_MOD=<dir>] [PROBE_PREFS=<dir to copy prefs from>]
#
# WHY a shadow game folder: the engine finds mods and data relative to its working directory and
# writes some outputs (doc files, profiling.csv) there too. A folder of symlinks to FS2PATH's
# directories and .vp archives lets the probe mod live beside the real data without copying
# anything into the user's game folder. Only directories and .vp files are linked, so a loose
# file the engine writes (sexps.html) can never follow a link and overwrite a user file.
set -u
SECS=$1; MISSION=$2; shift 2
HERE=$(cd "$(dirname "$0")" && pwd)
MOD_SRC=${PROBE_MOD:-$HERE/probe_mod}
# Run folder: mktemp -d makes a fresh 0700 directory with a random name, so no other user can
# predict or pre-create it. Parent is $TMPDIR (default /tmp), never a fixed shared path.
D=$(mktemp -d "${TMPDIR:-/tmp}/fsoh-probe.XXXXXXXXXX")
mkdir -p "$D/prefs" "$D/game"
if [ -n "${PROBE_PREFS:-}" ]; then cp -r "$PROBE_PREFS"/. "$D/prefs/"; fi
for e in "$FS2PATH"/*; do
	n=$(basename "$e")
	if [ -d "$e" ] || [[ "$n" == *.vp ]]; then ln -s "$e" "$D/game/$n"; fi
done
cp -r "$MOD_SRC" "$D/game/fsoh_probe"

# Xvfb picks a free display and writes its number (-displayfd 1) into the pipe read here on fd 3,
# so parallel probes never collide on a fixed :N.
exec 3< <(Xvfb -displayfd 1 -screen 0 1280x800x24 -nolisten tcp 2>/dev/null)
XP=$!
read -r -t 10 DNUM <&3 || { echo "Xvfb did not start"; exit 1; }

unset WAYLAND_DISPLAY
export DISPLAY=":$DNUM" SDL_VIDEODRIVER=x11 GDK_BACKEND=x11 QT_QPA_PLATFORM=xcb FSO_PREFERENCES_PATH="$D/prefs/"
if [[ " $* " != *" -vulkan "* ]]; then export LIBGL_ALWAYS_SOFTWARE=1; fi
cd "$D/game" || exit 1
ARGS=(-mod "fsoh_probe,${FSO_TEST_MODS:-MVPS-4.7.3}" -window -res 1280x720 -nosound -no_unfocused_pause)
[ "$MISSION" != "-" ] && ARGS+=(-start_mission "$MISSION")
"$FSO_BIN" "${ARGS[@]}" "$@" > "$D/engine.out" 2>&1 < /dev/null &
EP=$!
# SHOTS=<n>: also screenshot the whole virtual screen every n seconds into the run folder.
for i in $(seq 1 "$SECS"); do
	kill -0 $EP 2>/dev/null || break
	if [ -n "${SHOTS:-}" ] && [ $((i % SHOTS)) -eq 0 ]; then
		import -window root "$D/shot-$(printf %04d "$i").png" 2>/dev/null
	fi
	sleep 1
done
if kill -0 $EP 2>/dev/null; then
	kill $EP; sleep 2; kill -9 $EP 2>/dev/null
	echo "== engine killed after ${SECS}s"
else
	wait $EP; echo "== engine exited by itself, status $?"
fi
kill $XP 2>/dev/null
echo "== run dir $D"
echo "== probe log"; cat "$D/prefs/fsoh_probe/data/cache/probe.log" 2>/dev/null || echo "(no probe log)"
echo "== engine tail"; tail -8 "$D/engine.out"
