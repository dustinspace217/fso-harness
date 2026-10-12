#!/usr/bin/env bash
# Probe: can qtFRED's error checker be read by OCR on a headless display?
# Copies the mission into a private run folder, opens that copy in qtFRED on a private Xvfb,
# screenshots after load, presses Ctrl+S (which runs the error checker before saving),
# screenshots again and OCRs both shots.
#
# usage: fred_ocr_probe.sh <absolute mission path>   env: FS2PATH QTFRED_BIN [FSO_TEST_MODS]
set -u
MISSION=$1
# Run folder: mktemp -d makes a fresh 0700 directory with a random name, so no other user can
# predict or pre-create it. Parent is $TMPDIR (default /tmp), never a fixed shared path.
D=$(mktemp -d "${TMPDIR:-/tmp}/fsoh-fredprobe.XXXXXXXXXX")
mkdir -p "$D/prefs" "$D/game"
# Open a COPY: the Ctrl+S below makes qtFRED save over the file it opened, and the caller's
# mission must never be written (QA SA-4).
cp -- "$MISSION" "$D/" || { echo "cannot copy $MISSION"; exit 1; }
MISSION="$D/$(basename -- "$MISSION")"
for e in "$FS2PATH"/*; do
	n=$(basename "$e")
	if [ -d "$e" ] || [[ "$n" == *.vp ]]; then ln -s "$e" "$D/game/$n"; fi
done
exec 3< <(Xvfb -displayfd 1 -screen 0 1280x800x24 -nolisten tcp 2>/dev/null)
XP=$!
read -r -t 10 DNUM <&3 || { echo "Xvfb did not start"; exit 1; }
unset WAYLAND_DISPLAY
export DISPLAY=":$DNUM" SDL_VIDEODRIVER=x11 GDK_BACKEND=x11 QT_QPA_PLATFORM=xcb LIBGL_ALWAYS_SOFTWARE=1 FSO_PREFERENCES_PATH="$D/prefs/"
cd "$D/game" || exit 1
read -r -a EXTRA <<< "${FRED_EXTRA:-}"
"$QTFRED_BIN" -mod "${FSO_TEST_MODS:-MVPS-4.7.3}" -start_mission "$MISSION" "${EXTRA[@]}" > "$D/fred.out" 2>&1 < /dev/null &
FP=$!
# Wait (bounded) for the main window.
for _ in $(seq 1 120); do
	xdotool search --onlyvisible --pid $FP >/dev/null 2>&1 && break
	kill -0 $FP 2>/dev/null || break
	sleep 1
done
sleep 15
import -window root "$D/after-load.png"
xdotool search --onlyvisible --name . getwindowname %@ > "$D/windows-after-load.txt" 2>/dev/null
WID=$(xdotool search --onlyvisible --pid $FP | head -1)
echo "== main window $WID: $(xdotool getwindowname "$WID")"
xdotool windowsize "$WID" 1280 760
sleep 2
# No window manager: give keyboard focus by clicking the status bar (neutral spot), then send the key.
xdotool mousemove 900 745 click 1
sleep 1
xdotool windowfocus "$WID" 2>/dev/null
xdotool key ctrl+s
sleep 6
import -window root "$D/after-save.png"
xdotool search --onlyvisible --name . getwindowname %@ > "$D/windows-after-save.txt" 2>/dev/null
kill $FP 2>/dev/null; sleep 2; kill -9 $FP 2>/dev/null; kill $XP 2>/dev/null
echo "== run dir $D"
echo "== windows after load"; cat "$D/windows-after-load.txt"
echo "== windows after save"; cat "$D/windows-after-save.txt"
echo "== OCR after save"; tesseract "$D/after-save.png" - --psm 6 2>/dev/null
echo "== qtfred tail"; tail -5 "$D/fred.out"
