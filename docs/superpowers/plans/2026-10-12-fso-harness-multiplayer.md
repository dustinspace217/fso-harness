# fso-harness multiplayer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an agent run a FreeSpace Open multiplayer game (standalone server or player host, plus clients) as harness sessions on one machine, inside a private network that the LAN and the internet cannot see.

**Architecture:** A *match* is an unprivileged user+network namespace held open by a sleeping holder process in its own systemd scope (`unshare --user --map-root-user --net`). Multiplayer sessions join it by starting their runner through `nsenter --target <holder pid>`, so Xvfb and the engine live in the private network while every harness file (session.json, bridge channel, X socket) stays reachable from outside. Three roles map to engine flags; readiness, stop ordering and cleanup extend the v1 code paths rather than adding new ones.

**Tech Stack:** Python 3 (fsoctl), util-linux `unshare`/`nsenter`, iproute2 `ss`/`ip`, systemd user scopes, the bridge's Lua, pytest (unit + `engine` tier under `bounded-run`), mutmut.

**Spec:** `docs/superpowers/specs/2026-10-12-fso-harness-multiplayer-design.md` (approved 2026-10-12). Parent: `docs/superpowers/specs/2026-10-12-fso-harness-design.md`.

## Status (updated 2026-10-13 01:15 PDT)
Phase: 11 of 11 BUILT, QA CLOSED (multiplayer), plus the DEF-MP-03 slice (every session's
engine in a private network) BUILT on build/private-default and through its own HIGH-RISK QA
(52 IDs, manifest OK; record kept outside this repo)
Done: match/role/network slice merged to main and published (6f1817b); private-by-default
engine with the runner and Xvfb on the host (aa6652c), found and fixed on the way: a namespaced
Xvfb takes the desktop's display number as seen from the host (DEF-MP-03 appendix entry)
Next: merge build/private-default into main and publish (Dustin: "Implement, and push to
github"); re-register the MCP server from the merged checkout; the upstream note on a
bind-address / no-network engine flag is Dustin's to post
Blocked: nothing

## Deviations from the plan as written
- DEF-MP-03 slice (2026-10-13, behavioral-change, Dustin-approved "I concur with your
  recommendation"): every session's ENGINE now runs in a private network by default
  (`--network real` opts out). First built as "runner under unshare + host-side display
  allocator" (b07f174, a940bf8); the QA found the allocator could not hold a display number for
  the session's life, so the shipped shape (aa6652c) keeps the runner and Xvfb on the host and
  wraps only the engine (runner.engine_argv). Effect: an ordinary session no longer binds UDP
  7808 on the LAN; nothing else a user sees changes. Below the plan's level: which process
  carries the namespace.
- Found on the way (2026-10-13, other): the PUBLISHED match clients (6f1817b) had the same
  display-number collision (their Xvfb ran inside the match namespace); fixed by the same
  change. The head's probes replaced and then removed the desktop's /tmp/.X11-unix/X1 socket
  file on Dustin's machine (abstract :1 unaffected; recreated at next login); told him.
- Task 1: `-nomovies` is required for any probe (the intro movie blocks the frame hook).
- Task 2: `-almission` belongs to the host player, never the standalone; the spike script carries
  `ALMISSION_ON=client|server` instead of a hand edit; the start-game screen needs an Accept
  (GS_EVENT_MULTI_HOST_SETUP) the plan did not know about.
- Task 5: every engine process binds its own UDP port, so sessions in a match get 7808, 7809,
  ... (spec section 4 amended); a standalone refuses `--mission` and `--pilot-source`.
- Task 6: a background security review found argument/config injection through `game_name`,
  `connect` and `mission`; fixed in its own commit (e2e47b7) with a RED-first test. The Lua tier
  hosts the bridge tests (stubs extended), so Task 9 is not the only coverage. The
  "Joining netgame" popup is exempt from `dismiss_popups`.
- Task 8: the lifetime behaviour had landed with Task 4; the tests pin it (hand mutant killed).
- Task 9: the bridge gained a post-briefing sync-screen Launch hook (the client sat in
  GS_STATE_MULTI_MISSION_SYNC for 300 s without it); the standalone's web panel is NOT used as
  test evidence (its copies were stale for a minute of gameplay; probe-results 21.4); panel
  credentials stay the engine defaults admin/admin; the spike script's server kill was fixed
  after three standalones outlived their holders.
- Task 10: INSTRUCTIONS went over the 2,048 cap once because a gating check's exit status was
  masked by a pipe; caught by task-done, fixed, amended before any push.
- Task 11: mutation survivors are dispositioned by class (message strings, argparse help,
  engine-tier-only functions) in mutation.md, each class with its mutant names, instead of one
  ID per mutant (196 survivors); real logic survivors got tests (pid_start parsing, alive cases,
  host -port, long mission names, wait-listening order, remove on running sessions, CLI choices).

## Global Constraints

- Containment is the private network per match; nothing in this plan opens, forwards, or binds a port on the real network (spec §5.1, §5.5). The standalone web panel (TCP 8080) stays reachable only from inside the match.
- The harness never writes `+pxo` into a `multi.cfg` and never writes a tracker option to a pilot (spec §5.3).
- Nothing runs as the real root; `fsoctl` refuses match verbs when `os.getuid() == 0` (spec §5.4). No sudo anywhere.
- `--map-root-user`, never `--map-current-user` (the latter cannot bring up loopback; spec §3).
- Every engine run goes through `bounded-run`; every wait is bounded; sessions never touch the user's desktop, pilots, settings or game folder (project CLAUDE.md etiquette).
- No daemon: match state is `matches/<id>/match.json`, read fresh by every verb (spec §6).
- A match's sessions count against `FSO_HARNESS_MAX_SESSIONS` (default 3); the match itself counts zero (spec §6).
- Commit messages in this repo keep the Claude co-author trailer; nothing goes upstream from this plan.
- Comments follow the workspace Commenting Rules (what + why); Python 4-space indent; `ruff check .` clean; shellcheck clean on new `probes/*.sh`.

## Review Focus

Inputs the spec implies but no task's tests exercise; each line's test is added to the owning task.

1. **Two clients in one match bind the same local port.** The engine binds `[::]:<port>` for every process, so a second client with the default port gets EADDRINUSE and dies at startup. Expected: the harness assigns each session in a match a distinct port (server 7808, clients 7809, 7810...) unless `--port` is given, and refuses a duplicate. (Task 5 pins it.)
2. **A match id or session role typed wrong** (`--role server`, `--match s1a2b3c4d`). Expected: a bad role is EXIT_USAGE with the valid values named; a bad or unknown match id is EXIT_NOT_FOUND (QA CR-9); nothing created either way. (Task 5.)
3. **The holder dies while sessions run** (scope killed, RuntimeMaxSec). Expected: `status` reports `dead-network` for each session, `launch --match` into it is refused, `cleanup` removes the match only after its sessions are gone. (Task 8.)
4. **`match remove` while a session is still starting** (its runner has no engine pid yet). Expected: the session is stopped through the normal stop path (marker, runner first, scope backstop), then the holder; nothing is left in the namespace. (Task 8.)
5. **A standalone launched with `--mission`** when the auto-load flag is consumed by the game master, which may not be the standalone. Expected: the harness documents who starts the mission (spike result) and refuses the combination that does nothing. (Task 2 measures; Task 5 pins the rule.)

---

### Task 1: Spike A, client enters the join screen without a human

**Files:**
- Create: `probes/mp_probe_mod/data/tables/mpprobe-sct.tbm`
- Modify: `docs/research/2026-10-12-probe-results.md` (new section 21)
- Modify: `probes/README.md` (one bullet)

**Interfaces:**
- Consumes: `probes/run_probe.sh` (`PROBE_MOD=<dir>`, args `<seconds> <mission|-> [fso args]`; prints the mod's `probe.log`).
- Produces: facts for Tasks 5 and 6: (a) whether `ui.PilotSelect.createPilot(name, true)` + `selectPilot(name, true)` yields a multiplayer pilot the main hall accepts; (b) whether `ba.postGameEvent(ba.GameEvents["GS_EVENT_MULTI_JOIN_GAME"])` from the main hall reaches `GS_STATE_MULTI_JOIN_GAME` and the auto-join query runs; (c) the exact log lines.

- [ ] **Step 1: Write the probe mod**

`probes/mp_probe_mod/data/tables/mpprobe-sct.tbm` (tabs, LF):

```
#Conditional Hooks
$Application: FS2_Open
$On Game Init: [
	-- Spike A (multiplayer plan Task 1): log state changes, create a MULTIPLAYER pilot, and
	-- post the join-screen event from the main hall the way the main hall's own button does
	-- (mainhallmenu.cpp:387). Writes probe.log through the cf API, never io.*.
	MpLines = {}
	function MpLog(s)
		MpLines[#MpLines + 1] = s
		local f = cf.openFile("probe.log", "w", "data/cache")
		if f and f:isValid() then
			f:write(table.concat(MpLines, "\n") .. "\n")
			f:close()
		end
	end
	MpLast = ""
	MpDone = {}
	MpLog("mp probe init")
]
$On Frame: [
	local st = ba.getCurrentGameState().Name
	if st ~= MpLast then
		MpLast = st
		MpLog(string.format("%.1f state %s", ba.getFrametimeOverall() / 65536, st))
	end
	if st == "GS_STATE_INITIAL_PLAYER_SELECT" and not MpDone.pilot then
		MpDone.pilot = true
		local ok = ui.PilotSelect.createPilot("MpProbe", true)
		MpLog("createPilot(MpProbe, multi=true) -> " .. tostring(ok))
		ui.PilotSelect.selectPilot("MpProbe", true)
	end
	if st == "GS_STATE_MAIN_MENU" and not MpDone.join then
		MpDone.join = true
		local ok = ba.postGameEvent(ba.GameEvents["GS_EVENT_MULTI_JOIN_GAME"])
		MpLog("post GS_EVENT_MULTI_JOIN_GAME -> " .. tostring(ok))
	end
]
```

Then append, unchanged, the `$On Dialog Frame:` hook and its `+Override:` line from `probes/probe_mod/data/tables/probe-sct.tbm` (they log and submit every popup; Submit without the override is ignored, project CLAUDE.md), replacing its `PrbLog`/`PrbDone`/`PrbSubmitted` names with `MpLog`/`MpDone`/`MpSubmitted`. End the file with `#End`.

- [ ] **Step 2: Run it (no server; the join query will time out, that is fine)**

Run (from the harness checkout): `PROBE_MOD=$PWD/probes/mp_probe_mod bounded-run probes/run_probe.sh 60 - -connect 127.0.0.1:7808`
Expected: the printed probe.log shows `createPilot(MpProbe, multi=true) -> true`, a `state GS_STATE_MAIN_MENU` line, `post GS_EVENT_MULTI_JOIN_GAME -> true`, then `state GS_STATE_MULTI_JOIN_GAME`. Any popup line names the text (e.g. a "single player pilot" or "connection speed" popup).

- [ ] **Step 3: If the join state is not reached, run the fallback and record which worked**

Fallback A1: the main hall sets `Game_mode = GM_MULTIPLAYER` before posting the event (mainhallmenu.cpp:380); if the join screen refuses a non-multi game mode, the log shows an immediate return to `GS_STATE_MAIN_MENU` or a popup. Fallback A2: drive the real button: take a screenshot of the main hall at 1280x720 (`SHOTS=5` on run_probe.sh), find the multiplayer door, and in a second run replace the `postGameEvent` lines with nothing and click it from outside with `xdotool mousemove --window <id> X Y click 1` (the probe prints DISPLAY). Record which of A, A1-diagnosis, A2 reached `GS_STATE_MULTI_JOIN_GAME`.

- [ ] **Step 4: Record the results**

Append to `docs/research/2026-10-12-probe-results.md`:

```
## 21. Multiplayer: pilot, join screen, private network (plan Tasks 1-2, 2026-10-12)

### 21.1 Multiplayer pilot and join-screen entry (Task 1, probes/mp_probe_mod)
- createPilot(name, true) / selectPilot(name, true): <result and log line>
- Posting GS_EVENT_MULTI_JOIN_GAME from GS_STATE_MAIN_MENU: <reached GS_STATE_MULTI_JOIN_GAME: yes/no; popups seen; fallback used>
- Auto-join query with -connect and no server: <log lines; how long until the "Joining netgame" popup or cancel>
- Connection speed: no popup (os_config_read_string(nullptr, "ConnectionSpeed", "Fast") default, multiutil.cpp:2939).
```

Fill every `<...>` with what the log showed. Add to `probes/README.md`: `` - `mp_probe_mod/` — multiplayer pilot + join-screen entry probe (probe-results section 21.1). ``

- [ ] **Step 5: Commit**

```bash
git add probes/mp_probe_mod probes/README.md docs/research/2026-10-12-probe-results.md
git commit -m "Spike: multiplayer pilot and join-screen entry (probe-results 21.1)"
```

---

### Task 2: Spike B, standalone + client inside one match network, heartbeat in multiplayer states

**Files:**
- Create: `probes/mp_match_probe.sh`
- Modify: `docs/research/2026-10-12-probe-results.md` (section 21.2, 21.3)
- Modify: `probes/README.md` (one bullet)

**Interfaces:**
- Consumes: Task 1's probe mod; the harness bridge mod (`fso_harness_bridge`, installed in FS2PATH by any earlier launch) for the heartbeat question.
- Produces: facts for Tasks 5-7: the server listens inside the namespace and not outside; `-connect 127.0.0.1:7808` with a distinct client `-port` joins; which process consumes `-almission` (standalone vs the first client, multiui.cpp:3667 requires `NETINFO_FLAG_AM_MASTER`); the states a client passes through; whether the bridge's frame hook keeps writing heartbeats in `GS_STATE_MULTI_JOIN_GAME`, `GS_STATE_MULTI_CLIENT_SETUP`, `GS_STATE_MULTI_HOST_SETUP`, `GS_STATE_MULTI_MISSION_SYNC`.

- [ ] **Step 1: Write the probe script**

`probes/mp_match_probe.sh`:

```bash
#!/usr/bin/env bash
# Spike B (multiplayer plan Task 2): a standalone server and one client inside ONE private,
# loopback-only network, then: is the server visible from the real network (want no), does the
# client join, who starts a mission given with -almission, what states the client passes.
#
# usage: mp_match_probe.sh [multiplayer mission, e.g. m-01]   (name without .fs2 for -almission)
# env:   FS2PATH FSO_BIN [FSO_TEST_MODS] [CLIENT_PORT=7809] [SECONDS_CLIENT=150]
#
# WHY unshare + nsenter here and not the harness: the harness does not know matches yet; this
# script is the ground truth the match module is built from. WHY a shadow game folder: same reason
# as run_probe.sh (the engine writes next to its working directory; the user's folder stays clean).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
MISSION=${1:-}
OUT=$(mktemp -d "${TMPDIR:-/tmp}/fsoh-mp.XXXXXXXXXX")
echo "out: $OUT"
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
(
	cd "$OUT/game" || exit 1
	FSO_PREFERENCES_PATH="$OUT/srv" "${NS[@]}" "$FSO_BIN" -mod "${FSO_TEST_MODS:-MVPS-4.7.3}" \
		-standalone -port 7808 -nosound -noninteractive > "$OUT/server.out" 2>&1
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
EXTRA=(-connect 127.0.0.1:7808 -port "${CLIENT_PORT:-7809}")
if [ -n "$MISSION" ]; then EXTRA+=(-almission "$MISSION"); fi
PROBE_MOD="$HERE/mp_probe_mod" "${NS[@]}" bash "$HERE/run_probe.sh" "${SECONDS_CLIENT:-150}" - "${EXTRA[@]}" | tee "$OUT/client.out"

kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null
echo "6. server log tail:"; tail -n 40 "$OUT/srv/data/fs2_open.log" 2>/dev/null || tail -n 40 "$OUT/server.out"
kill "$HOLDER" 2>/dev/null
echo "done; folder kept: $OUT"
```

`chmod +x probes/mp_match_probe.sh`; `shellcheck probes/mp_match_probe.sh` clean.

- [ ] **Step 2: Run without a mission**

Run (from the harness checkout): `bounded-run probes/mp_match_probe.sh`
Expected: line 3 prints 1, line 4 prints 0, line 5 prints `1 / 0`; the client log shows `GS_STATE_MULTI_JOIN_GAME` then `GS_STATE_MULTI_HOST_SETUP` (the first player in a standalone game becomes the host) or `GS_STATE_MULTI_CLIENT_SETUP`; the server log names the joined player (`MpProbe`).

- [ ] **Step 3: Run with a multiplayer mission, twice**

Pick a retail multiplayer mission name from `FS2PATH` (`fsoctl vp list root_fs2.vp | grep -i '^data/missions/m-'` or the MediaVPs' `m-*.fs2`), e.g. `m-01`.
Run A: `bounded-run probes/mp_match_probe.sh m-01` (the `-almission` goes to the CLIENT).
Run B: edit nothing; export `SRV_EXTRA` is not supported, so for this run add `-almission m-01` to the server line by hand, run, and revert. (One-off; the script keeps the client-side form because multiui.cpp:3667 gates the flag on `NETINFO_FLAG_AM_MASTER`.)
Expected: exactly one of the two runs shows the client passing `GS_STATE_MULTI_MISSION_SYNC` and reaching `GS_STATE_BRIEFING` or `GS_STATE_GAME_PLAY`. Record which; if neither, record the last state and the popup text (a non-multiplayer mission gives " Not a multi player-mission").

- [ ] **Step 4: Heartbeat in multiplayer states**

Repeat Step 2 with the bridge first in `-mod` instead of the probe mod: `PROBE_MOD=$FS2PATH/fso_harness_bridge` and, because the bridge needs `switches.txt`, pre-create `$OUT`'s prefs is not possible from outside; instead run one harness session normally (`fsoctl launch --skip-movies --pilot create:Hb`) to confirm the bridge version, then in this spike read `fs2_open.log` for the bridge's own `[fsoharness]` lines across the multiplayer states. Expected: a heartbeat write (the bridge logs `heartbeat` or its ready marker) continues in `GS_STATE_MULTI_JOIN_GAME` and the setup states; if the On Frame hook is silent in a state, record that state by name.

- [ ] **Step 5: Record**

Append to section 21:

```
### 21.2 Standalone + client in one private network (Task 2, probes/mp_match_probe.sh)
- Server inside/outside listener counts: <3/4/5 lines verbatim>
- Client with -port 7809 joined: <yes/no; states in order with times>
- -almission consumer: <client (first player = host) | standalone>; mission reached <state>
- Server log on join: <line>
- IPv6: -connect 127.0.0.1 worked: <yes/no>; [::1] needed: <yes/no>

### 21.3 Bridge heartbeat in multiplayer states (Task 2 step 4)
- States where the frame hook kept writing: <list>
- States where it did not: <list or "none">
```

Add the README bullet: `` - `mp_match_probe.sh [mission]` — standalone + client inside one private network; the facts behind the match module (section 21.2-21.3). ``

- [ ] **Step 6: Commit**

```bash
git add probes/mp_match_probe.sh probes/README.md docs/research/2026-10-12-probe-results.md
git commit -m "Spike: standalone and client in one private network (probe-results 21.2-21.3)"
```

If line 4 ever printed anything but 0, or the client never joined even with `[::1]`, STOP here: the spec (§8) says the slice stops and the finding goes to Dustin.

---

### Task 3: The match module

**Files:**
- Create: `fsoctl/match.py`
- Modify: `fsoctl/paths.py` (add `matches_root()`, `match_dir(mid)`)
- Test: `tests/test_match_unit.py`

**Interfaces:**
- Consumes: `scope.start(unit, argv, env, log_path, props) -> Popen`, `scope.caps(...)`, `scope.is_active(unit)`, `scope.stop(unit)`, `scope.is_gone(unit)`, `paths.runtime_root()`, `paths.state_dir()`, `session.list_sessions()`.
- Produces (used by Tasks 4-9):
  - `@dataclass Match(id: str, scope: str, holder_pid: int | None, holder_start: int | None, created: float, label: str, status: str)` with status in `"starting" | "alive" | "dead" | "removed"`.
  - `HOLDER_ARGV: list[str]`
  - `create(label: str = "") -> Match`
  - `load(mid: str) -> Match` (EXIT_NOT_FOUND)
  - `save(m: Match) -> None`
  - `list_matches() -> list[Match]`
  - `pid_start(pid: int) -> int | None`
  - `alive(m: Match) -> bool`
  - `nsenter_prefix(m: Match) -> list[str]` (raises EXIT_REFUSED when dead)
  - `run_inside(m: Match, argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess`
  - `end_holder(m: Match) -> None`
  - `audit(m: Match, kind: str, text: str) -> None`
  - `paths.matches_root() -> Path`, `paths.match_dir(mid) -> Path` (validates `^m[0-9a-f]{8}$`)

- [ ] **Step 1: Write the failing tests**

`tests/test_match_unit.py`:

```python
"""Unit tests for fsoctl.match: records, dead-holder detection, nsenter prefix. The real holder
(systemd + unshare) is exercised in tests/test_multi_engine.py; here scope is stubbed."""
import os
import subprocess
import time

import pytest

from fsoctl import match, paths, scope
from fsoctl.errors import EXIT_NOT_FOUND, EXIT_REFUSED, HarnessError


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid


def _stub_scope(monkeypatch, pid, active=True):
    """Make scope.start 'start' a holder whose pid is `pid` and report its scope active."""
    monkeypatch.setattr(scope, "start", lambda unit, argv, env, log, props: _FakeProc(pid))
    monkeypatch.setattr(scope, "is_active", lambda unit: active)
    monkeypatch.setattr(scope, "state", lambda unit: "active" if active else "inactive")
    # The post-start check runs `ip -brief link show lo` inside the namespace; stub it to "UP".
    monkeypatch.setattr(match, "run_inside", lambda m, argv, timeout=10: subprocess.CompletedProcess(
        argv, 0, stdout="lo  UNKNOWN  00:00:00:00:00:00 <LOOPBACK,UP,LOWER_UP>\n", stderr=""))


def test_pid_start_reads_field_22_of_proc_stat():
    me = match.pid_start(os.getpid())
    assert isinstance(me, int) and me > 0
    assert match.pid_start(2**22 + 12345) is None     # no such pid


def test_create_records_holder_and_is_alive(tmp_runtime, monkeypatch):
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        _stub_scope(monkeypatch, sleeper.pid)
        m = match.create(label="t")
        assert m.id.startswith("m") and len(m.id) == 9
        assert m.holder_pid == sleeper.pid
        assert m.holder_start == match.pid_start(sleeper.pid)
        assert m.status == "alive" and m.label == "t"
        assert match.load(m.id).holder_pid == sleeper.pid
        assert [x.id for x in match.list_matches()] == [m.id]
        assert match.alive(m)
        assert match.nsenter_prefix(m)[:6] == ["nsenter", "--preserve-credentials", "--user",
                                                "--net", "--target", str(sleeper.pid)]
    finally:
        sleeper.kill()
        sleeper.wait()


def test_dead_holder_detected_by_pid_start(tmp_runtime, monkeypatch):
    sleeper = subprocess.Popen(["sleep", "30"])
    _stub_scope(monkeypatch, sleeper.pid)
    m = match.create()
    sleeper.kill()
    sleeper.wait()
    assert not match.alive(m)            # pid gone
    m.holder_pid = os.getpid()           # a reused pid with a different start time
    assert not match.alive(m)
    with pytest.raises(HarnessError) as e:
        match.nsenter_prefix(m)
    assert e.value.code == EXIT_REFUSED


def test_inactive_scope_means_dead_even_with_live_pid(tmp_runtime, monkeypatch):
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        _stub_scope(monkeypatch, sleeper.pid, active=True)
        m = match.create()
        monkeypatch.setattr(scope, "is_active", lambda unit: False)
        assert not match.alive(m)
    finally:
        sleeper.kill()
        sleeper.wait()


def test_load_unknown_and_bad_ids(tmp_runtime):
    with pytest.raises(HarnessError) as e:
        match.load("m00000000")
    assert e.value.code == EXIT_NOT_FOUND
    with pytest.raises(HarnessError):
        paths.match_dir("../etc")


def test_refuses_root(tmp_runtime, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 0)
    with pytest.raises(HarnessError) as e:
        match.create()
    assert e.value.code == EXIT_REFUSED


def test_audit_writes_persistent_line(tmp_runtime, monkeypatch):
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        _stub_scope(monkeypatch, sleeper.pid)
        m = match.create()
        text = (paths.state_dir() / "audit.log").read_text()
        assert m.id in text and '"kind": "match_create"' in text
    finally:
        sleeper.kill()
        sleeper.wait()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `bounded-run pytest tests/test_match_unit.py -q`
Expected: ImportError / AttributeError on `fsoctl.match` and `paths.match_dir` (collection error counts).

- [ ] **Step 3: Implement**

`fsoctl/paths.py`, after `session_dir`:

```python
_MID_RE = re.compile(r"^m[0-9a-f]{8}$")


def matches_root() -> Path:
    """Return the folder that holds one subfolder per match (created, under the runtime root)."""
    root = runtime_root() / "matches"
    root.mkdir(exist_ok=True)
    return root


def match_dir(mid: str) -> Path:
    """Return the folder for match `mid` (validated id; the folder may not exist yet)."""
    if not _MID_RE.match(mid):
        raise HarnessError(EXIT_NOT_FOUND, f"not a match id: {mid!r}", "use `fsoctl match list`")
    return matches_root() / mid
```

`fsoctl/match.py`:

```python
"""Matches: one private, loopback-only network shared by the sessions of a multiplayer game.

WHY a namespace and not firewall rules: the engine binds its UDP game port on every interface
and has no bind-address option (psnet_init_socket, [::]:7808), and the standalone server opens a
web panel on TCP 8080. Rules on the real loopback need root and leave the ports on the LAN between
rule changes. An unprivileged user+network namespace (unshare --user --map-root-user --net) gives
the engine a network with only loopback and no route out; the kernel enforces it, no root is
needed, and nothing exists on the real network at any moment. Probed 2026-10-12 (spec section 3).

WHY a holder process: a network namespace lives as long as something holds it. The holder is a
`sleep infinity` inside the namespace, in its own systemd scope so `stop` and `status` can find
it, and its pid is what sessions join through (`nsenter --target <pid>`). The namespace dies with
the holder, which is exactly the lifetime the spec wants (section 5.6).

Records live in <runtime root>/matches/<id>/match.json, read fresh by every verb (no daemon).
"""
import json
import os
import secrets
import subprocess
import time
from dataclasses import asdict, dataclass, field

from . import paths, scope
from .errors import EXIT_LAUNCH, EXIT_NOT_FOUND, EXIT_REFUSED, HarnessError

# --map-root-user, not --map-current-user: only the namespace's own root may configure loopback
# (probed 2026-10-12: --map-current-user fails with "Operation not permitted" on `ip link set lo up`).
# The uid-0 inside maps to the real user outside; it carries no privilege on the host.
HOLDER_ARGV = ["unshare", "--user", "--map-root-user", "--net", "sh", "-c",
               "ip link set lo up && exec sleep infinity"]
HOLDER_RUNTIME_SEC = 24 * 3600      # a match never outlives a day; its sessions have their own caps
_HOLDER_MEM_KB = 131072             # 128 MiB is far more than a sleep needs
_LO_UP_TIMEOUT = 5.0


@dataclass
class Match:
    """One private network and what is known about its holder."""
    id: str
    scope: str
    holder_pid: int | None = None
    # /proc/<pid>/stat field 22 (start time in clock ticks since boot): with the pid it makes a
    # handle no later process can reuse by accident (spec section 6).
    holder_start: int | None = None
    created: float = field(default_factory=time.time)
    label: str = ""
    status: str = "starting"      # starting | alive | dead | removed


def scope_name(mid: str) -> str:
    """Return the holder's systemd scope name (without ".scope")."""
    return f"fso-harness-match-{mid}"


def pid_start(pid: int) -> int | None:
    """Return the process's start time (clock ticks since boot) from /proc, or None if it is gone.

    /proc/<pid>/stat: "pid (comm) state ..." and comm may contain spaces or parentheses, so the
    fields are taken after the LAST ')'; field 22 of the man page is then index 19.
    """
    try:
        text = open(f"/proc/{pid}/stat").read()
    except OSError:
        return None
    rest = text.rsplit(")", 1)[1].split()
    return int(rest[19])


def save(m: Match) -> None:
    """Write match.json atomically (temp file + rename), creating the match folder."""
    d = paths.match_dir(m.id)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "match.json.tmp"
    tmp.write_text(json.dumps(asdict(m), indent=1))
    os.replace(tmp, d / "match.json")


def load(mid: str) -> Match:
    """Read a match by id; EXIT_NOT_FOUND when there is none."""
    f = paths.match_dir(mid) / "match.json"
    if not f.exists():
        raise HarnessError(EXIT_NOT_FOUND, f"no match {mid}", "use `fsoctl match list`")
    return Match(**json.loads(f.read_text()))


def list_matches() -> list[Match]:
    """Return every match record under the runtime root, oldest first."""
    out = []
    for d in sorted(paths.matches_root().iterdir()):
        f = d / "match.json"
        if f.exists():
            out.append(Match(**json.loads(f.read_text())))
    return sorted(out, key=lambda m: m.created)


def audit(m: Match, kind: str, text: str) -> None:
    """Append one JSON line to the persistent audit log: a private network is a resource worth a
    record (spec section 7), and the runtime copy would vanish with the match folder."""
    with open(paths.state_dir() / "audit.log", "a") as f:
        f.write(json.dumps({"t": time.time(), "mid": m.id, "kind": kind, "text": text}) + "\n")


def alive(m: Match) -> bool:
    """True while the holder's scope is active AND the recorded pid still has the recorded start
    time. Both checks: the scope alone cannot tell a reused pid; the pid alone cannot tell a scope
    systemd is tearing down."""
    if m.holder_pid is None or m.holder_start is None or m.status == "removed":
        return False
    return scope.is_active(m.scope) and pid_start(m.holder_pid) == m.holder_start


def nsenter_prefix(m: Match) -> list[str]:
    """Return the command prefix that runs something inside the match's network.

    --preserve-credentials keeps the caller's uid/gid instead of becoming the namespace's root
    (the engine should not think it is root); --user is needed because the network namespace is
    owned by the user namespace. Refuses (EXIT_REFUSED) a dead match: joining a dead holder's
    namespace is impossible, and a reused pid would join the wrong one.
    """
    if not alive(m):
        raise HarnessError(EXIT_REFUSED, f"match {m.id} is dead (holder gone)",
                           "create a new match with `fsoctl match create`")
    return ["nsenter", "--preserve-credentials", "--user", "--net", "--target",
            str(m.holder_pid), "--"]


def run_inside(m: Match, argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess:
    """Run `argv` inside the match's network and return the completed process (text output)."""
    return subprocess.run(nsenter_prefix(m) + argv, capture_output=True, text=True,
                          timeout=timeout, check=False)


def _wait_loopback(m: Match) -> None:
    """Block (bounded) until loopback is up inside the new namespace; raise EXIT_LAUNCH otherwise."""
    deadline = time.monotonic() + _LO_UP_TIMEOUT
    while time.monotonic() < deadline:     # bounded by the deadline
        r = run_inside(m, ["ip", "-brief", "link", "show", "lo"])
        if r.returncode == 0 and "UP" in r.stdout:
            return
        time.sleep(0.1)
    raise HarnessError(EXIT_LAUNCH, f"loopback never came up inside match {m.id}",
                       "is `unshare --user --net` allowed? check /proc/sys/user/max_user_namespaces")


def create(label: str = "") -> Match:
    """Start a holder in its own scope and record the match; return it once loopback is up.

    Refuses to run as the real root (spec section 5.4): the whole design is unprivileged, and a
    root holder would own a namespace with real capabilities.
    """
    if os.getuid() == 0:
        raise HarnessError(EXIT_REFUSED, "fsoctl match verbs refuse to run as root",
                           "run as your normal user")
    for _ in range(16):  # bounded: 16 collisions in 2^32 is effectively impossible
        mid = "m" + secrets.token_hex(4)
        if not paths.match_dir(mid).exists():
            break
    else:
        raise HarnessError(EXIT_NOT_FOUND, "could not allocate a match id", "retry")
    m = Match(id=mid, scope=scope_name(mid), label=label)
    save(m)
    props = scope.caps(runtime_sec=HOLDER_RUNTIME_SEC, cpu_cores=1, tasks=16, mem_kb=_HOLDER_MEM_KB)
    proc = scope.start(m.scope, HOLDER_ARGV, dict(os.environ), paths.match_dir(mid) / "holder.log",
                       props)
    # unshare execs sh, and sh execs sleep, so the scope's command pid IS the holder.
    m.holder_pid, m.holder_start = proc.pid, pid_start(proc.pid)
    m.status = "alive"
    save(m)
    try:
        _wait_loopback(m)
    except HarnessError:
        end_holder(m)
        raise
    audit(m, "match_create", f"holder pid {m.holder_pid} scope {m.scope} label {label!r}")
    return m


def end_holder(m: Match) -> None:
    """Stop the holder's scope (the network dies with it) and mark the match removed."""
    scope.stop(m.scope)
    m.status = "removed"
    save(m)
    audit(m, "match_remove", f"holder pid {m.holder_pid}")
```

Check `scope.stop`'s name and signature in `fsoctl/scope.py` before using it (it is called `stop(unit)` in launcher.stop; confirm). Check `scope.caps` accepts `mem_kb` (it does: `caps(runtime_sec, cpu_cores, tasks, mem_kb)`).

- [ ] **Step 4: Run the tests**

Run: `bounded-run pytest tests/test_match_unit.py -q`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add fsoctl/match.py fsoctl/paths.py tests/test_match_unit.py
git commit -m "Add match records and the private-network holder"
```

---

### Task 4: `fsoctl match` verbs and the MCP tools

**Files:**
- Modify: `fsoctl/match.py` (CLI verb at the bottom)
- Modify: `fsoctl/cli.py:23-25` (`_VERB_MODULES` gains `"match"`)
- Modify: `fsoctl/launcher.py` (new `remove_match(mid)`; `_cleanup_locked` gains dead-match removal)
- Modify: `mcp/server.py` (tools `match_create`, `match_list`, `match_remove`)
- Test: `tests/test_match_unit.py` (CLI), `tests/test_mcp_wrapper.py` (tool mapping, read its existing pattern first)

**Interfaces:**
- Consumes: Task 3's module; `cli.register(name, help, configure)`; `launcher.stop(sid)`; `session.list_sessions()`; `mcp/server.py`'s `_ctl(*args, timeout=...)` and `_opt(name, value)`.
- Produces: `fsoctl match create [--label L]` -> `{"id", "holder_pid", "scope", "status"}`; `fsoctl match list` -> `{"matches": [{id, status, alive, label, holder_pid, sessions: [ids]}]}`; `fsoctl match remove <id>` -> `{"id", "stopped": [sids], "status": "removed"}`; `launcher.remove_match(mid) -> dict`; `launcher.sessions_in_match(mid) -> list[Session]` (needs Task 5's `Session.match` field; until then it returns `[]`, see the ruling note below).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_match_unit.py`:

```python
from fsoctl import cli, launcher, session  # noqa: E402  (test file; grouped at the top in the real file)


def test_cli_match_create_list_remove(tmp_runtime, monkeypatch, capsys):
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        _stub_scope(monkeypatch, sleeper.pid)
        stopped = []
        monkeypatch.setattr(scope, "stop", lambda unit: stopped.append(unit))
        rc = cli.main(["--json", "match", "create", "--label", "repro"])
        out = capsys.readouterr().out
        assert rc == 0 and '"holder_pid": %d' % sleeper.pid in out
        mid = json.loads(out)["id"]
        cli.main(["--json", "match", "list"])
        listed = json.loads(capsys.readouterr().out)["matches"]
        assert listed[0]["id"] == mid and listed[0]["alive"] is True and listed[0]["sessions"] == []
        rc = cli.main(["--json", "match", "remove", mid])
        assert rc == 0 and stopped == [match.scope_name(mid)]
        assert match.load(mid).status == "removed"
    finally:
        sleeper.kill()
        sleeper.wait()


def test_cli_match_bad_action(tmp_runtime):
    with pytest.raises(HarnessError) as e:
        cli.main(["--json", "match", "frobnicate"])
    assert e.value.code == EXIT_USAGE
```

(Add `import json` and `from fsoctl.errors import EXIT_USAGE` at the top. Read `tests/test_b_cli.py` first for how `cli.main` is invoked and whether it returns or raises on usage errors, and match that.)

- [ ] **Step 2: Run to verify they fail**

Run: `bounded-run pytest tests/test_match_unit.py -q -k cli`
Expected: FAIL, `match` is not a verb (`argparse` unknown command / HarnessError EXIT_USAGE naming the verb list).

- [ ] **Step 3: Implement the verb and remove_match**

Append to `fsoctl/match.py`:

```python
# ---- command line ----------------------------------------------------------------------------
import argparse  # noqa: E402  (verb registration lives at the bottom, as in launcher.py)

from .cli import register  # noqa: E402


def _configure_match(p: argparse.ArgumentParser) -> None:
    """`match create [--label L]` | `match list` | `match remove <id>`."""
    p.add_argument("action", choices=("create", "list", "remove"))
    p.add_argument("id", nargs="?", help="match id (remove)")
    p.add_argument("--label", default="", help="free text shown by `match list`")


@register("match", "create, list or remove a private multiplayer network", _configure_match)
def _cli_match(args: argparse.Namespace) -> dict:
    """Dispatch the match actions. `remove` lives in launcher (it stops sessions first)."""
    from . import launcher   # local import: launcher imports this module
    if args.action == "create":
        m = create(args.label)
        return {"id": m.id, "holder_pid": m.holder_pid, "scope": m.scope, "status": m.status}
    if args.action == "list":
        return {"matches": [{"id": m.id, "status": m.status, "alive": alive(m), "label": m.label,
                             "holder_pid": m.holder_pid,
                             "sessions": [s.id for s in launcher.sessions_in_match(m.id)]}
                            for m in list_matches()]}
    if not args.id:
        raise HarnessError(EXIT_USAGE, "match remove needs an id", "fsoctl match remove <id>")
    return launcher.remove_match(args.id)
```

(`EXIT_USAGE` joins the errors import at the top.) In `fsoctl/cli.py` add `"match"` to `_VERB_MODULES`.

In `fsoctl/launcher.py` (after `stop`):

```python
def sessions_in_match(mid: str) -> list[session.Session]:
    """Return the sessions launched into match `mid` (field added with the roles; Task 5)."""
    return [s for s in session.list_sessions() if getattr(s, "match", None) == mid]


def remove_match(mid: str) -> dict:
    """Stop every session in the match (normal stop path, runner first), then end the holder.

    WHY sessions first (spec section 5.6): an engine must never outlive its network; ending the
    holder first would leave engines in a namespace nothing can enter to stop them cleanly.
    """
    m = match.load(mid)
    stopped = []
    for s in sessions_in_match(mid):
        if s.status in ("starting", "running"):
            stop(s.id)
            stopped.append(s.id)
    match.end_holder(m)
    return {"id": mid, "stopped": stopped, "status": "removed"}
```

`from . import match` joins launcher's imports. In `_cleanup_locked`, after the session loop:

```python
    for m in match.list_matches():
        # A dead or removed match with no session left is just a folder; same age rule as sessions.
        if match.alive(m) or sessions_in_match(m.id) or now - m.created < older_than_sec:
            continue
        try:
            shutil.rmtree(paths.match_dir(m.id))
            removed.append(m.id)
        except OSError as e:
            failed.append({"path": str(paths.match_dir(m.id)), "error": str(e)})
```

MCP (`mcp/server.py`, next to `cleanup`):

```python
@mcp.tool()
def match_create(label: str = "") -> str:
    """Create a private, loopback-only network for one multiplayer game; returns its id. Launch
    the server and clients into it with launch(match=..., role=...). Nothing in it is reachable
    from the LAN or the internet. Remove it with match_remove when done."""
    return _ctl("match", "create", *_opt("label", label or None))


@mcp.tool()
def match_list() -> str:
    """List private multiplayer networks and the sessions in each."""
    return _ctl("match", "list")


@mcp.tool()
def match_remove(id: str) -> str:
    """Stop every session in a match, then end its private network."""
    return _ctl("match", "remove", id)
```

- [ ] **Step 4: Run the tests and the MCP check**

Run: `bounded-run pytest tests/test_match_unit.py tests/test_mcp_wrapper.py tests/test_b_cli.py -q && python3 tests/mcp_check.py`
Expected: all pass; mcp_check lists the three new tools.

- [ ] **Step 5: Commit**

```bash
git add fsoctl/match.py fsoctl/cli.py fsoctl/launcher.py mcp/server.py tests/test_match_unit.py
git commit -m "Add fsoctl match verbs, remove_match ordering, and MCP match tools"
```

---

### Task 5: Roles: request and session fields, validation, engine arguments, multi.cfg

**Files:**
- Modify: `fsoctl/launcher.py:49-71` (LaunchRequest), `:96-113` (build_argv), `:243-263` (_validate), `:273-307` (_prepare), `:756-791` (flags + _cli_launch)
- Modify: `fsoctl/session.py:25-54` (Session fields)
- Modify: `mcp/server.py:184-215` (launch tool params)
- Modify: `docs/superpowers/specs/2026-10-12-fso-harness-multiplayer-design.md` §4 (port rule, see Step 0)
- Test: `tests/test_launcher_unit.py`

**Interfaces:**
- Consumes: Task 3 (`match.load`, `match.alive`), Task 4 (`sessions_in_match`), Task 2's facts (who consumes `-almission`).
- Produces: `LaunchRequest.match: str | None`, `.role: str | None`, `.port: int | None`, `.connect: str`, `.game_name: str | None`, `.closed: bool`; `Session.match`, `.role`, `.port`, `.connect`, `.game_name`, `.closed` (same types, defaults `None/None/None/"127.0.0.1:7808"/None/False`); `ROLES = ("standalone", "host", "client")`; `DEFAULT_GAME_PORT = 7808`; `assign_port(mid: str, wanted: int | None) -> int`; `multi_cfg_text(game_name: str) -> str`; switches `pilot_multi: bool` and `multi_role: str | None` in `switches.txt` (consumed by Task 6's bridge change).

- [ ] **Step 0: Spec amendment (plan deviation, recorded)**

Spec §4 says `--port` default 7808 for every role. Every engine process binds `[::]:<port>` (psnet), so two sessions in one namespace cannot share it (Review Focus 1). Amend §4's sentence to: "`--port` (default: 7808 for the first session in the match, then the next free port 7809, 7810...; a duplicate inside the match is refused)". Commit the amendment with this task.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_launcher_unit.py`:

```python
def _match_alive(monkeypatch, mid="m0a1b2c3d"):
    """Pretend `mid` exists and is alive without a holder."""
    m = match.Match(id=mid, scope=match.scope_name(mid), holder_pid=1, holder_start=1, status="alive")

    def fake_load(x):
        if x == mid:
            return m
        raise HarnessError(EXIT_NOT_FOUND, f"no match {x}", "use `fsoctl match list`")
    monkeypatch.setattr(match, "load", fake_load)
    monkeypatch.setattr(match, "alive", lambda x: True)
    return m


def test_argv_standalone(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    s = _sess(mission=None, role="standalone", match="m0a1b2c3d", port=7808, extra_args=[])
    argv = launcher.build_argv(s)
    assert "-standalone" in argv and argv[argv.index("-port") + 1] == "7808"
    for flag in ("-window", "-res", "-connect", "-startgame", "-almission"):
        assert flag not in argv
    assert "-nosound" in argv and "-noninteractive" in argv
    assert argv[1:3] == ["-mod", f"{paths.BRIDGE_MOD},MVPS"]


def test_argv_host_and_client(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    h = _sess(mission="m-01.fs2", role="host", match="m0a1b2c3d", port=7808, game_name="g",
              closed=True, extra_args=[])
    argv = launcher.build_argv(h)
    assert "-startgame" in argv and "-closed" in argv
    assert argv[argv.index("-gamename") + 1] == "g"
    assert argv[argv.index("-almission") + 1] == "m-01"      # the flag takes the name, no .fs2
    assert "-start_mission" not in argv and "-window" in argv
    c = _sess(mission=None, role="client", match="m0a1b2c3d", port=7809,
              connect="127.0.0.1:7808", extra_args=[])
    argv = launcher.build_argv(c)
    assert argv[argv.index("-connect") + 1] == "127.0.0.1:7808"
    assert argv[argv.index("-port") + 1] == "7809" and "-startgame" not in argv


def test_multi_cfg_never_enables_tracker():
    text = launcher.multi_cfg_text("g")
    assert "+name g" in text and "+pxo" not in text and "+webapi_server_port 8080" in text


def test_role_validation(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    base = dict(binary="/bin/true")
    for bad, code in ((dict(role="client"), EXIT_USAGE),                       # role without match
                      (dict(match="m0a1b2c3d"), EXIT_USAGE),                   # match without role
                      (dict(match="m0a1b2c3d", role="server"), EXIT_USAGE),    # bad role
                      (dict(match="m0a1b2c3d", role="client", kind="qtfred"), EXIT_USAGE),
                      (dict(match="m00000000", role="client"), EXIT_NOT_FOUND)):
        with pytest.raises(HarnessError) as e:
            launcher._validate(LaunchRequest(**base, **bad))
        assert e.value.code == code, bad


def test_assign_port_skips_ports_used_in_match(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    _sess(role="standalone", match="m0a1b2c3d", port=7808)
    _sess(role="client", match="m0a1b2c3d", port=7809)
    assert launcher.assign_port("m0a1b2c3d", None) == 7810
    with pytest.raises(HarnessError) as e:
        launcher.assign_port("m0a1b2c3d", 7809)
    assert e.value.code == EXIT_USAGE
    assert launcher.assign_port("mffffffff", None) == 7808   # an empty match starts at the default


def test_prepare_writes_multi_cfg_and_switches(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    monkeypatch.setattr(launcher.bridge_install, "install", lambda fs2: {"updated": False})
    monkeypatch.setenv("FS2PATH", str(tmp_runtime))
    req = LaunchRequest(binary="/bin/true", match="m0a1b2c3d", role="standalone")
    sess, _ = launcher._prepare(req)
    assert (session.prefs_dir(sess) / "data" / "multi.cfg").read_text().startswith("+name m0a1b2c3d")
    req = LaunchRequest(binary="/bin/true", match="m0a1b2c3d", role="client", pilot_mode="create")
    sess, _ = launcher._prepare(req)
    sw = json.loads((paths.channel_dir(session.prefs_dir(sess)) / "switches.txt").read_text())
    assert sw["pilot_multi"] is True and sw["multi_role"] == "client"
    assert not (session.prefs_dir(sess) / "data" / "multi.cfg").exists()
```

Imports to add at the top of the test file: `import json`, `from fsoctl import match`, `from fsoctl.errors import EXIT_NOT_FOUND, EXIT_USAGE, HarnessError`, `from fsoctl.launcher import LaunchRequest`. `_sess` passes `**sw` into `session.new_session`, so the new Session fields must exist for these to construct.

- [ ] **Step 2: Run to verify they fail**

Run: `bounded-run pytest tests/test_launcher_unit.py -q`
Expected: new tests FAIL (TypeError: unexpected keyword `role` / AttributeError `multi_cfg_text`); old ones pass.

- [ ] **Step 3: Implement**

`fsoctl/session.py`, Session, after `memory_max`:

```python
    # Multiplayer (spec section 4): the match this session joined, its role, the local UDP port
    # the engine binds, and for clients the server address. None/False for ordinary sessions.
    match: str | None = None
    role: str | None = None           # "standalone" | "host" | "client"
    port: int | None = None
    connect: str = "127.0.0.1:7808"
    game_name: str | None = None
    closed: bool = False
```

`fsoctl/launcher.py`:

```python
ROLES = ("standalone", "host", "client")
DEFAULT_GAME_PORT = 7808                      # DEFAULT_GAME_PORT in psnet2.h
_SWITCH_NAMES = (..., "pilot_multi", "multi_role")   # extend the existing tuple
```

LaunchRequest gains (after `cwd`):

```python
    match: str | None = None             # a `fsoctl match create` id; required with role
    role: str | None = None              # one of ROLES
    port: int | None = None              # local UDP port; None = next free in the match
    connect: str = "127.0.0.1:7808"      # client: the server address
    game_name: str | None = None         # host/standalone: shown in the join list; None = match id
    closed: bool = False                 # host: -closed

    @property
    def pilot_multi(self) -> bool:
        """Multiplayer roles create multiplayer pilots (a single-player pilot cannot enter multi)."""
        return self.role in ("host", "client")

    @property
    def multi_role(self) -> str | None:
        """What the bridge needs to know: it posts the join-screen event only for clients."""
        return self.role
```

`_check_role(req)` called from `_validate` (before the pilot checks):

```python
def _check_role(req: LaunchRequest) -> None:
    """Refuse a multiplayer request that is incomplete or points at a dead match."""
    if req.role is None and req.match is None:
        return
    if req.role is None or req.match is None:
        raise HarnessError(EXIT_USAGE, "--match and --role go together",
                           "fsoctl match create, then launch --match <id> --role standalone|host|client")
    if req.role not in ROLES:
        raise HarnessError(EXIT_USAGE, f"unknown role {req.role!r}", f"use one of {', '.join(ROLES)}")
    if req.kind != "game":
        raise HarnessError(EXIT_USAGE, "qtFRED has no multiplayer role", "drop --match/--role")
    m = match.load(req.match)                   # EXIT_NOT_FOUND
    if not match.alive(m):
        raise HarnessError(EXIT_REFUSED, f"match {req.match} is dead",
                           "create a new one with `fsoctl match create`")
    if req.role == "standalone" and req.pilot_source:
        raise HarnessError(EXIT_USAGE, "a standalone server has no pilot", "drop --pilot-source")


def assign_port(mid: str, wanted: int | None) -> int:
    """Return the local port for a new session in match `mid`: `wanted` if free there, else the
    first free port from DEFAULT_GAME_PORT up. Every engine binds [::]:<port>, so two sessions in
    one namespace cannot share a port (Review Focus 1)."""
    used = {s.port for s in sessions_in_match(mid) if s.port}
    if wanted is not None:
        if wanted in used:
            raise HarnessError(EXIT_USAGE, f"port {wanted} is already used in match {mid}",
                               "leave --port unset to take the next free one")
        return wanted
    port = DEFAULT_GAME_PORT
    while port in used:          # bounded by the session cap (at most a handful of ports)
        port += 1
    return port


def multi_cfg_text(game_name: str) -> str:
    """Return the standalone's multi.cfg: a name and the (private) web panel port; never +pxo."""
    return f"+name {game_name}\n+webapi_server_port 8080\n"
```

`build_argv` (replace the game branch):

```python
    sw = sess.switches
    argv = [sess.binary, "-mod", ",".join([paths.BRIDGE_MOD, *sess.mods]), "-nosound"]
    if sess.role != "standalone":
        # The standalone has no window (stand_gui-unix.cpp); everything else draws into Xvfb.
        argv += ["-window", "-res", f"{sess.width}x{sess.height}", "-no_unfocused_pause"]
    if sw.get("noninteractive", True):
        argv.append("-noninteractive")
    if sw.get("skip_movies"):
        argv.append("-nomovies")
    if sw.get("pilot_mode") == "existing":
        argv += ["-pilot", sw.get("pilot_name", "HarnessPilot")]
    if sess.role is None and sess.mission:
        argv += ["-start_mission", sess.mission]
    if sess.role is not None:
        argv += _multi_argv(sess)
    if sess.renderer == "vulkan":
        argv.append("-vulkan")
    return argv + sess.extra_args


def _multi_argv(sess: session.Session) -> list[str]:
    """Return the role's engine flags (spec section 4; cmdline.cpp:210-303, 429)."""
    name = sess.game_name or sess.match
    out = ["-port", str(sess.port)]
    if sess.role == "standalone":
        out.insert(0, "-standalone")
    elif sess.role == "host":
        out += ["-startgame", "-gamename", name]
        if sess.closed:
            out.append("-closed")
    else:
        out += ["-connect", sess.connect]
    if sess.mission:
        # -almission takes the mission NAME; the engine adds .fs2 (multiui.cpp:3674).
        out += ["-almission", sess.mission[:-4] if sess.mission.endswith(".fs2") else sess.mission]
    return out
```

`_prepare`: pass the new fields into `new_session(..., match=req.match, role=req.role, port=assign_port(req.match, req.port) if req.match else None, connect=req.connect, game_name=req.game_name, closed=req.closed)`; keep `switches={k: getattr(req, k) for k in _SWITCH_NAMES}` (the two properties make `pilot_multi`/`multi_role` resolve). After the channel/switches block add:

```python
        if req.role == "standalone":
            cfg = session.prefs_dir(sess) / "data" / "multi.cfg"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(multi_cfg_text(req.game_name or req.match))
```

Ruling to apply from Task 2: if 21.2 showed the STANDALONE consumes `-almission`, keep `_multi_argv` as written (every role passes it); if only the first client (host) does, make `_check_role` refuse `mission` with `role == "standalone"` (EXIT_USAGE, "give --mission to the host or the first client") and add that case to `test_role_validation`. Ledger the ruling.

CLI flags (`_configure_launch`): `--match`, `--role` (choices ROLES), `--port` (int), `--connect` (default "127.0.0.1:7808"), `--game-name`, `--closed` (store_true); `_cli_launch` passes them. MCP `launch` gains `match: str | None = None, role: str | None = None, port: int | None = None, connect: str = "127.0.0.1:7808", game_name: str | None = None, closed: bool = False` and the matching `*_opt(...)` entries; extend its docstring with one sentence: "Multiplayer: create a match, then launch role=standalone|host|client with match=<id>; clients auto-join connect (default the match's server)."

- [ ] **Step 4: Run the tests**

Run: `bounded-run pytest tests/test_launcher_unit.py tests/test_session.py tests/test_b_session.py tests/test_mcp_wrapper.py -q && python3 tests/mcp_check.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add fsoctl/launcher.py fsoctl/session.py mcp/server.py tests/test_launcher_unit.py docs/superpowers/specs/2026-10-12-fso-harness-multiplayer-design.md
git commit -m "Add multiplayer roles: request fields, validation, engine flags, multi.cfg"
```

---

### Task 6: Runner inside the match network; no Xvfb for a standalone; bridge multiplayer switches

**Files:**
- Modify: `fsoctl/launcher.py:365-388` (`_start`: nsenter prefix)
- Modify: `fsoctl/runner.py:253-289` (`main`: skip Xvfb for standalone)
- Modify: `fsoctl/envsetup.py:35` (`child_env` accepts `display: str | None`)
- Modify: `fsoctl/bridge/fso_harness_bridge/data/scripts/fsoharness.lua:166-171` (switch status), `:1283-1294` (pilot hook), new main-hall hook
- Test: `tests/test_launcher_unit.py`, `tests/test_b_runner.py` (read its fixtures first), `tests/test_bridge_lua.py` (read how it drives the Lua; if it cannot host these hooks, the engine test in Task 9 covers them)

**Interfaces:**
- Consumes: `match.nsenter_prefix(m)`; Task 5's `Session.role`, switches `pilot_multi`, `multi_role`; Task 1's result (the join mechanism).
- Produces: runner argv `nsenter ... -- python3 -m fsoctl.runner <sid>` for match sessions; `Session.display is None` for standalone; bridge switch outcomes `pilot_mode` (now multi-aware) and `multi_join` ("pending" -> "done"/"failed: ...") in the heartbeat's `switches`.

- [ ] **Step 1: Write the failing tests**

`tests/test_launcher_unit.py`:

```python
def test_start_wraps_runner_in_nsenter_for_match_sessions(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    monkeypatch.setattr(match, "nsenter_prefix", lambda m: ["nsenter", "--target", "1", "--"])
    monkeypatch.setattr(launcher.bridge_install, "install", lambda fs2: {"updated": False})
    monkeypatch.setenv("FS2PATH", str(tmp_runtime))
    seen = {}

    def fake_start(unit, argv, env, log, props):
        seen["argv"] = argv
        return None
    monkeypatch.setattr(launcher.scope, "start", fake_start)
    monkeypatch.setattr(launcher, "check_capacity", lambda: [])
    launcher._start(LaunchRequest(binary="/bin/true", match="m0a1b2c3d", role="client"))
    assert seen["argv"][:4] == ["nsenter", "--target", "1", "--"]
    assert seen["argv"][4:7] == [sys.executable, "-m", "fsoctl.runner"]
    seen.clear()
    launcher._start(LaunchRequest(binary="/bin/true"))
    assert seen["argv"][0] == sys.executable          # ordinary sessions are not wrapped
```

`tests/test_b_runner.py` (follow its existing pattern for running `runner.main` with a fake engine binary; the test asserts): a session with `role="standalone"` runs `main` without starting Xvfb (monkeypatch `runner._start_xvfb` to raise `AssertionError("xvfb started")`), the engine (`/bin/true` stand-in) runs, and `session.load(sid).display is None` with `env` lacking `DISPLAY` (capture via a stand-in script that writes `os.environ` to a file, as the file's other tests do).

Lua: if `tests/test_bridge_lua.py` can load the script with a stub `ui`/`ba`, add: with `switches = {pilot_mode="create", pilot_name="P", pilot_multi=true}` the state hook calls `createPilot("P", true)` and `selectPilot("P", true)`; with `multi_role="client"` the `GS_STATE_MAIN_MENU` hook posts `GS_EVENT_MULTI_JOIN_GAME` and sets `multi_join = "done"`. If the harness there cannot express it, record that in the ledger and rely on Task 9.

- [ ] **Step 2: Run to verify they fail**

Run: `bounded-run pytest tests/test_launcher_unit.py tests/test_b_runner.py -q -k "nsenter or standalone"`
Expected: FAIL (no nsenter prefix; Xvfb started / `display` set).

- [ ] **Step 3: Implement**

`launcher._start`, replace the `scope.start(...)` call:

```python
            runner_argv = [sys.executable, "-m", "fsoctl.runner", sess.id]
            if sess.match:
                # The runner, and so Xvfb and the engine, live in the match's network. Only the
                # network namespace changes: files, the X socket (a filesystem socket) and the
                # bridge channel are the same as for any session (spec section 4).
                runner_argv = match.nsenter_prefix(match.load(sess.match)) + runner_argv
            scope.start(sess.scope, runner_argv, _runner_env(),
                        session.run_dir(sess) / "runner.log", props)
```

`runner.main`: replace the Xvfb block:

```python
        display, xvfb = None, None
        if sess.role != "standalone":
            # A standalone server has no window (stand_gui-unix.cpp); give it no display at all
            # so a stray SDL init cannot open one.
            try:
                display, xvfb = _start_xvfb(sess.width, sess.height)
            except (OSError, RuntimeError) as e:
                return _fail(sid, f"xvfb: {e}")
        xvfb_pid = xvfb.pid if xvfb else None
```

and keep the rest; `envsetup.child_env(base, display: str | None, ...)`: when `display is None`, `env.pop("DISPLAY", None)` instead of setting it (read the function body for where DISPLAY is set and branch there).

Bridge (`fsoharness.lua`):

```lua
-- init_switch_status: add
	if H.switches.multi_role == "client" then H.switch_status.multi_join = "pending" end

-- pilot hook: replace the two ui.PilotSelect calls
	local multi = H.switches.pilot_multi == true
	local created = ui.PilotSelect.createPilot(name, multi)
	H.event("createPilot " .. name .. " multi=" .. tostring(multi) .. " -> " .. tostring(created))
	...
	ui.PilotSelect.selectPilot(name, multi)

-- new hook, after the pilot hook
-- Clients: the main hall's multiplayer button sets Game_mode and posts GS_EVENT_MULTI_JOIN_GAME
-- (mainhallmenu.cpp:380-387); nothing on the command line does that, and -connect only acts once
-- the join screen is up. Spike 21.1 showed <posting the event is enough | the click is needed>.
H.on_state("GS_STATE_MAIN_MENU", function()
	if H.switches.multi_role ~= "client" then return end
	local ok = ba.postGameEvent(ba.GameEvents["GS_EVENT_MULTI_JOIN_GAME"])
	H.event("multi join -> " .. tostring(ok))
	H.set_switch("multi_join", ok and "done" or "failed: postGameEvent returned false")
end, "multi_join")
```

If Task 1 recorded that only the real button works (fallback A2), the hook instead records `multi_join = "needs-click"` and `_wait_ready` (Task 7) performs the click from outside with the coordinates Task 1 recorded, through `xinput`'s existing click helper; ledger the ruling. `luajit -bl fsoctl/bridge/fso_harness_bridge/data/scripts/fsoharness.lua > /dev/null` must pass. Bump the bridge version constant the way the bridge-install tests expect (read `fsoctl/bridge_install.py` for the stamp).

- [ ] **Step 4: Run the tests**

Run: `bounded-run pytest tests/test_launcher_unit.py tests/test_b_runner.py tests/test_envsetup.py tests/test_bridge_lua.py tests/test_b_bridge_install.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add fsoctl/launcher.py fsoctl/runner.py fsoctl/envsetup.py fsoctl/bridge tests
git commit -m "Run match sessions inside the private network; standalone without Xvfb; bridge multiplayer switches"
```

---

### Task 7: Readiness and launch output per role

**Files:**
- Modify: `fsoctl/launcher.py:404-451` (`_wait_ready`, `_warnings`, `launch`), `:500-513` (`status`), `:636-640` (`list_all`)
- Test: `tests/test_launcher_unit.py`

**Interfaces:**
- Consumes: `match.run_inside(m, ["ss", "-H", "-lun", f"sport = :{port}"])`, `channel.Channel(...).wait_ready`, `_alive_or_starting`.
- Produces: `listening(sess: Session) -> bool`; `launch()` output gains `match`, `role`, `port`, `holder_pid` (None for ordinary sessions) and, for a standalone, `renderer: "none"`; `status()` gains `role`, `match`, `listening` (standalone) and `network: "alive" | "dead" | None`; `list_all()` rows gain `role`, `match`.

- [ ] **Step 1: Write the failing tests**

```python
def test_listening_asks_inside_the_match(tmp_runtime, monkeypatch):
    m = _match_alive(monkeypatch)
    s = _sess(role="standalone", match="m0a1b2c3d", port=7808, mission=None, extra_args=[])
    calls = []

    def fake_run(mm, argv, timeout=10):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="UNCONN 0 0 *:7808 *:*\n", stderr="")
    monkeypatch.setattr(match, "run_inside", fake_run)
    assert launcher.listening(s) is True
    assert calls[0] == ["ss", "-H", "-lun", "sport = :7808"]
    monkeypatch.setattr(match, "run_inside",
                        lambda mm, argv, timeout=10: subprocess.CompletedProcess(argv, 0, "", ""))
    assert launcher.listening(s) is False


def test_wait_ready_standalone_times_out_with_launch_code(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    s = _sess(role="standalone", match="m0a1b2c3d", port=7808, mission=None, extra_args=[])
    s.status = "running"
    session.save(s)
    monkeypatch.setattr(launcher, "listening", lambda sess: False)
    req = LaunchRequest(binary="/bin/true", match="m0a1b2c3d", role="standalone", ready_timeout=0.3)
    with pytest.raises(HarnessError) as e:
        launcher._wait_ready(req, s.id)
    assert e.value.code == EXIT_TIMEOUT and "7808" in e.value.message


def test_status_reports_dead_network(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    s = _sess(role="client", match="m0a1b2c3d", port=7809, mission=None, extra_args=[])
    s.status = "running"
    session.save(s)
    monkeypatch.setattr(launcher.channel.Channel, "heartbeat", lambda self: {})
    monkeypatch.setattr(launcher, "alive", lambda sess: True)
    assert launcher.status(s.id)["network"] == "alive"
    monkeypatch.setattr(match, "alive", lambda m: False)
    st = launcher.status(s.id)
    assert st["network"] == "dead" and st["status"] == "dead-network"
```

(`EXIT_TIMEOUT` joins the errors import.)

- [ ] **Step 2: Run to verify they fail**

Run: `bounded-run pytest tests/test_launcher_unit.py -q -k "listening or dead_network or wait_ready_standalone"`
Expected: FAIL (`listening` missing; `network` key missing).

- [ ] **Step 3: Implement**

```python
def listening(sess: session.Session) -> bool:
    """True when a UDP socket on the session's port exists inside its match (standalone readiness).

    Asked through nsenter because the socket exists only in the private network; `ss` on the real
    network must show nothing, which is the point (spec section 5.1).
    """
    r = match.run_inside(match.load(sess.match), ["ss", "-H", "-lun", f"sport = :{sess.port}"])
    return r.returncode == 0 and bool(r.stdout.strip())


def _wait_listening(req: LaunchRequest, sid: str) -> None:
    """Block (bounded) until the standalone listens; EXIT_TIMEOUT / EXIT_ENGINE otherwise."""
    deadline = time.monotonic() + req.ready_timeout
    while time.monotonic() < deadline:
        s = session.load(sid)
        if s.status in ("exited", "failed"):
            raise HarnessError(EXIT_ENGINE, "the standalone server exited during startup",
                               "see `fsoctl log <id>`")
        if s.status == "running" and listening(s):
            return
        time.sleep(0.5)
    raise HarnessError(EXIT_TIMEOUT, f"the standalone never listened on port {req.port or 'its port'} "
                       f"in {req.ready_timeout:.0f}s", "see `fsoctl log <id>`")
```

In `_wait_ready`: `if req.role == "standalone": _wait_listening(req, sid); return {}` before the game branch. In `launch()`: for a standalone, `found = {"renderer": "none", "libraries": []}` instead of `renderer.classify(...)` (read `renderer.classify`'s return keys and match them exactly); `_warnings` returns `[]` when `req.role == "standalone"`. The result dict gains `"match": sess.match, "role": sess.role, "port": sess.port, "holder_pid": match.load(sess.match).holder_pid if sess.match else None`.

`status()`:

```python
    network = None
    if sess.match:
        network = "alive" if match.alive(match.load(sess.match)) else "dead"
    out = {**asdict(sess), "alive": alive(sess), "heartbeat": hb, "heartbeat_age_sec": age,
           "stale": None if age is None else age > STALE_SEC, "network": network,
           "listening": listening(sess) if sess.role == "standalone" and network == "alive" else None}
    if network == "dead" and sess.status in ("starting", "running"):
        out["status"] = "dead-network"     # spec section 5.6: the record is not rewritten, the read says it
    return out
```

`list_all()` rows: add `"role": s.role, "match": s.match`.

- [ ] **Step 4: Run the tests**

Run: `bounded-run pytest tests/test_launcher_unit.py tests/test_b_launcher.py tests/test_wait_unit.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add fsoctl/launcher.py tests/test_launcher_unit.py
git commit -m "Readiness per role: standalone listener check, dead-network status, match fields in output"
```

---

### Task 8: Refusals and lifetime: dead match, remove while starting, cleanup

**Files:**
- Modify: `fsoctl/launcher.py` (`_check_role` already refuses a dead match; `remove_match`; `_removable`)
- Test: `tests/test_launcher_unit.py`, `tests/test_match_unit.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_remove_match_stops_starting_sessions_first(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    s = _sess(role="client", match="m0a1b2c3d", port=7809, mission=None, extra_args=[])   # status "starting"
    order = []
    monkeypatch.setattr(launcher, "stop", lambda sid: order.append(("stop", sid)) or {"status": "stopped"})
    monkeypatch.setattr(match, "end_holder", lambda m: order.append(("holder", m.id)))
    out = launcher.remove_match("m0a1b2c3d")
    assert order == [("stop", s.id), ("holder", "m0a1b2c3d")]
    assert out["stopped"] == [s.id]


def test_cleanup_keeps_match_with_sessions_and_removes_dead_empty_one(tmp_runtime, monkeypatch):
    m = _match_alive(monkeypatch)
    match.save(m)
    s = _sess(role="client", match="m0a1b2c3d", port=7809, mission=None, extra_args=[])
    s.status = "exited"
    session.save(s)
    monkeypatch.setattr(match, "alive", lambda mm: False)
    monkeypatch.setattr(launcher, "_removable", lambda sess, now, older: False)   # keep the session
    out = launcher.cleanup(0)
    assert "m0a1b2c3d" not in out["removed"]
    monkeypatch.setattr(launcher, "_removable", lambda sess, now, older: True)
    out = launcher.cleanup(0)
    assert s.id in out["removed"] and "m0a1b2c3d" in out["removed"]
    assert not paths.match_dir("m0a1b2c3d").exists()
```

- [ ] **Step 2: Run to verify they fail**

Run: `bounded-run pytest tests/test_launcher_unit.py -q -k "remove_match or cleanup_keeps"`
Expected: the first passes only if Task 4's `remove_match` already stops "starting" sessions (it does: status in ("starting", "running")); the second FAILS until `_cleanup_locked` evaluates `sessions_in_match` AFTER the session loop removed the session's folder (verify by reading the output: the match must be removed in the same cleanup run once its last session goes).

- [ ] **Step 3: Implement**

Make `_cleanup_locked` compute the match pass after the session loop and use `sessions_in_match` (which reads the folders, so a removed session no longer counts). If the first test failed, fix `remove_match` to include "starting".

- [ ] **Step 4: Run**

Run: `bounded-run pytest tests/test_launcher_unit.py tests/test_match_unit.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add fsoctl/launcher.py tests
git commit -m "Match lifetime: remove order and cleanup of dead matches"
```

---

### Task 9: Engine-tier end-to-end and the security tests

**Files:**
- Create: `tests/test_multi_engine.py`
- Modify: `tests/engine_helpers.py` (helper `launch_role`)

**Interfaces:**
- Consumes: everything above; `engine_env`, `tmp_runtime`, `wait_hb`.
- Produces: the executable proof for spec §5.1, 5.2, 5.3, 5.6, 5.7 and success criteria (a), (b).

- [ ] **Step 1: Write the tests**

`tests/test_multi_engine.py`:

```python
"""Engine-tier multiplayer tests: a real match (unshare/nsenter), a real standalone and client.

Security requirements from the spec (section 5) are asserted here, each one as a check that would
FAIL if containment broke: listeners from the real network, a send out of the match, +pxo, holder
death. Run: bounded-run pytest -m engine tests/test_multi_engine.py -p no:xdist
"""
import errno
import os
import socket
import subprocess
import textwrap
import time

import pytest

from fsoctl import launcher, match, paths, session
from fsoctl.launcher import LaunchRequest
from tests.engine_helpers import MODS, wait_hb

pytestmark = pytest.mark.engine
MP_MISSION = os.environ.get("FSO_TEST_MP_MISSION", "m-01")     # name, not filename (-almission)


@pytest.fixture
def live_match(tmp_runtime):
    m = match.create(label="test")
    try:
        yield m
    finally:
        launcher.remove_match(m.id)


def _listeners(port: int, proto: str = "u") -> int:
    """Count sockets on `port` on the REAL network (what the LAN would see)."""
    out = subprocess.run(["ss", "-H", f"-l{proto}n", f"sport = :{port}"], capture_output=True,
                         text=True, check=False).stdout
    return len(out.strip().splitlines())


def _launch(fso, m, role, **kw):
    r = launcher.launch(LaunchRequest(binary=str(fso), mods=MODS, match=m.id, role=role,
                                      skip_movies=True, dismiss_popups=True, **kw))
    return r


def test_match_holder_is_unprivileged_and_private(live_match):
    m = live_match
    assert match.alive(m)
    uid = match.run_inside(m, ["id", "-u"]).stdout.strip()
    assert uid == "0"                                   # root INSIDE the user namespace only
    outside = subprocess.run(["ps", "-o", "uid=", "-p", str(m.holder_pid)], capture_output=True,
                             text=True, check=False).stdout.strip()
    assert outside == str(os.getuid())                   # the plain user outside
    # spec 5.2: a send to a non-loopback address fails inside, with ENETUNREACH, not silently
    probe = textwrap.dedent("""
        import socket, errno
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(b"x", ("8.8.8.8", 53)); print("SENT")
        except OSError as e:
            print(e.errno)
    """)
    r = match.run_inside(m, ["python3", "-c", probe])
    assert r.stdout.strip() == str(errno.ENETUNREACH)
    # spec 5.7, recorded not required: names may still resolve through the host resolver socket
    r = match.run_inside(m, ["getent", "hosts", "example.com"])
    assert r.returncode in (0, 2)      # documents the current behaviour either way


def test_standalone_listens_only_inside_the_match(engine_env, live_match):
    _, fso = engine_env
    m = live_match
    before_udp, before_tcp = _listeners(7808), _listeners(8080, "t")
    r = _launch(fso, m, "standalone")
    try:
        assert r["role"] == "standalone" and r["port"] == 7808 and r["renderer"] == "none"
        st = launcher.status(r["id"])
        assert st["listening"] is True and st["network"] == "alive"
        assert _listeners(7808) == before_udp               # spec 5.1: nothing new on the LAN side
        assert _listeners(8080, "t") == before_tcp
        cfg = (session.prefs_dir(session.load(r["id"])) / "data" / "multi.cfg").read_text()
        assert "+pxo" not in cfg                            # spec 5.3
        assert "+pxo" not in "".join(p.read_text() for p in
                                     session.prefs_dir(session.load(r["id"])).rglob("*.cfg"))
    finally:
        launcher.stop(r["id"])


def test_client_joins_standalone(engine_env, live_match):
    _, fso = engine_env
    m = live_match
    srv = _launch(fso, m, "standalone")
    cli = None
    try:
        cli = _launch(fso, m, "client", pilot_mode="create", pilot_name="Alpha")
        assert cli["port"] == 7809
        hb = wait_hb(cli["id"], lambda h: h.get("state") in ("GS_STATE_MULTI_HOST_SETUP",
                                                             "GS_STATE_MULTI_CLIENT_SETUP"),
                     "a multiplayer setup state", timeout=180)
        assert hb["switches"]["multi_join"] == "done" and hb["switches"]["pilot_mode"] == "done"
        log = (session.run_dir(session.load(srv["id"])) / "engine.log").read_text(errors="replace")
        assert "Alpha" in log or "Alpha" in (session.prefs_dir(session.load(srv["id"]))
                                             / "data" / "fs2_open.log").read_text(errors="replace")
    finally:
        if cli:
            launcher.stop(cli["id"])
        launcher.stop(srv["id"])


def test_two_clients_get_distinct_ports_and_cap_counts_them(engine_env, live_match, monkeypatch):
    _, fso = engine_env
    m = live_match
    monkeypatch.setenv("FSO_HARNESS_MAX_SESSIONS", "3")
    ids = []
    try:
        ids.append(_launch(fso, m, "standalone")["id"])
        ids.append(_launch(fso, m, "client", pilot_mode="create", pilot_name="A")["id"])
        ids.append(_launch(fso, m, "client", pilot_mode="create", pilot_name="B")["id"])
        assert [session.load(i).port for i in ids] == [7808, 7809, 7810]
        with pytest.raises(Exception) as e:                 # the cap: 3 sessions, match counts 0
            _launch(fso, m, "client", pilot_mode="create", pilot_name="C")
        assert "cap" in str(e.value)
    finally:
        for i in ids:
            launcher.stop(i)


def test_mission_starts_with_almission(engine_env, live_match):
    """Success criterion (a): server + client reach GS_STATE_GAME_PLAY together."""
    _, fso = engine_env
    m = live_match
    srv = _launch(fso, m, "standalone")
    cli = None
    try:
        cli = _launch(fso, m, "client", pilot_mode="create", pilot_name="Alpha", mission=MP_MISSION,
                      skip_command_briefing=True, skip_briefing=True)
        wait_hb(cli["id"], lambda h: h.get("state") == "GS_STATE_GAME_PLAY", "GS_STATE_GAME_PLAY",
                timeout=300)
    finally:
        if cli:
            launcher.stop(cli["id"])
        launcher.stop(srv["id"])


def test_holder_death_ends_the_network(engine_env, live_match):
    _, fso = engine_env
    m = live_match
    srv = _launch(fso, m, "standalone")
    try:
        subprocess.run(["systemctl", "--user", "kill", "--signal=SIGKILL", f"{m.scope}.scope"],
                       check=False)
        for _ in range(50):          # bounded: 10 s for systemd to end the holder's scope
            if not match.alive(m):
                break
            time.sleep(0.2)
        assert not match.alive(m)
        st = launcher.status(srv["id"])
        assert st["network"] == "dead" and st["status"] in ("dead-network", "exited", "stopped")
        with pytest.raises(Exception) as e:
            _launch(fso, m, "client", pilot_mode="create", pilot_name="Z")
        assert "dead" in str(e.value)
    finally:
        launcher.stop(srv["id"])


def test_remove_match_stops_sessions_then_holder(engine_env, live_match):
    _, fso = engine_env
    m = live_match
    srv = _launch(fso, m, "standalone")
    out = launcher.remove_match(m.id)
    assert out["stopped"] == [srv["id"]]
    assert not launcher.alive(session.load(srv["id"])) and not match.alive(m)
```

If Task 2 recorded that `-almission` belongs to the standalone, move `mission=MP_MISSION` to the standalone launch in `test_mission_starts_with_almission`.

Add to `tests/engine_helpers.py` nothing unless two tests share a helper; `_launch` stays local.

- [ ] **Step 2: Run them**

Run (from the harness checkout): `bounded-run pytest -m engine tests/test_multi_engine.py -p no:xdist -q -x 2>&1 | tail -n 30`
Expected: 7 passed. Each test has a `finally` that stops what it started; if one fails, run `fsoctl match list` and `fsoctl list` and remove leftovers before rerunning.

- [ ] **Step 3: Full suite**

Run: `bounded-run pytest -q -p no:xdist 2>&1 | tail -n 5 && ruff check . && shellcheck probes/*.sh mcp/*.sh`
Expected: unit suite green (engine tests skip without the env), ruff and shellcheck clean.

- [ ] **Step 4: Commit**

```bash
git add tests/test_multi_engine.py tests/engine_helpers.py
git commit -m "Engine-tier multiplayer tests: containment, join, ports, cap, holder death"
```

---

### Task 10: Documentation and agent guidance

**Files:**
- Modify: `README.md` (section "Multiplayer")
- Modify: `docs/AGENT-GUIDE.md` (section "Reproduce a multiplayer bug")
- Modify: `mcp/server.py` (`INSTRUCTIONS` one sentence; `PROMPTS["reproduce_multiplayer_bug"]`)
- Modify: `skills/fso-harness/SKILL.md` (one sentence)
- Modify: `CLAUDE.md` (one load-bearing decision line)
- Test: `tests/mcp_check.py` (INSTRUCTIONS <= 2048; prompt names), `tests/test_d_docs.py` (read it: it may check guide headings)

- [ ] **Step 1: Write the guide recipe**

`docs/AGENT-GUIDE.md`, new `## Reproduce a multiplayer bug` before `## Read a crash`:

```
## Reproduce a multiplayer bug

Every multiplayer session runs inside a *match*: a private, loopback-only network that the LAN and
the internet cannot see (no port is ever opened on the real network; the online tracker is never
enabled). Create one, launch a server and clients into it, drive them like any session, remove it.

1. `match_create()` -> `{"id": "m...", ...}`.
2. Server: `launch(match=<id>, role="standalone")` (no window; `status` shows `listening`), or a
   player host: `launch(match=<id>, role="host", pilot="create:Host", mission="m-01")`.
3. Clients: `launch(match=<id>, role="client", pilot="create:Alpha", skip_movies=true,
   dismiss_popups=true)`; each gets the next free port and auto-joins the server. The first client
   of a standalone game is the game host; give it `mission="m-01"` to start that mission without a
   human (the engine's -almission).
4. `wait_for(id, state="GS_STATE_MULTI_HOST_SETUP")` / `..._CLIENT_SETUP` / `GS_STATE_GAME_PLAY`;
   then `sexp`, `lua`, `state`, `pilot`, `screenshot` as usual on each client.
5. Read both sides: `log(<client>)`, `log(<server>)` (the standalone writes fs2_open.log too).
6. `match_remove(<id>)` stops every session in the match first, then the network.

Facts: a session's port is in `launch`'s result; `status` shows `network: alive|dead`; a dead
network makes every session in it `dead-network`. Hostnames still resolve inside a match (the
host's resolver answers over a local socket) but nothing can be sent to the answer. Open
multiplayer issues: label `multi` upstream; the maintainers' checklist is #6927.
```

- [ ] **Step 2: README, INSTRUCTIONS, PROMPTS, SKILL.md, CLAUDE.md**

README "Multiplayer" section: four short paragraphs (what a match is and why a namespace; the three roles and their flags; what is private and what is not, including the name-resolution residual; the `match` verbs). `PROMPTS["reproduce_multiplayer_bug"] = "Reproduce a multiplayer bug"`. INSTRUCTIONS: add one sentence to the prompts line: `Multiplayer: match_create, then launch role=standalone|host|client into it (private network, nothing on the LAN); prompt /fso-harness:reproduce_multiplayer_bug.` and run `python3 tests/mcp_check.py` to confirm the 2,048 cap still holds (shorten elsewhere in INSTRUCTIONS if not; the cap is the contract). SKILL.md: the same sentence under its recipes list. `CLAUDE.md` load-bearing decisions: `- **Multiplayer runs inside a match**: an unprivileged user+network namespace per game (unshare --map-root-user, holder process in a scope, sessions join via nsenter). Nothing is ever bound on the real network; never add a port forward or firewall rule to "help".`

- [ ] **Step 3: Checks**

Run: `python3 tests/mcp_check.py && bounded-run pytest tests/test_d_docs.py tests/test_mcp_wrapper.py -q`
Expected: pass; INSTRUCTIONS length printed <= 2048.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/AGENT-GUIDE.md mcp/server.py skills/fso-harness/SKILL.md CLAUDE.md
git commit -m "Document matches and multiplayer roles for agents and readers"
```

---

### Task 11: Mutation pass, plan status, close-out

**Files:**
- Modify: `pyproject.toml` (`[tool.mutmut]` `source_paths`/`also_copy` as the earlier passes used; read it)
- Modify: this plan's Status block and a "Deviations" section

- [ ] **Step 1: Mutation on touched Python**

Run (from the harness checkout): `bounded-run mutmut run --paths-to-mutate fsoctl/match.py,fsoctl/launcher.py 2>&1 | tail -n 5; mutmut results | head -n 40`
Expected: every survivor in `match.py` and in the new launcher functions (`_check_role`, `assign_port`, `multi_cfg_text`, `_multi_argv`, `listening`, `_wait_listening`, `remove_match`) is read; each is either killed by a new test (RED first, then GREEN) or recorded with a reason. Write the `MUTATION:` line for the QA synthesis.

- [ ] **Step 2: Update the plan**

Status block: Phase 11 of 11, commits listed, Next = HIGH-RISK QA (the holder is a long-lived process with a pid handle; takeover/remove behaviour; security boundary), then the cross-family pass. Add `## Deviations` listing: the port rule (Task 5 Step 0), the `-almission` consumer ruling, the join mechanism ruling, anything else ledgered.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/plans/2026-10-12-fso-harness-multiplayer.md pyproject.toml tests
git commit -m "Multiplayer: mutation pass and plan status"
```

Then the workspace QA Review runs (HIGH-RISK tier: state-lifetime-auditor with the premise-challenger brief, code-reviewer, test-analyzer, security-auditor; mutation line; cross-family seats), per the workspace's review rules; it is not part of this plan's tasks.

## Appendix: Deferments originated in the multiplayer slice (QA 2026-10-13)
- DEF-MP-01 (found by TA-2/CR-10; 93da8b7; defer target: next multiplayer slice; MEDIUM):
  host-plus-client topology. A -almission host starts its mission at once, before a client can
  load and join, and a client cannot enter a mission in progress. Fix direction: a `hold`
  switch keeping the host in host setup until N players joined, then Launch (bridge event).
  Obsolete if the standalone-first recipe is the only one anyone uses. Test:
  test_client_joins_player_host, strict xfail.
- DEF-MP-02 (CR-13; LOW): display verbs on a standalone give the xdotool error instead of a
  role-aware refusal. One line in xinput; revisit when a standalone-only agent flow needs it.
- DEF-MP-03 (SA-3): RESOLVED 2026-10-13 (Dustin: "I concur with your recommendation"): every
  session runs in its own loopback-only network by default via the same unshare prefix, no
  holder for a single session; `--network real` is the opt-out. Engine test
  test_ordinary_sessions_are_private_by_default_and_real_is_opt_in. Found on the way: Xvfb
  inside a namespace picked the desktop's display number as seen from the host (also true of
  the published match clients); the fix keeps the runner and Xvfb on the host and wraps only the
  engine (runner.engine_argv; engine test test_private_session_display_is_reachable_from_the_host
  pins the window's pid). The upstream half (a
  bind-address or no-network engine flag) is a note for Goober's list, Dustin's to post.
- DEF-MP-04 (XFA-1/XFA-2; MEDIUM): standalone + two clients + mission. The engine refuses an
  unlocked briefing with more than one player and the countdown can start before every
  player's settings ack. Fix direction: probe the cell on FSO_BIN_DEBUG, then a bridge
  "lock loadouts" step or a readiness poll before startCountdown. Test: none yet (the cell is
  unexecuted).

## Appendix: Deferments originated in the DEF-MP-03 slice (QA 2026-10-13)
- DEF-PN-01 (found by the head's watcher on the fix-wave tiers; aa6652c; defer target: next
  qtFRED slice; MEDIUM; pre-existing): qtFRED writes FS2PATH/sexps.html on every launch (its help
  dialog, qtfred/src/mission/dialogs/HelpTopicsDialogModel.cpp:196) because qtfred sessions run
  with cwd = FS2PATH. Seven writes in the qtfred tier, none in the engine tier; the earlier
  api_docs failure was the qtfred tier overlapping it. Fix direction: a shadow working folder
  for qtFRED launches (as api_docs does). Obsolete when qtFRED launches no longer write there.
- DEF-PN-02 (TA-5; LOW): real x qtfred has no executed cell. Revisit if an agent flow needs
  the editor on the real network.
