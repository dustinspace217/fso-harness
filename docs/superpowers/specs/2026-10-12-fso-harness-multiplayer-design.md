# fso-harness multiplayer: design

Status: approved in conversation 2026-10-12 21:46 PDT ("Yes, do it"), web panel kept closed.
Parent spec: `2026-10-12-fso-harness-design.md` (v1 harness). This document adds multiplayer
sessions to that harness; everything it does not mention stays as v1 built it.

## 1. Intent

Dustin's words, 2026-10-12: Goober asked him to "Fix multiplayer support - I see there are a lot
of open issues on multiplayer", and for the harness: "Make the harness extension, but make sure
not to compromise device security."

Outcome: an agent can start a FreeSpace Open multiplayer game on one machine (a standalone
server or a player host, plus one or two clients), drive it with the existing harness tools, and
reproduce the open `multi`-labelled engine issues (15 open on 2026-10-12; the maintainers'
checklist is scp-fs2open/fs2open.github.com#6927), with nothing reachable from the LAN or the
internet while it runs.

Success: (a) a harness-only test shows a standalone server and a client reach `GS_STATE_GAME_PLAY`
together; (b) a probe from the real network during that test finds no listener on the game port
or the server's web panel port; (c) at least one open `multi` issue is reproduced through the
harness with a recipe an agent can rerun.

## 2. Scope of this slice

In: match objects (a private network shared by a group of sessions), three session roles
(standalone, host, client), their engine flags and private `multi.cfg`, readiness and waits for
each role, the security invariants and the tests that pin them, docs and MCP guidance.

Out (later slices, each its own spec): a second machine as a peer (Dustin's doorpi idea stays a
later, deliberate test case, and not on doorpi itself: it is the house's DNS box); the standalone
server's web panel reachable from a browser (decided closed, see §5); more than the session cap
allows (3 sessions = server + 2 clients); PXO / online-tracker games (never); multiplayer-specific
`state` readers beyond what the bridge already reports (added when an issue needs one).

## 3. Facts the design rests on (verified 2026-10-12 unless marked)

- The engine binds one UDP socket on every interface: `psnet_init_socket()` creates an AF_INET6
  dual-stack socket and binds `[::]:7808` (`-port` changes the number). There is no bind-address
  option. A standalone server also starts a web control panel on TCP 8080 (`webapiPort = 8080`,
  `mg_start` in `stand_gui-unix.cpp`), configurable in `multi.cfg` (`+webapi_server_port`).
- The online tracker (PXO) is off by default (`Multi.TogglePXO` default false) and only switched on
  by the player option or `+pxo` in a standalone `multi.cfg`. The harness never writes either.
- `-standalone` runs with no window on Linux (`stand_gui-unix.cpp`; `2d.cpp` skips the renderer
  when `Is_standalone`). `-startgame` skips the main hall and hosts. `-connect IP:PORT` makes a
  client query that address and join when it reaches the multiplayer join screen
  (`multiui.cpp`, `Cmdline_connect_addr`); nothing on the command line takes a client to that
  screen by itself.
- `multi.cfg` is read from `data/` through cfile (`cfopen(MULTI_CFG_FILE, "rt", CF_TYPE_DATA)`), so
  a file in the session's private prefs `data/` folder is found first and never touches the
  user's game folder.
- An unprivileged user namespace with its own network namespace works on this machine
  (`unshare --user --map-root-user --net`): loopback can be brought up, the engine-style
  `[::]:7808` bind succeeds, a second process joins the same network with
  `nsenter --preserve-credentials --user --net --target <pid>`, the real network shows no listener
  while it runs, and sending to an internet address fails with "Network is unreachable". It
  works inside a `systemd-run --user --scope`. `--map-current-user` does NOT work (no right to
  configure loopback); the process is uid 0 inside the namespace and the plain user outside.
- Residual: name lookups still resolve inside the namespace (the host resolver answers over a
  local socket), so a hostname can be looked up; nothing can be sent to the answer. This is one
  instance of a class (QA SL-10): pathname unix sockets are not network-namespaced, so a process
  inside a match can reach any host daemon's socket; the containment is IP containment, which is
  what the owner's requirement needs (the engine is not adversarial; envsetup points its D-Bus
  and XDG runtime into the run folder).
- Every engine run binds UDP 7808 on every interface, single-player included (game_init calls
  psnet_init unconditionally; measured on an ordinary harness session 2026-10-13, QA SA-3).
  Matches contain match sessions only; ordinary sessions are as visible on the LAN as the game
  itself. Whether every session should get a private network is a later design decision.
- A dead holder is a lost handle, not a dead network (QA SL premise attack 1): member processes
  hold the namespace until they exit; sessions in a dead match stay stoppable through their own
  scopes. The match record is written back as "dead" once, with one audit line.

## 4. Architecture

```
real network (LAN, internet)      nothing listens, no route out
--------------------------------------------------------------------
match m1a2 = one private network (loopback only), owned by a holder
   holder scope  fso-harness-match-m1a2 : unshare --user --map-root-user --net sleep infinity
   session s1    role standalone : engine -standalone -port 7808  (+ web panel :8080)
   session s2    role client     : Xvfb + engine -connect 127.0.0.1:7808
   session s3    role client     : Xvfb + engine -connect 127.0.0.1:7808
```

**Match.** `fsoctl match create [--label ...]` starts the holder in its own scope and records
`$XDG_RUNTIME_DIR/fso-harness/matches/<id>/match.json` (id, holder pid, scope, created, label).
The holder is `sleep infinity` inside the private network; its pid is the handle every session
joins through, and the network lives exactly as long as the holder. `match list` reports matches
and the sessions in each; `match remove <id>` stops the match's sessions first (existing `stop`,
runner-first ordering), then the holder scope; a match whose holder is gone is reported `dead`
and cleaned up by `cleanup`. No match is created implicitly: `launch --role ...` without `--match`
is refused with the hint to create one, so the private network is always a visible object.

**Session in a match.** `launch` gains `--match <id>` and `--role standalone|host|client` (and
`--connect`, default `127.0.0.1:7808`, `--port` (default: 7808 for the first session in the
match, then the next free port 7809, 7810...; a duplicate inside the match is refused, because
every engine process binds `[::]:<port>` and two in one namespace cannot share it; amended
2026-10-12 at plan Task 5), `--game-name`, default the match id, `--closed`). `--mission` on a
multiplayer role becomes `-almission`, which the HOST PLAYER consumes (the client that becomes
host on a standalone, or a `host` session; probe-results 21.2): a standalone with `--mission` is
refused. The session record gains `match`, `role`, `port`, `connect`. The scope is started
as today; the only change in the chain is that the runner's command line is
`nsenter --preserve-credentials --user --net --target <holder pid> -- python3 -m fsoctl.runner <id>`,
so Xvfb and the engine both live in the match network. Nothing else about the runner changes.
Xvfb's X socket is a filesystem unix socket (`/tmp/.X11-unix/X<n>`), which crosses network
namespaces, so `xdotool`, screenshots and the bridge files work exactly as before (the abstract
socket does not cross; X clients fall back to the filesystem one).

**Roles and engine arguments** (`build_argv`, additions only):
- `standalone`: `-standalone -port <port>`; no `-window/-res`, no Xvfb (the runner skips Xvfb for
  this role); a generated `multi.cfg` in the session's prefs `data/`:
  `+name <game-name>`, `+webapi_server_port 8080` (private anyway), never `+pxo`.
- `host`: `-startgame -gamename <game-name> -port <port>` plus `-closed` when asked; pilot created
  as a multiplayer pilot (spike 2 names the call); the bridge dismisses the host-setup popups
  the same way it dismisses single-player ones.
- `client`: `-connect <connect> -port <port>`; pilot as above; after the main hall is up the
  bridge takes the client to the join screen (spike 2 confirms the event; expected
  `GS_EVENT_MULTI_JOIN_GAME`), where the engine's own auto-join runs.
  The user-facing `multi.cfg` and the user's pilots are never read or written: every role gets
  the session's private prefs folder, as every v1 session does.

**Readiness.** standalone: the holder's network shows a UDP listener on `<port>` (checked with
`ss` run through `nsenter`); failure to listen within the launch budget is `EXIT_TIMEOUT` with a
hint (the v1 one-timeout-code convention; amended at QA CR-12), an exited server `EXIT_ENGINE`. host and client: the existing bridge
heartbeat; `wait_for state` works with the multiplayer states (`GS_STATE_MULTI_HOST_SETUP`,
`GS_STATE_MULTI_CLIENT_SETUP`, `GS_STATE_MULTI_START_GAME`, `GS_STATE_GAME_PLAY`), and a client
that is still on the join screen after the launch budget is reported with the server's state.

**What stays the same.** scopes and caps, private HOME/XDG, bridge, `state`, `log`, `sexp`, `lua`,
`wait_for`, `screenshot`, `stop`, `cleanup`, the eval gate, the session cap (a match's sessions
count against it), exit codes.

## 5. Security requirements (hard; each has a test in §9)

1. Game and web sockets of every match session exist only inside that match's network: during
   a match, `ss -lun`/`ss -ltn` in the real namespace show no new listener on the game port or
   8080.
2. No route out: from inside a match, a send to a non-loopback address fails; the test asserts the
   failure (ENETUNREACH), not merely the absence of a reply.
3. The harness never enables the online tracker: no `+pxo` in any generated `multi.cfg`, no
   tracker option written to any pilot; a grep over generated files is part of the test.
4. No privilege: nothing in the chain runs as the real root; the holder is a plain user process in
   a user namespace; `fsoctl` refuses to run as uid 0 for match verbs (it already should not be).
5. The web panel is not reachable from the user's browser (decided 2026-10-12). Agents that need
   it read it from inside the match (`nsenter` + a local HTTP read), through a harness verb if an
   issue needs one; nothing forwards the port out.
6. Containment dies with the holder: stopping the holder scope ends the network; `match remove`
   stops sessions first so no engine outlives its network; a dead holder makes every session in
   the match report `status: dead-network` on the next read.
7. Names may resolve (residual from §3): documented in README and AGENT-GUIDE; a test records the
   current behaviour so a change is noticed.

## 6. State and lifetime

- `matches/<id>/match.json` is written once at create and read fresh by every verb (no daemon,
  as v1). Staleness: a holder pid that is gone, or whose scope is inactive, means the match is
  dead; verbs say so instead of acting on it.
- A holder pid could in theory be reused by an unrelated process after the holder dies; the
  match record therefore also stores the holder's scope name and start time, and every join
  checks the scope is active and the pid's start time matches before `nsenter`.
- `cleanup` removes dead matches (holder gone and no live sessions) with the same age rule it
  uses for sessions.
- Capacity: a match counts zero sessions; its sessions count as today. The launch lock covers
  match create and session join so two agents cannot race the cap.
- Re-entry: `fsoctl` restarts cost nothing (file state); a machine reboot ends everything
  (runtime dir is tmpfs), same as v1.

## 7. Observability

`launch` output names the match, role, port and holder pid. `status` shows role, match and, for
a standalone, whether it is listening. `log` works for all roles (the standalone writes the same
`fs2_open.log`). Every match create/remove is one line in the existing persistent audit log
(`~/.local/state/fso-harness/audit.log`), because a private network is a resource worth a record.

## 8. Spikes before the plan (each a throwaway script in `probes/`, results into probe-results.md)

1. Standalone + client handshake inside one match network: the client reaches
   `GS_STATE_MULTI_CLIENT_SETUP`, the server lists it; both logs read.
2. Client join-screen entry without a human: the Lua call that takes a fresh multiplayer pilot
   from the main hall to the join screen, and the pilot-creation call that marks a pilot
   multiplayer (`ui.PilotSelect.createPilot` arguments, from `api_docs`).
3. Bridge heartbeat and `state` in multiplayer states (host setup, client setup, mission sync).
   If the bridge's frame hook does not run in some state, readiness for that role falls back to
   the log.
If spike 1 fails on IPv6 dual-stack inside the namespace, the fallback is `-connect [::1]:7808`;
if that fails too, the slice stops and the finding goes to Dustin.

## 9. Tests

Unit (no engine): match.json round trip; `build_argv` per role (exact flags; never `+pxo`); the
`nsenter` wrapping of the runner command; refusal of `--role` without `--match`; dead-holder
detection by pid start time; `multi.cfg` generation.
Engine tier (`-m engine`, needs FS2PATH, FSO_BIN): the three spikes turned into tests; the
security tests of §5 (listeners from the real namespace during a match, ENETUNREACH from inside,
grep for `+pxo`, holder death ends the network); `match remove` ordering; cap accounting with a
match.
Mutation: the usual `mutmut` pass on touched Python (`also_copy` as before).

## 10. Documentation

README: a "Multiplayer" section (match, roles, what is private, the name-resolution residual).
AGENT-GUIDE: recipe "Reproduce a multiplayer bug" (create match, start standalone, start client,
wait for the states, read both logs, stop in order). MCP instructions: one sentence pointing at
the recipe (budget: the 2,048-character cap). SKILL.md: same sentence.

## 11. Decisions taken in this design (owner: Dustin unless noted)

- D1. Containment by private network per match, not firewall rules or containers (Dustin,
  "Yes, do it", 2026-10-12).
- D2. Web panel closed to the user's browser (Dustin, same message).
- D3. A match is an explicit object; no implicit creation (Claude; keeps the private network a
  visible resource with a record and a removal verb).
- D4. Standalone sessions run without Xvfb (Claude; the engine has no window in that mode).
- D5. doorpi is not a peer host in this slice (Claude, reasons in §2; Dustin's idea recorded as a
  later test case).
