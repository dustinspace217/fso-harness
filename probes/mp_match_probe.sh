#!/usr/bin/env bash
# Spike B (multiplayer plan Task 2): a standalone server and one client inside ONE private,
# loopback-only network, then: is the server visible from the real network (want no), does the
# client join, who starts a mission given with -almission, what states the client passes.
#
# usage: mp_match_probe.sh [multiplayer mission, e.g. m-01]   (name without .fs2 for -almission)
# env:   FS2PATH FSO_BIN [FSO_TEST_MODS] [CLIENT_PORT=7809] [SECONDS_CLIENT=150]
#        [ALMISSION_ON=client|server]  (which side gets -almission; default client)
#
# WHY unshare + nsenter here and not the harness: the harness does not know matches yet; this
# script is the ground truth the match module is built from. WHY a shadow game folder: same reason
# as run_probe.sh (the engine writes next to its working directory; the user's folder stays clean).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
MISSION=${1:-}
OUT=$(mktemp -d "${TMPDIR:-/tmp}/fsoh-mp.XXXXXXXXXX")
echo "out: $OUT"
# QA XFA-4: whatever ends this script, the server and the holder go with it (bounded).
SRV=""; HOLDER=""
cleanup() {
	[ -n "$SRV" ] && { kill "$SRV" 2>/dev/null; sleep 2; kill -9 "$SRV" 2>/dev/null; }
	[ -n "$HOLDER" ] && kill "$HOLDER" 2>/dev/null
}
trap cleanup EXIT INT TERM
mkdir -p "$OUT/game" "$OUT/srv/data"
for e in "$FS2PATH"/*; do
	n=$(basename "$e")
	if [ -d "$e" ] || [[ "$n" == *.vp ]]; then ln -s "$e" "$OUT/game/$n"; fi
done

# 1. The private network: a holder that only sleeps. Its pid is the handle.
unshare --user --map-root-user --net sh -c 'ip link set lo up && exec sleep infinity' &
HOLDER=$!
sleep 0.5
NS=(nsenter --preserve-credentials --user --net --target "$HOLDER" --)
echo "holder pid $HOLDER; inside: $("${NS[@]}" ip -brief addr show lo)"

# 2. Standalone server, no display, private prefs with a minimal multi.cfg (never +pxo).
printf '+name mpprobe\n' > "$OUT/srv/data/multi.cfg"
SRV_EXTRA=()
if [ -n "$MISSION" ] && [ "${ALMISSION_ON:-client}" = "server" ]; then SRV_EXTRA=(-almission "$MISSION"); fi
(
	cd "$OUT/game" || exit 1
	# exec: nsenter and then the engine take over this subshell's pid, so `kill $SRV` below
	# reaches the engine (without it the first runs left three standalones running).
	exec env -u DISPLAY -u WAYLAND_DISPLAY FSO_PREFERENCES_PATH="$OUT/srv/" "${NS[@]}" "$FSO_BIN" \
		-mod "${FSO_TEST_MODS:-MVPS-4.7.3}" -standalone -port 7808 -nosound -noninteractive \
		"${SRV_EXTRA[@]}" > "$OUT/server.out" 2>&1
) &
SRV=$!
for _ in $(seq 1 150); do
	"${NS[@]}" ss -H -lun "sport = :7808" | grep -q . && break
	sleep 0.2
done
echo "3. server sockets on :7808 inside the match: $("${NS[@]}" ss -H -lun 'sport = :7808' | wc -l) (want 1)"
echo "4. sockets on :7808 on the REAL network:      $(ss -H -lun 'sport = :7808' | wc -l) (want 0)"
echo "5. web panel :8080 inside / outside:          $("${NS[@]}" ss -H -ltn 'sport = :8080' | wc -l) / $(ss -H -ltn 'sport = :8080' | wc -l) (want 1 / 0)"

# 3. Client: run_probe.sh inside the same network (it starts its own Xvfb there; the X socket is
#    a filesystem socket, so tools outside the network can still reach the display).
EXTRA=(-nomovies -connect 127.0.0.1:7808 -port "${CLIENT_PORT:-7809}")
if [ -n "$MISSION" ] && [ "${ALMISSION_ON:-client}" = "client" ]; then EXTRA+=(-almission "$MISSION"); fi
PROBE_MOD="$HERE/mp_probe_mod" "${NS[@]}" bash "$HERE/run_probe.sh" "${SECONDS_CLIENT:-150}" - "${EXTRA[@]}" | tee "$OUT/client.out"

kill "$SRV" 2>/dev/null
for _ in $(seq 1 20); do kill -0 "$SRV" 2>/dev/null || break; sleep 0.5; done   # bounded 10 s
kill -9 "$SRV" 2>/dev/null; SRV=""
echo "6. server log tail:"; tail -n 40 "$OUT/srv/data/fs2_open.log" 2>/dev/null || tail -n 40 "$OUT/server.out"
echo "done; folder kept: $OUT"
