# Cross-platform Plan A: real-network default, CI on three OSes, probes

## Status (updated 2026-11-16)
Phase: 3 of 3: CLOSED. Plan B is written: docs/superpowers/plans/2026-11-16-cross-platform-plan-b.md
Done: Tasks 0-10 (2026-10-25; QA fso-harness-artifacts/qa-xplat-plan-a-probes/synthesis.md, 39 IDs:
37 fixed, 1 rejected as equivalent mutants, 1 tracked DEF-PA-01). 2026-11-16, on the owner's
"Continue. Test with VMs": the open items measured in a Windows 11 VM instead of his Windows 10
boot (FRED2 with retail data, combo-box routes, the engine with data on a hidden desktop):
docs/research/2026-11-16-xplat-vm-results.md.
Next: Plan B.
Blocked: nothing. Still unmeasured: a real graphics card on a hidden desktop (a VM has none).
Owner decision still open: a private report of the stock-engine panel issue upstream

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the real network the default on Linux (sessions and matches), add CI on Linux,
Windows and macOS, and run the probes whose results shape Plan B (platform layer + Windows).

**Architecture:** Linux code changes only in the network path (launcher, match, runner, MCP
server, docs). Everything else in this plan is new: GitHub Actions workflows and self-contained
probe scripts under `probes/xplat/` whose output is a JSON result per probe, collected into one
research note. Probes are measurements, not product code: they may fail, and a failure is a
result.

**Tech Stack:** Python 3.12, pytest, GitHub Actions (ubuntu-latest, windows-latest,
macos-latest), ctypes (Win32), PySide6 (Qt 6 stand-in app), pywinauto or comtypes (UI
Automation), pyobjc (macOS Accessibility), pyatspi (AT-SPI), FSO release 26.0.1 builds.

**Spec:** docs/superpowers/specs/2026-10-16-cross-platform-design.md (approved 2026-10-16). Split
agreed the same night: probes before the platform layer; Plan B is written after the results.

## Global Constraints

- Linux install stays dependency-free (`dependencies = []`); probe packages are installed only
  inside CI jobs or via a `probes` requirements file, never added to `pyproject.toml`.
- Nothing that works on Linux today is removed. `--network private` and private matches stay,
  opt-in.
- Real network is the default on every OS (owner: "Real network. I never meant to change this
  about how the engine works.").
- The standalone server never starts with the engine's default web panel login (admin/admin).
- No retail game data in CI or in the repository (licensing).
- Public history is curated: CI must not require pushing local history to the public repo
  (see Task 0).
- Every bounded loop and wait has an explicit cap (workspace Power-of-Ten rule 2).

## Review Focus

1. Two single-player sessions on the real network at once: both run; neither fails to bind the
   game port (each session gets its own `-port`). Pinned in Task 1.
2. A real-network match next to another real-network match: no port collision between them,
   for game ports or web panel ports. Pinned in Task 2.
3. An old session record or match record without the new `network` field (written before this
   change) still loads and is treated as what it was (private). Pinned in Tasks 1 and 2.
4. `launch` from the MCP server with no network argument gives the real network, the same as
   the CLI. Pinned in Task 1.
5. The web panel credentials are readable by the agent (in the launch result and `status`) but
   never written to the audit log or a journal. Pinned in Task 3.

---

### Task 0: Decide where CI runs (owner's decision, no code)

The public repository only receives curated single commits from the publish script, so
iterating CI there means publishing every attempt. Options for the owner:
(a) a private repository (for example `fso-harness-ci`) that receives pushes of the local
`main` for CI only (private repos get free Actions minutes; macOS minutes count 10x);
(b) publish each CI iteration to the public repo.
Recommendation: (a). Record the decision in this plan's Status block before Task 4.

### Task 1: Real network by default for single sessions; a unique game port per session

**Files:**
- Modify: `fsoctl/launcher.py` (LaunchRequest.network default, `--network` default and help,
  `_check_network`, `build_argv`, port assignment)
- Modify: `mcp/server.py:199` (launch default `network="real"`)
- Modify: `fsoctl/session.py` (record loading: a record without `network` loads as "private")
- Modify: `README.md` (How it works, Multiplayer, Limits), `docs/AGENT-GUIDE.md` (network lines),
  `CLAUDE.md` (the "Nothing is ever bound on the real network" decision, superseded by the
  owner's 2026-10-16 ruling; found by the scope audit)
- Test: `tests/test_launcher_unit.py`, `tests/test_session.py`

**Interfaces:**
- Produces: `launcher.NETWORKS = ("real", "private")`; `LaunchRequest.network = "real"`;
  `launcher.host_port(wanted: int | None) -> int` (first free UDP game port from 7808 up across
  every live real-network session on the host, or `wanted` if free; raises EXIT_USAGE if taken);
  real-network game sessions always get `-port <n>` in argv.

- [ ] **Step 1: Write failing tests**
  - `test_network_defaults_to_real`: `LaunchRequest(binary="/bin/true").network == "real"`, and
    the CLI parser's default for `--network` is `"real"`.
  - `test_two_real_sessions_get_distinct_ports`: with one live real-network session on 7808
    (scope stubbed alive), `host_port(None) == 7809`; with it gone, `7808`.
  - `test_real_session_argv_carries_port`: `build_argv` of a real-network game session contains
    `-port` followed by its assigned port.
  - `test_private_still_works`: `--network private` passes `_check_network` (tools stubbed
    present) and argv has the unshare prefix as today.
  - `test_old_record_without_network_loads_private`: a session.json lacking `network` loads with
    `network == "private"`.
  - `test_mcp_launch_defaults_to_real`: the MCP `launch` tool's signature default is `"real"`.
- [ ] **Step 2: Run** `bounded-run python3 -m pytest -q tests/test_launcher_unit.py tests/test_session.py`
  Expected: the new tests FAIL.
- [ ] **Step 3: Implement** the defaults, `host_port`, `-port` for real sessions, the record
  default, the MCP default. Keep the private path byte-identical.
- [ ] **Step 4: Live check on this machine:** launch two single-player sessions with no
  `--network` at once, both reach `GS_STATE_MAIN_MENU`, `ss -lunp` shows two different UDP
  ports owned by the two engines, both stop cleanly.
- [ ] **Step 5: Update README/AGENT-GUIDE** (real network is the default; `--network private`
  is the opt-in; what a running session exposes: one UDP game port per session).
- [ ] **Step 6: Run the full unit tier** `bounded-run python3 -m pytest -q`; Expected: all pass.
- [ ] **Step 7: Commit** `git commit -m "Sessions use the real network by default; --network private is opt-in"`

### Task 2: Matches on the real network by default

**Files:**
- Modify: `fsoctl/match.py` (Match.network field; create without a holder for "real"; alive,
  observe, nsenter_prefix, end_holder for real matches; CLI `match create --network`)
- Modify: `fsoctl/launcher.py` (`_check_network`: a match session takes its match's network;
  `assign_port` and `default_connect` use `host_port` rules for real matches)
- Modify: `fsoctl/runner.py` (no nsenter for real matches)
- Modify: `README.md` Multiplayer section; `docs/AGENT-GUIDE.md` multiplayer recipe
- Test: `tests/test_match_unit.py`, `tests/test_launcher_unit.py`, `tests/test_b_runner.py`

**Interfaces:**
- Consumes: `launcher.host_port` (Task 1).
- Produces: `Match.network: str = "private"` (dataclass default stays "private" so old
  records load as private; `create(label, network="real")` sets it); `match.create(label: str =
  "", network: str = "real") -> Match`; for real matches `alive(m)` is `m.status == "alive"`,
  `nsenter_prefix(m) == []`, `end_holder` only writes status.

- [ ] **Step 1: Write failing tests**
  - `test_real_match_has_no_holder`: `create(network="real")` starts no scope, status `alive`.
  - `test_real_match_sessions_get_host_unique_ports`: two real matches each with a standalone:
    ports differ (7808, 7809).
  - `test_real_match_client_connects_to_its_server`: `default_connect(mid)` is
    `127.0.0.1:<that match's server port>`.
  - `test_private_match_unchanged`: `create(network="private")` behaves as today (holder stubbed).
  - `test_old_match_record_loads_private`: match.json without `network` loads as private.
  - `test_runner_skips_nsenter_for_real_match`: `runner.engine_argv` of a real-match session has
    no nsenter prefix.
- [ ] **Step 2: Run** the three test files; Expected: new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Live check:** `match create` (real), a standalone and a client launched into it
  with the multiplayer probe mod (`probes/mp_probe_mod`), the client joins the standalone's game,
  both stop, `match remove` succeeds; then the same with `--network private` still works.
- [ ] **Step 5: Docs; Step 6: full unit tier; Step 7: Commit**
  `git commit -m "Matches use the real network by default; private matches are opt-in"`

### Task 3: Standalone web panel: unique port, never the default login

**Files:**
- Modify: `fsoctl/launcher.py` (`multi_cfg_text(game_name, web_port, user, password)`; the
  one-server-per-match rule's comment; session fields)
- Modify: `fsoctl/session.py` (fields `web_port`, `web_user`, `web_password`)
- Modify: `fsoctl/launcher.py` status/launch result (report `web_panel: {port, user, password}`
  for standalone sessions)
- Test: `tests/test_launcher_unit.py`, `tests/test_d_validate.py` or the audit-log test file

**Interfaces:**
- Produces: `launcher.web_port(wanted: int | None) -> int` (first free from 8080 across live
  standalones on the host); multi.cfg lines `+webapi_server_port <n>`, `+webapi_username <u>`,
  `+webapi_password <p>` (engine keys, code/network/multi_options.cpp:294-317); user
  `fsoh-<6 hex>`, password 24 url-safe random characters (`secrets.token_urlsafe(18)`).

- [ ] **Step 1: Write failing tests**
  - `test_multi_cfg_never_default_login`: the text contains `+webapi_username` and
    `+webapi_password`, neither value is `admin`.
  - `test_two_standalones_get_distinct_web_ports`: 8080 then 8081.
  - `test_launch_result_reports_web_panel`: a standalone launch result has `web_panel.port`,
    `.user`, `.password`.
  - `test_web_password_not_in_audit_or_journal`: after a launch and a status call, the audit log
    and the journal file do not contain the password string.
- [ ] **Step 2: Run; Expected FAIL. Step 3: Implement. Step 4: Run; PASS.**
- [ ] **Step 5: Live check:** a standalone in a real match; `curl -u <user>:<password>
  http://127.0.0.1:<web_port>/` answers, and `admin:admin` is refused.
- [ ] **Step 6: Commit** `git commit -m "Standalone web panel: per-session port and random login"`

### Task 4: CI workflow, unit tier on three OSes

**Files:**
- Create: `.github/workflows/tests.yml`
- Create: `probes/xplat/census.py` (runs the unit tier and writes `census-<os>.json`: per test
  file, passed/failed/errored counts and the first error line of each failure)
- Modify: `README.md` Tests section

**Interfaces:**
- Produces: workflow jobs `linux-unit` (gating: `pytest -q` must pass), `windows-census` and
  `macos-census` (non-gating: `continue-on-error: true`, upload `census-<os>.json` as an
  artifact). The census is probe 0: which existing tests already pass on Windows/macOS.

- [ ] **Step 1:** Write the workflow: Python 3.12, `pip install -e .`, LuaJIT installed per OS
  (apt `luajit`, choco `luajit`, brew `luajit`), then `pytest -q` (Linux) or
  `python probes/xplat/census.py` (Windows/macOS).
- [ ] **Step 2:** Push to the CI location chosen in Task 0; `linux-unit` must be green.
- [ ] **Step 3:** Download both census artifacts; commit them under
  `docs/research/xplat/census-windows.json` and `census-macos.json`.
- [ ] **Step 4: Commit** `git commit -m "CI: Linux unit tier, Windows and macOS census"`

### Task 5: Probe 1, hidden Windows desktop (rendering, input, UI Automation reach)

**Files:**
- Create: `probes/xplat/win_hidden_desktop.py`, `probes/xplat/gl_square.py` (a PySDL2 +
  PyOpenGL program that opens an SDL OpenGL 3.2 core window like the engine does and clears it
  to a known colour every frame)
- Create: `.github/workflows/probes.yml` (manual `workflow_dispatch`, one job per probe)

**Interfaces:**
- Produces: `probe-hidden-desktop.json` with fields `desktop_created`, `gl_context` (version
  string or error), `pixels_match` (bool from `glReadPixels` in the child), `capture_matches`
  (bool from PrintWindow of the child window), `uia_sees_window` (bool),
  `postmessage_key_reached` (bool), `fso_reached_gl_init` (bool, from fs2_open.log of the FSO
  26.0.1 x64-SSE2 release build started with `-window` on the hidden desktop, no game data),
  `notes`.

- [ ] **Step 1:** Create a desktop with `CreateDesktopW`, start `gl_square.py` on it with
  `CreateProcessW` (`STARTUPINFO.lpDesktop`), wait up to 20 s for its "ready" file.
- [ ] **Step 2:** Record each field above; run once with the runner's default OpenGL and once
  with Mesa's software `opengl32.dll` next to the program (mesa-dist-win release), as two result
  objects in the file.
- [ ] **Step 3:** Run on `windows-latest`; download the JSON into `docs/research/xplat/`.
- [ ] **Step 4:** If the owner's Windows 10 boot is available, run the same script there with
  the real GPU (manual, optional); record it as a second file.
- [ ] **Step 5: Commit**

### Task 6: Probe 2, Qt 6 controls in each accessibility layer

**Files:**
- Create: `probes/xplat/qt_standin.py` (PySide6: a dialog with a `QLineEdit` objectName
  `shipNameEdit`, a `QComboBox` `shipClassCombo` with three items, a `QPushButton` `okButton`
  text "OK", a `QTreeWidget` `eventTree` with two nested items, a `QLabel` buddy for the line
  edit; writes its widget values to a JSON file on OK)
- Create: `probes/xplat/a11y_probe.py` (one entry point; per-OS readers: UI Automation on
  Windows, AX API via pyobjc on macOS, pyatspi on Linux under Xvfb with an at-spi bus)

**Interfaces:**
- Produces: `probe-qt-a11y-<os>.json` with `object_name_exposed` (and as which property:
  AutomationId / AXIdentifier / accessible-id), `set_text_ok`, `combo_select_ok`, `press_ok`,
  `tree_walk_ok`, `values_after_ok` (the stand-in's written JSON), `needs_permission` (macOS),
  `notes`.

- [ ] **Step 1:** Write the stand-in and the probe; run the Linux variant locally under Xvfb.
- [ ] **Step 2:** Run on `windows-latest` and `macos-latest` through `probes.yml`.
- [ ] **Step 3:** Commit the three JSON results under `docs/research/xplat/`.

### Task 7: Probe 3, FRED2 in UI Automation

**Files:**
- Create: `probes/xplat/fred2_uia.py`

**Interfaces:**
- Produces: `probe-fred2-uia.json`: `window_found`, `dialog_controls` (list of
  {AutomationId, ControlType, Name} for the first dialog FRED2 shows), `automation_id_is_resource_id`
  (bool, compared with `fred2/fred.rc`), `notes`.

- [ ] **Step 1:** On `windows-latest`, start `fred2_open_26_0_1_x64_SSE2.exe` from the release
  zip with no game data; whatever window appears (main window or an error dialog), dump its
  control tree through UI Automation; close it.
- [ ] **Step 2 (owner's Windows 10 boot, optional, manual):** the same script against FRED2 with
  retail data, opening the Ship Editor (`IDD_SHIP_EDITOR`), setting the ship name field and
  pressing OK; record the result as a second file.
- [ ] **Step 3:** Commit results.

### Task 8: Probe 4 and 5, macOS permission and FSO on runners

**Files:**
- Create: `probes/xplat/mac_ax_permission.py`, `probes/xplat/fso_start.py`

**Interfaces:**
- Produces: `probe-mac-ax.json` (`ax_trusted`, how trust was obtained if at all, whether the
  Qt stand-in's tree was readable); `probe-fso-start-<os>.json` (`exit_code`, `seconds`,
  `log_tail`, `reached_gl_init`, `bound_udp_port`) for the 26.0.1 release on each OS with
  `-window` and no data.

- [ ] **Step 1:** macOS: `AXIsProcessTrusted()`, then attempt to read the stand-in (Task 6);
  record what the runner allows.
- [ ] **Step 2:** All three OSes: start the release engine, wait up to 30 s, record the fields.
- [ ] **Step 3:** Commit results.

### Task 9: Probe 6, per-session audio through OpenAL Soft's wave writer

**Files:**
- Create: `probes/xplat/openal_wave.py`

**Interfaces:**
- Produces: `probe-openal-wave-<os>.json`: `openal_is_soft` (from the bundled library's
  version string), `wave_file_written` (bool), `wave_bytes`, `notes`. Method: a per-session
  `alsoft.conf` named by `ALSOFT_CONF`, `drivers = wave`, `[wave] file = <session>/audio.wav`.

- [ ] **Step 1:** Run on Linux locally with the engine's own libopenal, then on the Windows and
  macOS runners with the release's bundled OpenAL; a tiny OpenAL program plays a tone.
- [ ] **Step 2:** Commit results.

### Task 10: Results note and inputs for Plan B

**Files:**
- Create: `docs/research/2026-10-16-xplat-probe-results.md`
- Modify: `docs/superpowers/specs/2026-10-16-cross-platform-design.md` (Probes section: each
  probe's outcome, one line, linked to its JSON)

- [ ] **Step 1:** One section per probe: what was run, the result, what it means for Plan B
  (which design option it keeps or rules out).
- [ ] **Step 2:** Update the design's probe list with outcomes; update this plan's Status block.
- [ ] **Step 3: Commit;** hand the note to the owner before Plan B is written.

## Deviations from the plan as written (Phase 1, recorded 2026-10-17)

- **Order of the live checks (other).** Task 2's live check ran after Task 3 was built, not
  before Task 2's commit: a standalone in a real match before Task 3 would have opened the
  engine's web panel on the LAN with admin/admin, against the global constraint. Both tasks were
  committed locally, nothing pushed, and the checks ran together on the Task 3 code. Effect: none
  on behaviour; Task 2 has no separately-recorded live check.
- **Task 3 live check path (other).** The plan says `curl ... http://127.0.0.1:<web_port>/`; the
  check used `/api/1/auth`, a resource that asks for the login; `/` cannot tell a good login
  from admin/admin. Effect: the check measures what the plan meant.
- **MCP `match_create(network=...)` (scope-change, small).** Task 2's file list does not name
  mcp/server.py. Without the parameter, private matches would be unreachable from MCP, which the
  global constraint "nothing that works on Linux today is removed" forbids. Effect: MCP callers
  can still make private matches; the default is real, as on the CLI.
- **A real match with no running server refuses a client (behavioral-change).** Not in the plan.
  The private-match fallback (127.0.0.1:7808) is wrong on the host, where 7808 may be another
  match's server or a real game. Effect: launch the server first, or pass `--connect`.
- **Only a client asks for a server to join (behavioral-change, bug fix).** `_prepare` used to
  compute a default connect address for every match role; with the refusal above, launching a
  standalone into an empty real match failed. Caught by a cold re-read before the Task 2 commit;
  pinned by tests/test_match_real.py::test_prepare_a_real_match_server_then_client.
- **The 24 h `--runtime` cap applies to private matches only (behavioral-change).** It exists
  because a private match's holder lives 24 h; a real match has no holder. Effect: real-match
  sessions may run longer, like ordinary sessions.
- **web_port counts only real-network standalones; a private standalone always gets 8080
  (other).** The plan says "first free from 8080 across live standalones on the host"; a private
  standalone's panel is inside its own namespace and holds nothing on the host. Effect: none
  outside the namespace.
- **ruff fix in fsoctl/scope.py (scope-change, separate commit 0f18b38).** Three lines from
  5eee358 left `ruff check .` failing on main; wrapped, no behaviour change.

### Changes the QA review made (Phase 1, 2026-10-17; behavioural unless noted)
- Every standalone gets `+webui_root` = an empty 0700 folder in its run folder (QA SA-1). Not in
  the plan; added by the QA review.
  A runtime path whose folders end in .cgi/.pl/.php, or whose root line would not
  fit the engine's 512-byte config line, is refused (XFA-1, XFA-2).
- Host ports are test-bound before they are handed out (port_free), and readiness and
  `web_panel` count only a socket owned by the session's engine pid (engine_holds). A real-network
  HOST whose engine lost its port fails the launch; other real sessions get a warning (QA CR-1
  group, XFA-3). The plan's host_port/web_port specified records only.
- `web_port` lost its `wanted` parameter (no caller; QA CR-8). Interface change from the plan.
- An empty real match ends a day after creation (match.observe writes "dead" once); a "starting"
  record within the launch grace counts as live (QA CR-4 group, XFA-4). Not in the plan.
- A replayed record with no network field runs on the real network (QA CR-5/TA-10); the plan's
  Review Focus 3 said such a SESSION record was private, which was wrong for sessions.
- Game name, --connect and mission are checked as whole values (fullmatch).
- CI (other): ruff step, `permissions: contents: read`, the Chocolatey luajit step removed (no
  such package). Unit tier: conftest stubs port_free (marker `host_ports` for its own tests).

## Deviations from the plan as written (Phase 2, Task 5, recorded 2026-10-25, before the code)

- **FastDebug engine instead of the release engine (other).** Task 5 says the "x64-SSE2 release
  build"; the probe starts `fs2_open_26_0_1_x64_SSE2-FASTDBG.exe` from the same zip. Release
  builds log nothing (`LoggingEnabled` is false under NDEBUG; fso/CLAUDE.md), so
  `fso_reached_gl_init` could not be read from fs2_open.log. A local run of the Debug build with
  no game data (2026-10-25) wrote "... OpenGL init is complete!" and then stopped at a modal error
  ("Web cursor bitmap not found", 2d.cpp:2338, inside gr_init), so the line is reached without
  data. Effect: measures what the plan meant. Scope audit: SHIP.
- **Four result objects, not two (other).** Each GL variant (runner default, Mesa) is also run on
  the runner's normal desktop as a control, so a `false` on the hidden desktop can be told apart
  from a bug in the probe. Every field carries its error text. Effect: more evidence, same fields.
- **Mesa for the Python stand-in via SDL_OPENGL_LIBRARY (other).** "Next to the program" would
  mean next to python.exe; the stand-in instead points SDL at Mesa's opengl32.dll and puts the
  Mesa folder on PATH for its dependencies. The engine gets the DLLs next to its exe as planned.
  The stand-in reports GL_RENDERER, so which OpenGL it got is in the result.
- **Engine processes stuck in an error dialog ignore SIGTERM (finding, Linux).** The no-data run
  above survived `timeout`'s TERM while its error box was up, and exited only once its X display
  was gone. The probe ends engines with TerminateProcess; the harness's own stop path already
  escalates past TERM [unverified this session].
- **Task 5: the engine starts with -nosound -nomusic (other).** The second CI run showed the
  engine's log stopping at "Initializing OpenAL..." in every cell, before graphics (the runner
  has no audio device). Sound is probe 6's question; with it off, snd_init returns at once.
- **Task 6: Linux session script (other, small scope addition).** `linux_a11y_session.sh` and
  `a11y-session.conf` give the Linux run its Xvfb, a private D-Bus bus that cannot auto-start
  services, and an AT-SPI registry started by hand. The first local try on a normal private bus
  woke the desktop's portal services (one orphan, stopped); the plan's "under Xvfb with an at-spi
  bus" needed this to be repeatable. Scope audit: SHIP.
- **Task 6: `qt_version` field (other).** Each result records the Qt it measured.
- **Task 8 Step 1 reuses Task 6 (other).** `needs_permission` (AXIsProcessTrusted) is recorded
  in probe-qt-a11y-macos.json; Task 8 points to it instead of measuring it again (scope audit).
- **Task 7: FRED2 runs with ALSOFT_DRIVERS=null; only IDC_ names count as a match (other).**
  The first run's window was SDL's "OpenAL 1.1 or newer is required" box (no audio device on the
  runner), and its OK button (AutomationId 100) matched IDD_ABOUTBOX (100) by coincidence. OpenAL
  Soft's null backend (documented in its env-vars.txt; "null" in alc.cpp's backend list) gets
  FRED2 past the audio check; only IDC_ control IDs count, negative IDs included.
- **Task 8: FastDebug engine, -nosound, FSO_PREFERENCES_PATH (other).** The release engine
  writes no log, so `log_tail`/`reached_gl_init` need FastDebug (same as Task 5). Sound is off for
  probe 1's reason. FSO_PREFERENCES_PATH (osapi.cpp at 26.0.1) puts the log in the run's own
  folder on every OS. The Linux leg was run locally first (reached GL init; no UDP port, as the
  engine opens it after graphics, which no-data stops inside).
- **Task 9: the safety gate is the wave file, not the device name (other).** OpenAL Soft 1.23.1
  names the wave writer's device "OpenAL Soft" (newer versions: "Wave File Writer"); the writer
  creates audio.wav when the context comes up, so the tone plays only once that file exists. Local
  Linux run: 1.04 s, 48 kHz stereo float, peak -10.7 dB (ffmpeg volumedetect).

### Changes the QA review made (Phase 2, 2026-10-25; record qa-xplat-plan-a-probes/)
- Probe 1 runs the engine with a preferences folder of its own and Mesa from a copy of the
  engine, so a manual run on a real install never touches the user's profile or game folder
  (CR-6, TA-5). It records gl_meets_minimum, and writes a hidden row even without a desktop.
- Probe 2 tries two selection methods per OS and records each return; qt_saw_name/qt_saw_class
  compare with what Qt holds. Probe 3 sorts ids into fred2/windows/combo_child/other. Probe 6
  reads the WAV's peak. Probe 5 lists all UDP ports and survives a failed start.
- The Linux accessibility run uses xvfb-run -a, a private runtime folder and a sentinel argument.
- Workflow: actions pinned to commit SHAs, persist-credentials false, pyobjc set pinned,
  fso-start installs psutil only. tests.yml still pins actions by tag (outside this range).
- FRED2's job gets Mesa and OpenAL's null output, so FRED2 gets past its first two checks; the
  engine-start Linux job installs libopenal1 (the AppImage does not bundle it).

## Appendix: Deferments originated in Phase 2
- **DEF-PA-01** · Found by: mutation pass (MU-7) · Commit: final commit of the Phase 2 QA ·
  Defer target: Plan B (platform layer) · Severity: LOW · What: 147 mutants survive in
  win_hidden_desktop.one_cell / fso_check and openal_wave.run (note text and Windows- or
  OpenAL-only branches the Linux unit tier cannot reach). · Why deferred: these are one-off
  measurement scripts whose outputs are read field by field from real CI runs; simulating Win32
  and OpenAL in unit tests costs more than the probes. · Fix direction: when Plan B moves any of
  this logic into product code, it gets real per-platform tests there. · Obsolete when: the
  probes are retired or replaced by product code.
