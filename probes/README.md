# Probes

One-off experiments that answered "does the engine actually do X?" before the harness was built on
the answer. They are kept because they document behaviour the bridge depends on; results are in
`docs/research/2026-10-12-probe-results.md`.

- `run_probe.sh <seconds> <mission|-> [fso args]` — runs the engine with `probe_mod` first in `-mod`,
  on a private Xvfb and a private preferences folder, from a shadow game folder (symlinks to
  FS2PATH's directories and `.vp` archives) so nothing is written into the real game folder.
  Env: `FS2PATH`, `FSO_BIN`, optional `FSO_TEST_MODS`, `PROBE_MOD` (another mod folder),
  `PROBE_PREFS` (a prefs folder to start from), `SHOTS=<n>` (screenshot every n seconds).
- `probe_mod/` — logs state changes, answers popups (Submit + Override), creates a pilot, skips both
  briefings, flies with Lua control, fires, sets time compression, pauses and resumes.
- `fred_ocr_probe.sh <absolute mission path>` — opens a mission in qtFRED on Xvfb, presses Ctrl+S,
  screenshots the pre-save error check. Env: `FS2PATH`, `QTFRED_BIN`, `FRED_EXTRA` (extra args,
  e.g. `-noninteractive`).

Run engine probes under `bounded-run`.
