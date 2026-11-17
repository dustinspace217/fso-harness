# Cross-platform Plan B: platform layer and Windows game sessions

## Status (updated 2026-11-17, 05:00 PST)
Phase: 3 of 3, done (all eleven tasks; the review is closed)
Done: Tasks 1-11. Review round 1 (103 finding IDs; fix waves 24bbe7c, 496f197, 0bbfee7); round 2 on those fixes (four seats, two outside reviewers, mutation runs; fix waves 6d1b1b0, 8fb4bb5; a cross-examination with no blocker or major); a closing commit (2ee0348) after CI's Windows machine showed that a just-ended session's log is still "in use" for an instant, read afterwards by two seats. Final tree: Linux unit tier 1850 passed; Windows VM 1101 unit tests (110 skipped) and 23 live; CI green on Linux, Windows and the macOS census at 2ee0348 (run 44086283285; the commit after it changes comments and documents only). Synthesis and receipt: the artifacts folder beside this repo, fso-harness-artifacts/qa-plan-b/synthesis.md
Next: the owner's choice of what to do with the branch (it is on the private CI remote only; nothing has gone to a public remote)
Blocked: nothing. For the owner: DEF-XB-12 (a proposal that reverses one load-bearing decision) and DEF-XB-22 (a real graphics card on a hidden desktop is unmeasured; the spec says measure first)

Owner ruling for Plan C, recorded here so it is not lost (2026-11-16 21:56, verbatim): "If the
harness needs to use qtFRED for best results, that's acceptable. We just have to note it in
the agent instructions." Plan B refuses every editor verb on Windows, so nothing in this plan
changes; Plan C may make qtFRED the Windows editor and must say so in the MCP instructions
and AGENT-GUIDE.

Phases: 1 = Tasks 1-4 (platform layer), 2 = Tasks 5-10 (Windows sessions), 3 = Task 11
(CI, documents, review, close-out). Progress ledger while building:
.superpowers/sdd/2026-11-16-cross-platform-plan-b/progress.md (git-ignored).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal (quoted):** "More or less the same functionality we have today on Linux, except where OS
limitations force disparity." This plan delivers the first part of that on Windows: the same
`fsoctl` and MCP verbs launch, drive, capture and stop a game session there, out of sight.

**Architecture:** A `fsoctl/platform/` package picks, once, by `sys.platform`, one module per
facility (process groups, process facts, locks, folders, safe file access, runner, capture).
Linux modules stay where they are and are selected unchanged; each gets a Windows module with
the same public names. A Windows session is a runner process started on a hidden desktop of its
own inside a named Job Object; the runner starts the engine, which inherits both.

**Tech Stack:** Python 3.12 standard library only (`ctypes` for Win32; no new package on any
OS), pytest, GitHub Actions (windows-latest for the unit tier and OS-level tests), the local
Windows 11 VM for engine tests with retail data (fso-harness-artifacts/xplat-vm-2026-11-16/).

**Spec:** docs/superpowers/specs/2026-10-16-cross-platform-design.md (approved 2026-10-16).
Evidence: docs/research/2026-10-16-xplat-probe-results.md (probes 1-6) and
docs/research/2026-11-16-xplat-vm-results.md (probes 7-9, VM). Call-site map:
docs/research/2026-11-16-linux-only-inventory.md (an agent's map: verify each line before use).

## Scope (the builder's call; check this)

In: spec phase 1's platform layer and spec phase 2 (Windows sessions: launch, command
channel, state/lua/sexp/pilot/wait, screenshots, stop, cleanup, crash reports, builds, hidden
desktop), plus the MCP server and install scripts on Windows.

Not in, each refused on Windows with an error that names it (never a silent no-op): editor
sessions and every `fred_*` verb, the input verbs (`keys`, `click`, `drag`, `move`, `scroll`),
`--sound`, matches, `--network private`, `--network sandbox`. They are spec phases 3 and 5 and
get their own plans: Plan C (editors: qtFRED and FRED2 on Windows, the accessibility method on
Linux), Plan D (macOS), Plan E (multiplayer and sound on Windows/macOS). Today's FRED2 and
combo-box results are Plan C's input and are recorded in the VM results note.

## Calls made in this plan (not the owner's; check these)

1. **Linux files do not move.** The spec says "moved, not rewritten". Selecting the existing
   modules (`scope.py`, `safefs.py`, `runner.py`) from `fsoctl/platform/__init__.py` leaves
   every Linux test and patch target untouched; only code that sits inside mixed modules
   (launcher, match, session, paths) moves, into `fsoctl/platform/linux_*.py`.
2. **Windows input verbs refuse in Plan B, in visible sessions too.** Probe 9: posted key
   presses reached the engine only when its window was the foreground one. They failed on a
   hidden desktop (where the window cannot be made the foreground one) and also on the normal
   desktop with another window in front; the same engine took the same keys once brought to
   the front. Real input for `--visible` sessions is Plan C work
   (it shares code with editor driving).
3. **A Windows session is not sandboxed and `safefs` there only refuses links.** The engine
   runs as the user, so a hostile engine build can already write anything the user can; the
   Linux guarantee (QA SA-1) cannot be given. README Limits says so.
4. **Engine tests with game data run in the local VM, not on CI** (no retail data on CI; the
   engine stops at a missing-data error without it). CI gets the unit tier and OS-level tests.
5. **Windows `crash_report` gives the dump file, not a backtrace**, unless `cdb.exe` is
   installed. The engine writes `<exe name>_<UTC stamp>.mdmp` beside its executable
   (code/globalincs/crashdump.cpp at 26.2.0).

## Global Constraints

- Linux install stays dependency-free (`dependencies = []`); Plan B adds no package on any OS.
- "Linux behaviour is the reference: its existing tests keep passing unchanged." A test file
  may be edited only where a patch target moved, with no change to what it asserts.
- Nothing that works on Linux today is removed or weakened.
- No silent fallbacks: an unavailable feature raises `HarnessError(EXIT_REFUSED, ...)` naming
  the feature and the platform.
- Sessions run out of sight by default: a hidden session never takes the user's keyboard,
  mouse or focus, never appears in the taskbar or Alt-Tab, never raises a window.
- Real network is the default on every OS; every engine gets its own `-port`.
- No retail game data in CI or in the repository.
- The MCP server stays a thin, stateless layer over `fsoctl --json`; no daemon. State is the
  run folder plus the OS's own process group (systemd scope / Job Object).
- The command channel, the Lua bridge and the game verbs stay platform-neutral.
- Windows-only calls are made inside functions, never at import time: every `win_*.py` module
  must import on Linux (the parity test loads them there).
- Every loop and wait has an explicit bound (Power of Ten rule 2); functions stay under ~60
  statements; `ruff check .` clean.
- Engine tests on Linux always via `bounded-run`.

## Review Focus

1. A `fsoctl launch` killed halfway on Windows (job made, runner not yet started, or runner
   started and engine not): the next `cleanup` ends the job and removes the run folder; nothing
   keeps running. Pinned in Task 5.
2. A recorded pid that now belongs to an unrelated process: `stop` and `status` never signal or
   report it as the session's. Windows compares the process creation time. Pinned in Task 2.
3. A run folder or game folder with spaces or non-ASCII letters (`C:\Users\José Müller\...`):
   launch, the channel and screenshots work. Pinned in Tasks 4 and 5.
4. Two `fsoctl launch` calls at the same moment on Windows: the lock serialises them and they
   get different game ports. Pinned in Tasks 1 and 5.
5. Every refused feature (`--sound`, `--network private|sandbox`, `match_create`, `kind=qtfred`,
   `keys`, `click`, `drag`, `move`, `scroll`, `fred_*`) exits 7 with a message naming the
   feature, the platform and where it is tracked; never a traceback. Pinned in Task 1.

## File structure

| File | Responsibility |
|---|---|
| `fsoctl/platform/__init__.py` | Picks each facility module by `sys.platform`; `NAME`; `unsupported()` |
| `fsoctl/platform/linux_procs.py` | Moved from launcher/match: pid facts, socket owners, loaded libraries |
| `fsoctl/platform/linux_locks.py` | Moved from session: the flock primitive |
| `fsoctl/platform/linux_dirs.py` | Moved from paths: default runtime, state and sandbox roots |
| `fsoctl/platform/win_api.py` | The ctypes signatures every `win_*` module shares (one place) |
| `fsoctl/platform/win_procs.py`, `win_locks.py`, `win_dirs.py` | Windows twins of the three above |
| `fsoctl/platform/win_scope.py` | Job Objects behind `scope.py`'s public names |
| `fsoctl/platform/win_safefs.py` | `safefs.py`'s public names; refuses reparse points |
| `fsoctl/platform/win_desktop.py` | Hidden desktop: create, name, open, run a function on it |
| `fsoctl/platform/win_runner.py` | The per-session runner on Windows |
| `fsoctl/platform/win_capture.py` | PrintWindow to PNG (pure-Python PNG writer) |
| `fsoctl/platform/win_crash.py` | Finds the engine's `.mdmp`; optional `cdb` backtrace |
| `mcp/setup.ps1`, `mcp/register.ps1` | PowerShell twins of the two shell scripts |
| `tests/test_platform_*.py`, `tests/winlive/` | Unit tests (all OSes) and OS-level tests (Windows CI) |

---

### Task 1: The platform package, refusals, locks and folders

**Files:**
- Create: `fsoctl/platform/__init__.py`, `linux_locks.py`, `win_locks.py`, `linux_dirs.py`,
  `win_dirs.py`, `win_api.py`
- Modify: `fsoctl/session.py` (file_lock, update, next_seq use `platform.locks`),
  `fsoctl/paths.py` (roots use `platform.dirs`), `fsoctl/launcher.py` (`_validate`: refusals),
  `fsoctl/match.py`, `fsoctl/xinput.py`, `fsoctl/fred.py` (verb entry points: refusals)
- Test: `tests/test_platform_select.py`, `tests/test_platform_parity.py`,
  `tests/test_platform_refusals.py`

**Interfaces:**
- Produces: `platform.NAME: str` ("linux" | "windows" | "macos");
  `platform.unsupported(feature: str, tracked: str) -> NoReturn` (raises
  `HarnessError(EXIT_REFUSED, f"{feature} is not available on {NAME}", f"tracked in {tracked}")`);
  facility attributes `platform.scope`, `procs`, `locks`, `dirs`, `safefs`, `RUNNER_MODULE`.
- `locks`: `lock_exclusive(f) -> bool` (non-blocking try; True when taken), `unlock(f) -> None`.
  Linux: `fcntl.flock(f, LOCK_EX | LOCK_NB)`. Windows: `msvcrt.locking(f.fileno(), LK_NBLCK, 1)`
  at offset 0 (seek first); the OS drops it when the holder dies, as flock does.
- `dirs`: `runtime_root() -> Path`, `state_root() -> Path`, `sandbox_root() -> Path` (defaults
  only; the `FSO_HARNESS_RUNTIME` / `_STATE` / `_SANDBOX_ROOT` overrides stay in paths.py).
  Windows: `%LOCALAPPDATA%\fso-harness\run`, `\state`, `\sandbox`.

- [ ] **Step 1: Failing tests**
  - `test_linux_selects_the_existing_modules`: on Linux `platform.scope is fsoctl.scope` and
    `platform.safefs is fsoctl.safefs`, `platform.RUNNER_MODULE == "fsoctl.runner"`.
  - `test_windows_selects_the_win_modules` (monkeypatch `sys.platform = "win32"`, reload):
    `platform.scope.__name__ == "fsoctl.platform.win_scope"`,
    `RUNNER_MODULE == "fsoctl.platform.win_runner"`.
  - `test_every_facility_pair_has_the_same_public_signatures`: for each (linux, windows)
    module pair, the sets of public function names are equal and `inspect.signature` matches
    name by name. (Fails until each later task adds its twin; mark the pairs not yet built
    `xfail(strict=True)` and remove the mark in the task that builds them.)
  - `test_win_modules_import_on_linux`: importing every `fsoctl.platform.win_*` succeeds.
  - `test_unsupported_names_feature_platform_and_tracker`: message
    `"--sound is not available on windows"`, hint contains `"Plan E"`, exit code 7.
  - `test_each_refused_feature_refuses_on_windows` (parametrised over the Review Focus 5 list,
    `platform.NAME` patched to "windows"): each raises EXIT_REFUSED before touching the OS.
  - `test_file_lock_times_out_while_another_process_holds_it` (real child process, runs on
    every OS): the second taker gets EXIT_TIMEOUT after its timeout; after the holder is
    killed, the lock is free again.
  - `test_windows_roots_come_from_localappdata` (env patched).
- [ ] **Step 2:** `bounded-run python3 -m pytest -q tests/test_platform_select.py tests/test_platform_parity.py tests/test_platform_refusals.py` → the new tests FAIL.
- [ ] **Step 3:** Implement. `session.file_lock` keeps its loop and messages and calls
  `locks.lock_exclusive` / `locks.unlock`. No `import fcntl` remains outside `linux_locks.py`.
- [ ] **Step 4:** Full unit tier on Linux `bounded-run python3 -m pytest -q` → all pass, no test
  file edited beyond patch targets (list each edited target in the commit message).
- [ ] **Step 5:** Commit `Platform package: facility selection, refusals, locks, folders`.

### Task 2: Process facts (`procs`)

**Files:**
- Create: `fsoctl/platform/linux_procs.py`, `fsoctl/platform/win_procs.py`
- Modify: `fsoctl/launcher.py` (alive, `_in_scope`, `_gone`, `_stop_runner_first`,
  `engine_holds`, the `renderer.read_maps` call), `fsoctl/match.py` (pid_start),
  `fsoctl/renderer.py` (classify learns Windows library names)
- Test: `tests/test_platform_procs.py`; patch targets in `tests/test_b_launcher.py`,
  `tests/test_host_ports.py`, `tests/test_match_unit.py`

**Interfaces:**
- `pid_alive(pid: int) -> bool`. Linux `os.kill(pid, 0)`. Windows `OpenProcess(
  PROCESS_QUERY_LIMITED_INFORMATION)` + `GetExitCodeProcess == STILL_ACTIVE (259)`. (On Windows
  `os.kill(pid, 0)` does not probe: it terminates the process.)
- `pid_start(pid: int) -> int | None`: a value that differs for two processes that reuse a pid.
  Linux `/proc/<pid>/stat` field 22; Windows `GetProcessTimes` creation FILETIME as an int.
- `in_group(pid: int, unit: str) -> bool`. Linux `/proc/<pid>/cgroup`; Windows
  `IsProcessInJob(process, OpenJobObjectW(unit))`.
- `gone(pid: int) -> bool` (absent or a zombie).
- `ask_stop(sess) -> str | None`: ask the session's runner to end the engine and exit; returns
  None or a "skipped:<why>". Linux `os.kill(runner_pid, SIGTERM)` after `in_group`. Windows
  writes `<run folder>\stop` (the runner polls for it; Task 5).
- `socket_owner_pids(proto: str, port: int) -> set[int]` (proto "udp" | "tcp"). Linux parses
  `ss -H -lunp/-ltnp` (the existing parser, moved); Windows parses `netstat -ano -p UDP|TCP`
  (columns: proto, local address, [state,] pid; match the port after the last `:`).
- `loaded_libraries(pid: int) -> str`: Linux `/proc/<pid>/maps`; Windows the module file names
  from `EnumProcessModulesEx` + `GetModuleFileNameExW`, one per line.
- `renderer.classify` additionally maps `nvoglv64.dll`, `atio6axx.dll`, `ig*icd64.dll` →
  "opengl (hardware)", `libgallium_wgl.dll` → "opengl (software, Mesa)", `vulkan-1.dll` →
  "vulkan"; the Linux names and results are unchanged.

- [ ] **Step 1: Failing tests** (Windows functions with `win_api` calls stubbed, in the style
  of tests/test_b_scope.py)
  - `test_netstat_udp_row_gives_the_owner_pid`: text
    `"  UDP    0.0.0.0:7821           *:*                                    4242\n  UDP    [::]:7821              *:*                                    4242\n  UDP    0.0.0.0:78210          *:*                                    9\n"`
    → `socket_owner_pids("udp", 7821) == {4242}` (7821 must not match 78210).
  - `test_netstat_tcp_counts_only_listening_rows`.
  - `test_a_reused_pid_is_not_the_runner` (Review Focus 2): session record with
    `runner_pid=10, runner_start=111`; `pid_start(10)` now 222 → `launcher.stop` reports
    `runner_first == "skipped:runner not running in the session's scope"` and never calls
    `ask_stop`. Needs `Session.runner_start: int | None = None` (old records load as None and
    fall back to `in_group` alone).
  - `test_pid_alive_false_for_an_exited_process_handle_still_open` (exit code != 259).
  - `test_classify_reads_windows_library_names` (three rows above).
- [ ] **Step 2:** run them → FAIL.
- [ ] **Step 3:** Move the Linux code (bodies unchanged) and write the Windows twins.
- [ ] **Step 4:** Linux unit tier → all pass. `bounded-run python3 -m pytest -m engine -p no:xdist -q tests/test_launch_engine.py` → pass (stop still ends the runner first, no core dump).
- [ ] **Step 5:** Commit `procs: process facts behind the platform layer`.

### Task 3: Safe file access on Windows

**Files:** Create `fsoctl/platform/win_safefs.py`; Test `tests/test_platform_safefs.py`

**Interfaces:** every public function of `fsoctl/safefs.py`, same signatures (the parity test
enforces it). Each walks from the session's prefs folder one path component at a time and
raises the same `HarnessError` safefs raises when a component is a reparse point
(`os.lstat(p).st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT`: symlinks and junctions);
reads accept regular files only and keep safefs's size caps; writes are a temp file created
with `O_EXCL` in the same folder, then `os.replace`.

- [ ] **Step 1: Failing tests** (run on every OS: `st_file_attributes` is stubbed on Linux
  through one module-level helper `_is_link(path) -> bool`)
  - the red proofs of tests/test_safefs.py that carry over, by name:
    a planted link in the channel folder is refused for read; for write; a link as a parent
    folder is refused; a file over the cap is refused; a write never leaves a half-written file.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** run → PASS; parity test for
  the safefs pair loses its xfail.
- [ ] **Step 5:** Commit `win_safefs: link-refusing file access for Windows sessions`.

### Task 4: Process groups on Windows (`win_scope`)

**Files:** Create `fsoctl/platform/win_scope.py`; extend `win_api.py`;
Test `tests/test_platform_scope.py`, `tests/winlive/test_job_live.py`

**Interfaces:** `scope.py`'s public names, same signatures:
`caps`, `start`, `is_active`, `active_units`, `state`, `is_gone`, `stop`, `room_for_engine`,
`mem_cap_kb`.
- `caps(...)` returns the same `"Key=Value"` strings as Linux (launcher reads `MemoryMax=`);
  available memory comes from `GlobalMemoryStatusEx().ullAvailPhys`.
- `start(unit, argv, env, log_path, props, desktop: str | None = None)`: `CreateJobObjectW(
  name=unit)`; limits from props: `MemoryMax` → `JOB_OBJECT_LIMIT_JOB_MEMORY`, `TasksMax` →
  `JOB_OBJECT_LIMIT_ACTIVE_PROCESS`, `CPUQuota` → `JobObjectCpuRateControlInformation` hard cap
  (percent of all cores × 100); NOT `KILL_ON_JOB_CLOSE` (the launching CLI exits while the
  session runs). Then `CreateProcessW(argv, CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP |
  CREATE_NO_WINDOW, lpDesktop=desktop, stdout/stderr=log_path)`, `AssignProcessToJobObject`,
  `ResumeThread`. Returns an object with `.pid`. The Linux `start` gains the same keyword and
  ignores it (the only signature change to a Linux function in this plan).
  `RuntimeMaxSec` is enforced by the runner (Task 5); `stop` is the backstop.
- A job exists while a process is in it: `is_active(unit)` = `OpenJobObjectW(unit)` succeeds
  and `JobObjectBasicAccountingInformation.ActiveProcesses > 0`. `state` returns "active" or
  "inactive". `active_units()` cannot enumerate jobs by name: it returns the units of the
  session folders under `runtime_root()` whose job is active.
- `stop(unit, timeout)`: `TerminateJobObject(job, 1)`, then wait (bounded by timeout) for
  `ActiveProcesses == 0`.

- [ ] **Step 1: Failing unit tests** (win_api stubbed): props → limit fields
  (`MemoryMax=2097152K` → 2147483648 bytes; `TasksMax=512`; `CPUQuota=800%` on a 16-core
  machine → rate 5000); `start` never sets KILL_ON_JOB_CLOSE; argv with a space and a quote is
  quoted by `subprocess.list2cmdline`; `is_active` false when the job cannot be opened.
- [ ] **Step 2: Failing OS-level tests** (`tests/winlive/`, marker `winlive`, skipped off
  Windows): a python child started through `win_scope.start` in a folder named
  `José Müller's run` (Review Focus 3) is `is_active`; it survives the starting process
  exiting (start it from a helper process that exits); `stop` ends it and a grandchild it
  spawned; a child that allocates past `MemoryMax=200000K` is ended by Windows; a third process
  with `TasksMax=2` fails to start.
- [ ] **Step 3:** implement. **Step 4:** unit tests pass on Linux; winlive passes on the VM:
  `xrun.sh 'C:\xplat\py312\python.exe -m pytest -q -m winlive tests\winlive'`.
- [ ] **Step 5:** Commit `win_scope: Job Objects behind scope's interface`.

### Task 5: A Windows session: hidden desktop, runner, launch, stop, cleanup

**Files:**
- Create: `fsoctl/platform/win_desktop.py`, `fsoctl/platform/win_runner.py`
- Modify: `fsoctl/launcher.py` (`_start`: runner module and desktop from the platform;
  `_check_role`: the root check is Linux-only; `_stop_runner_first` uses `procs.ask_stop`;
  `--visible` flag; cleanup ends leftover jobs), `fsoctl/session.py` (`Session.visible: bool =
  False`, `Session.desktop: str | None = None`), `fsoctl/envsetup.py` (a Windows branch)
- Test: `tests/test_platform_desktop.py`, `tests/test_win_runner.py`,
  `tests/winlive/test_desktop_live.py`

**Interfaces:**
- `win_desktop.name_for(sid) -> str` = `f"fso-harness-{sid}"`;
  `create(name) -> handle` (`CreateDesktopW(name, ..., GENERIC_ALL)`);
  `path(name) -> str` = `f"{window station name}\\{name}"` (what `lpDesktop` takes);
  `on_desktop(name, fn)`: run `fn()` in a fresh thread after `OpenDesktopW` +
  `SetThreadDesktop`, return its value (how a later CLI call reaches a hidden session's
  windows; probe 1's pattern). A desktop exists while a process runs on it or a handle is open.
- Launch on Windows: `launch` creates the desktop (unless `--visible`), starts
  `python -m fsoctl.platform.win_runner <sid>` through `win_scope.start(..., desktop=path)`,
  then closes its desktop handle. Readiness is the channel heartbeat, as on Linux.
- `win_runner.main(sid) -> int`: records `runner_pid` and `runner_start`; builds the child
  environment (`envsetup.child_env` Windows branch: `FSO_PREFERENCES_PATH=<prefs>\`, nothing
  else changed); starts the engine with `launcher.build_argv(sess)` (a child with no desktop
  named inherits the runner's); waits in a loop of `RUNNER_POLL_S = 0.5` for the first of:
  engine exit, the `stop` file, `runtime_sec` reached; on stop or runtime it posts `WM_CLOSE`
  to the engine's top-level window, waits `ENGINE_CLOSE_WAIT_S = 20`, then `TerminateProcess`;
  writes `exit_info` and `ended_by` exactly as the Linux runner does (`_record_exit`'s fields).
- `--visible`: the runner is started on the user's desktop (`lpDesktop=None`).

- [ ] **Step 1: Failing unit tests**
  - `test_runner_stops_on_the_stop_file`, `test_runner_ends_the_engine_at_runtime_sec`,
    `test_runner_records_exit_code_and_cause` (engine = a stub Popen; poll loop driven by a
    fake clock): `ended_by` is "stop" / "runtime_limit" / None and `exit_info.code` is set.
  - `test_windows_child_env_sets_only_the_prefs_path` (no HOME/XDG/DISPLAY keys added).
  - `test_launch_on_windows_names_the_desktop_and_the_win_runner` (scope.start captured):
    `desktop == "WinSta0\\fso-harness-<sid>"`, argv is `[sys.executable, "-m",
    "fsoctl.platform.win_runner", sid]`; with `visible=True` → `desktop is None`.
  - `test_cleanup_ends_a_job_whose_run_folder_says_starting_and_is_past_the_grace`
    (Review Focus 1): record status "starting", older than `_STARTING_GRACE`, job active →
    `scope.stop` called and the folder removed.
  - `test_root_check_is_skipped_on_windows` (no `os.getuid` call).
  - `test_paths_with_spaces_and_accents_survive_the_runner_command_line` (Review Focus 3):
    run folder `C:\Users\José Müller\AppData\Local\fso-harness\run\s0000abcd` and a game
    folder `D:\My Games\FS2` → the engine's argv and `FSO_PREFERENCES_PATH` carry both
    unchanged, and `list2cmdline` of the argv round-trips through `CommandLineToArgvW`
    (winlive half of the test).
- [ ] **Step 2: Failing OS-level tests** (`winlive`): a window made by a child started on
  `fso-harness-test` is found by `on_desktop(...)` + `EnumWindows` and is NOT among the windows
  enumerated on the default desktop; the desktop is gone after the child exits (a second
  `OpenDesktopW` fails).
- [ ] **Step 3:** implement.
- [ ] **Step 4: Live check in the VM with retail data** (the engine tier's Windows form):
  `fsoctl --json launch --binary C:\FS2\fs2_open_26_2_0_x64_SSE2-FASTDBG.exe --mission sm1-01
  --allow-eval` with every skip flag → status "running"; `state` returns a game state; `lua
  "return mn.getMissionFilename()"` returns the mission; `wait_for` a state; a VM screenshot
  (QMP screendump) taken while it runs shows no game window, no taskbar button;
  `Get-Process | ? MainWindowTitle` on the default desktop does not list it; the user's
  `%APPDATA%\HardLightProductions\FreeSpaceOpen` has no file newer than the launch; `stop`
  → "stopped", `ended_by: "stop"`, job inactive, no `fs2_open*.exe` left. Then two launches
  started at the same moment (two `xrun` jobs) → two sessions, ports 7808 and 7809 in
  `netstat -ano -p UDP` owned by the two engines (Review Focus 4). Record the outputs in
  docs/research/2026-11-16-xplat-vm-results.md under "Plan B live checks".
- [ ] **Step 5:** Commit `Windows sessions: hidden desktop, runner, launch, stop, cleanup`.

### Task 6: Screenshots on Windows

**Files:** Create `fsoctl/platform/win_capture.py`; Modify `fsoctl/xinput.py` (`screenshot`
asks the platform for the pixels; Linux path unchanged); Test `tests/test_platform_capture.py`,
`tests/winlive/test_capture_live.py`

**Interfaces:**
- `platform.capture.window_pixels(sess) -> tuple[int, int, bytes]` (width, height, top-down
  RGB). Windows: `on_desktop(sess.desktop, ...)` → the engine's top-level window by pid
  (`EnumWindows` + `GetWindowThreadProcessId`) → `PrintWindow(hwnd, dc,
  PW_RENDERFULLCONTENT)` → `GetDIBits` (probe 7's `capture`, moved).
- `png_bytes(width: int, height: int, rgb: bytes) -> bytes`: a minimal PNG (signature, IHDR
  8-bit RGB, one zlib IDAT with filter byte 0 per row, IEND; `zlib.crc32` per chunk).
- `crop(...)` and `scale(...)` on the pixel buffer in Python (scale = nearest neighbour by
  row/column stride), so `screenshot --crop/--scale` behave as on Linux without ImageMagick.

- [ ] **Step 1: Failing tests:** `png_bytes(2, 1, b"\xff\x00\x00\x00\xff\x00")` starts with
  `b"\x89PNG\r\n\x1a\n"`, its IHDR says 2×1, 8-bit, colour type 2, and decoding the IDAT with
  `zlib.decompress` gives `b"\x00\xff\x00\x00\x00\xff\x00"`; `crop` of a 4×4 buffer to
  `2x2+1+1` returns the middle four pixels; `scale(0.5)` of 4×4 returns 2×2 taking pixels
  (0,0),(2,0),(0,2),(2,2); a session with no window raises EXIT_NOT_FOUND with the Linux text.
- [ ] **Step 2 (winlive):** a child window filled with one colour on a hidden desktop →
  `window_pixels` centre equals that colour.
- [ ] **Step 3:** implement. **Step 4:** VM: `fsoctl screenshot <sid>` of the hidden session
  from Task 5 → a PNG showing the mission (copy it to the results folder and look at it).
- [ ] **Step 5:** Commit `Screenshots on Windows without ImageMagick`.

### Task 7: Ports, renderer and status on Windows

**Files:** Modify `fsoctl/launcher.py` (`engine_holds`, `_check_game_port`, `port_free`);
Test `tests/test_host_ports.py` (new Windows cases), `tests/winlive/test_ports_live.py`

**Interfaces:** `engine_holds(sess, proto, port) -> bool` = `sess.engine_pid in
procs.socket_owner_pids(proto, port)`. `port_free` on Windows binds WITHOUT `SO_REUSEADDR`
and with `SO_EXCLUSIVEADDRUSE` (on Windows `SO_REUSEADDR` lets a second socket take a port
that is in use, so the Linux test bind would call a taken port free).

- [ ] **Step 1: Failing tests:** `port_free` sets `SO_EXCLUSIVEADDRUSE` and not `SO_REUSEADDR`
  when `platform.NAME == "windows"` (socket stubbed); Linux options unchanged.
- [ ] **Step 2 (winlive):** with a UDP socket bound to a port by another process, `port_free`
  is False and `socket_owner_pids("udp", port)` holds that process's pid.
- [ ] **Step 3:** implement. **Step 4:** VM: `status <sid>` of a running session shows
  `renderer` "opengl (software, Mesa)" and its game port; `launch` when the asked `--port` is
  taken fails with the Linux message.
- [ ] **Step 5:** Commit `Ports and renderer facts on Windows`.

### Task 8: Crash reports on Windows

**Files:** Create `fsoctl/platform/win_crash.py`; Modify `fsoctl/crash.py` (`report` asks the
platform for the dump and backtrace; Linux path unchanged); Test `tests/test_platform_crash.py`

**Interfaces:** `find_dump(sess) -> dict | None`: the newest `<exe stem>_YYYYMMDD_HHMMSS.mdmp`
beside `sess.binary` whose stamp (UTC) is not before the session's start; returns `{"path",
"size", "time"}`. `backtrace(dump) -> list[str] | None`: None when `cdb.exe` is not on PATH
(the report then says `"backtrace": null, "hint": "install the Debugging Tools for Windows
(cdb) to get a backtrace"`); else the output lines of `cdb -z <dump> -c "kb 30; q"`, bounded
by `_GDB_TIMEOUT` and `_BT_MAX_LINES`.

- [ ] **Step 1: Failing tests:** a dump stamped before the session start is ignored; the newest
  matching one wins; another executable's dump in the same folder is ignored; no cdb → the
  hint above; the report keeps the Linux report's keys (`exit_info`, `log_tail`, `hints`).
- [ ] **Step 2:** run → FAIL. **Step 3:** implement.
- [ ] **Step 4:** VM: launch, then `lua` that the guide lists as fatal (probe-results section
  19) → `crash_report` names a fresh `.mdmp` and the engine's exit code. Record it.
- [ ] **Step 5:** Commit `crash_report on Windows: the engine's own minidump`.

### Task 9: `build` on Windows

**Files:** Modify `fsoctl/build.py`; Test `tests/test_build.py` (new cases)

**Interfaces:** `build(...)` gains `config: str | None = None` → `cmake --build <dir> --config
<config>` (Visual Studio generators hold every configuration in one build folder; Linux
single-config builds ignore it). The scope and lock come from `platform.scope` and
`platform.locks`; `_result` treats exit code 1 with "LNK"/"error C" lines as compiler errors
and reports no OOM hint on Windows (no SIGKILL there).

- [ ] **Step 1: Failing tests:** `config="Release"` adds `--config Release`; an MSVC error line
  `src\a.cpp(12,5): error C2065: 'x': undeclared identifier` is parsed into file, line, column
  and message; a linker line `LINK : fatal error LNK1104: cannot open file 'foo.lib'` is
  reported as a tool error.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement.
- [ ] **Step 4:** VM (it has the MSVC Build Tools and a configured tree under C:\FS2\...):
  `fsoctl build <worktree> --config Release --targets Freespace2` → `ok: true`; with a syntax
  error planted in a scratch copy → `ok: false` and the error row.
- [ ] **Step 5:** Commit `build on Windows: --config and MSVC diagnostics`.

### Task 10: MCP server, install scripts, CLI interruption

**Files:** Modify `mcp/server.py` (`_exec`), `fsoctl/cli.py` (signal setup);
Create `mcp/setup.ps1`, `mcp/register.ps1`; Test `tests/test_mcp_wrapper.py` (new cases),
`tests/test_cli.py`

**Interfaces:** On Windows `_exec` starts `fsoctl` with `CREATE_NEW_PROCESS_GROUP` and, at its
timeout, sends `signal.CTRL_BREAK_EVENT`, waits the same grace, then `kill()`
(`Popen.terminate()` is a hard kill there and would skip fsoctl's cleanup). `cli.main`
installs its handler for `signal.SIGBREAK` on Windows, `SIGTERM` elsewhere. The PowerShell
scripts do what the shell scripts do, with `.venv\Scripts\python.exe`.

- [ ] **Step 1: Failing tests:** `_exec` on Windows passes `creationflags` with
  `CREATE_NEW_PROCESS_GROUP` and sends CTRL_BREAK before kill (Popen stubbed); `cli` installs
  SIGBREAK when `platform.NAME == "windows"`.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement; `Invoke-ScriptAnalyzer` clean if present.
- [ ] **Step 4:** VM: `mcp\setup.ps1` then `mcp\register.ps1 -WhatIf` print the registration
  line; `python mcp\server.py` answers an MCP `tools/list` (tests/mcp_check.py).
- [ ] **Step 5:** Commit `MCP server and install scripts on Windows`.

### Task 11: CI, documentation, close-out

**Files:** Modify `.github/workflows/tests.yml` (a `windows-unit` job that must pass: `pytest
-q` and `pytest -q -m winlive tests/winlive`; macOS stays a census), `README.md` (Windows
install, Limits for Windows), `docs/AGENT-GUIDE.md`, `CLAUDE.md` (load-bearing decisions:
Job Object, hidden desktop, stop file, no sandbox on Windows), `pytest.ini` (`winlive` marker),
the spec's Outcomes section, this plan's Status and Deviations.

- [ ] **Step 1:** Push `main` to the `ci` remote; `windows-unit` green, `linux-unit` green.
- [ ] **Step 2:** In the VM run the engine tier's platform-neutral files:
  `pytest -m engine -p no:xdist tests\test_launch_engine.py tests\test_game_engine.py
  tests\test_control_engine.py tests\test_record_engine.py`; record pass/fail per test and the
  reason for each skip in the results note.
- [ ] **Step 3:** README Limits (Windows), one line each with its reason: no editor sessions
  yet, no input verbs yet, no `--sound`, no matches, no private network, no sandbox, crash
  backtraces need cdb, hidden sessions verified with software rendering only (see below).
- [ ] **Step 4:** QA Review (HIGH-RISK tier: long-lived processes, stop/cleanup behaviour;
  state-lifetime-auditor carries the premise-challenger brief), mutation pass on the touched
  `fsoctl/` files, cross-family pass, receipt.
- [ ] **Step 5:** Commit `Plan B close-out: Windows CI gate, docs`.

## Unmeasured: a real graphics card on a hidden desktop

Every hidden-desktop result so far used Mesa's software renderer (CI and the VM have no
graphics card). Whether a vendor OpenGL or Vulkan driver renders into a window on a
non-interactive desktop is not known. If it does not, the spec's fallbacks apply in order:
windows placed off-screen, then an optional virtual-monitor driver. The check is one command
on a Windows machine with a graphics card:
`python probes\xplat\hidden_run.py --log out\h.log -- python probes\xplat\engine_live.py --out
out --label hidden --fso <FastDebug exe> --game-dir <FS2 folder>`, then read `renderer` and
`capture.distinct_colours` in `out\probe-engine-live-hidden.json`. Task 5 runs it first when a
machine is available; until then README Limits carries the line in Task 11 Step 3.

## Deviations from the plan as written

Task 4 (behavioral-change, found by measurement before building): the plan said "a job exists
while a process is in it", so a later command could open it by name. Probe 10 showed the NAME
goes when the launching command's handle closes. The session's first process now inherits the
job handle (and the runner passes it to the engine). Effect: none on the design (still one
named job per session, stopped by name); the one new limit is that a job whose runner and
engine are both gone can no longer be found by name even if some stray child is left.

Task 4 (other): `scope.start` keeps its five parameters on Linux. The desktop reaches the
Windows `start` as a "Desktop=" entry in the property list instead of a sixth parameter (the
project's lint limit is five; and the Linux function is then not touched at all).

Task 4 (behavioral-change, found by the first on-Windows test): the session's first process is
started with an explicit list of the two handles it may inherit. With plain inheritance it also
got the launching command's output pipe, and a caller reading `fsoctl launch`'s output (the MCP
server does) would have waited until the session ended.

Task 4 Step 2 (other): a job's memory limit makes allocations fail; it does not end the
process as the Linux scope does. The test asserts what Windows does. README Limits will say so.

Task 5 (behavioral-change): the desktop is made by `win_scope.start` (from a "Desktop=<name>"
property) and the first process inherits a handle to it, as it does the job handle; the plan
had the launcher create it and close its handle after the start. Effect: the desktop lives
exactly as long as the runner or the engine does.

Task 5 (behavioral-change, found live): Windows sessions get `[Game] UnfocusedPause=false`
written into their prefs before the engine starts. The plan's "nothing else changed" for the
engine's environment still holds; this is a settings file, and without it every mission
paused one second in (engine source and session ids in probe-results section 23).

Task 5 (behavioral-change, found live): the bridge's file replace falls back to
delete-then-rename when a rename leaves the temp file behind (Windows), and the heartbeat
reader tries again a few times there. Linux takes neither path.

Task 5 (other): cleanup ends a session that is still "starting" after two minutes only where
the OS has no time limit on a process group (`platform.GROUP_TIME_LIMIT` False: Windows).
Linux cleanup is unchanged. `--visible` on Linux is a usage error.

Task 6 (other): a whole-display screenshot (`--full`) is refused by name on Windows; the plan
did not say. The window shot is the client area, so its size is the game's resolution.

Task 7 (behavioral-change, found live): the port test on Windows binds nothing. The plan had
it bind with SO_EXCLUSIVEADDRUSE. Any bind of a UDP port on every address makes Windows
Firewall add a rule for Python and put a question on the user's desktop (measured twice,
probe-results section 23), so `port_free` there asks the socket table instead, in both
address families. Effect: no firewall question from the harness itself. Cost: a port Windows
would refuse for a reason the table does not show is found only after the engine starts,
when the existing game-port check warns (or fails a host). Linux is unchanged.

Task 7 (other): Step 4's `launch --port` check cannot be run on Windows in this plan (`--port`
needs a match; matches are Plan E). The named-port refusal was checked by calling
`launcher.host_port(7808)` in the VM while another program held the port.

Task 8 (behavioral-change, measured): a dump is the session's only if the process id
recorded inside it is the session engine's. The plan matched by name and time; with two
sessions of one build running, the later crash would be reported for both. The result gains
a "pid" field (None when the file's process id cannot be read; name and time then decide).

Task 8 (behavioral-change, measured): the debugger command is `.ecxr; k 0n30; q`, not the
plan's `kb 30; q`, which printed three frames of the dump writer on the real dump. The
backtrace is the frame rows only. cdb is looked for on PATH and then in the Windows SDK's
own folder, because the SDK's installer does not put it on PATH. cdb runs with no symbol
download and from an empty folder, as gdb does on Linux.

Task 8 (other): the "install the Debugging Tools" text is a line in `hints`, the Linux
report's key, not a separate `hint` key; the report also gains `dump` and keeps `signal`
and `core` as None. Hints name the Windows exit code (0xE06D7363 and four others).

Task 9 (behavioral-change): on Windows the build runs through a new `win_scope.run`, a
wait-for-it form of `start`. Its job ends everything in it when `fsoctl build` ends or is
killed, and only `fsoctl` holds a handle to it. The plan said only "the scope comes from
`platform.scope`". Why: MSBuild leaves worker processes behind for minutes (14 plus
`mspdbsrv` were seen), and in a job that outlived the build they would make the next build
of the tree "already running". `run` exists on Windows only; the parity test lists it with
the reason. The Linux build command is unchanged.

Task 9 (behavioral-change, found live): when `cmake` is not on PATH, `build` runs the cmake
that configured the build folder (`CMAKE_COMMAND` in its `CMakeCache.txt`). Visual Studio's
Build Tools do not put theirs on PATH. This applies on Linux too, where it changes only a
case that used to fail.

Task 9 (other): `build()`'s sixth option, `config`, is keyword-only and carries the
project's one argument-count lint exemption (recorded in the bypass ledger). Position-less
errors of MSBuild and the compiler driver are reported as well as the linker's. Step 4's
error was planted in the VM's own tree and reverted, not in a scratch copy.

Task 10 (behavioral-change, measured): the plan had the MCP server always start `fsoctl` in a
new process group and send Ctrl-Break at a timeout. Measured: Ctrl-Break arrives only through
a console the server shares with `fsoctl`, and a server with no console would make `fsoctl`
open a console window on the user's desktop. So the server shares its console when it has
one, and otherwise starts `fsoctl` with no window and ends it at once on a timeout, with no
grace wait. In that second case `fsoctl`'s cleanup does not run; sessions it started end at
their own limits, and the timeout's hint says to check `list`.

Task 10 (other): `fsoctl` picks its stop signal by what Python offers (`SIGBREAK` exists on
Windows only), not by `platform.NAME`: many tests set that name to "windows" on Linux, where
the signal does not exist. The MCP tools gained `launch(visible=...)` and
`build(config=...)`. `register.ps1`'s real registration call was not run (no Claude Code in
the VM); its dry run and its three refusals were.

Task 11 (scope-change, found by running it): the plan asked for a Windows CI job that runs
the unit tier, and assumed the tier would run there. It had never been run on Windows: 13
test files could not be imported, one test ended the interpreter, about 250 failed. Added:
`tests/conftest.py` says which tests run where (21 Linux-only files, a `linux` mark on
single tests, a `symlinks` mark), and tests with Linux-shaped assumptions that hid nothing
were fixed. Effect: Windows runs about half the tier (815 of 1571 tests), the half that
does not test a Linux facility.

Task 11 (behavioral-change, three faults the Windows run found, each fixed test-first):
text files are opened as UTF-8 by name everywhere in `fsoctl` (lint rule on); the Lua
pre-check asks the OS what an absolute path is, so files loaded by a Windows path are
checked; the shadow folder's links say when their target is a folder, and an account that
may not create links is refused with the way to allow it.

Task 11 (behavioral-change, every OS): `compare` and `renderer_compare` refuse before
starting a session when ImageMagick is missing; they used to run both sessions and then
fail.

Task 11 (other): Step 2 ran ten engine-tier files in the VM, not the four named. Two
sandbox tests from before this plan were corrected so the Linux CI job can pass on GitHub's
runner (display 0 is the session's own there; the tests now also require `keyctl` and
`prlimit`), and CI installs `bubblewrap` and `keyutils` by name. The sandbox is unchanged.

Task 2 (other): `renderer.classify` keeps its three words (vulkan / opengl / unknown) on
Windows instead of the plan's "opengl (hardware)" and "opengl (software, Mesa)", because
callers compare the word with the renderer that was asked for; the driver shows in
"libraries". Task 7 Step 4's expected status text changes with it.

Tasks 1-3 (other): smaller rulings are in the ledger (a third lock function for the blocking
case; the "is this pid our runner" check lives in the launcher; win_safefs repeats safefs's
small helpers because the Linux module cannot be imported on Windows).

Outside the plan: one existing Linux test (tests/test_sandbox_mutation3e.py) failed one run in
six because its own setup was random; fixed in its own commit (006f546), assertion unchanged.

### Review fix wave (Task 11 Step 4, 2026-11-17; commits 24bbe7c, 496f197 and the close-out)

What the review changed in what the plan had built. Each was written test-first; the review
record is `fso-harness-artifacts/qa-plan-b/synthesis.md`, in the artifacts folder beside
this repo.

Behavioral changes on Windows (the plan's own code):
- Task 5 (behavioral-change): a runner that fails after starting the engine ends the engine
  and records `ended_by: "runner_error"`; before, it marked the session failed and left the
  engine running. Only a failed START is a failed session.
- Task 5 (scope-change, small): the cleanup step the plan gave for abandoned starts also
  ends a session whose recorded runner is known to be gone while its group runs
  (`runner_lost`), records what it did in the session record and the audit log, and carries
  on past a group it cannot end. Effect: on Windows an engine no longer outlives a lost
  runner past the next launch or cleanup. Why it is inside the plan: the spec asks for
  parity, Linux ends such a run by the scope's own time limit, and a Windows job has none.
- Task 5 (behavioral-change): one stop file. The runner watches the marker `stop` has
  always written; the second file named `stop` is gone. The runner looks for the marker
  before starting the engine, so a launch cancelled early starts none.
- Task 4 (behavioral-change): only "no job of that name" means a job is gone. A job that
  cannot be opened or counted is treated as there: `is_active` says True, `stop` ends it
  when it can and raises when it cannot. A session whose recorded runner still runs is not
  gone whatever the job lookup says (`launcher._gone`): from another Windows login a live
  session used to read as gone, and cleanup would have deleted its record.
- Tasks 1-5 (behavioral-change): session records are rewritten and read with a bounded
  retry on Windows (measured: without it a third of renames and a tenth of reads failed
  under two looping readers).
- Task 3 (behavioral-change, security): `win_safefs.write_atomic` uses a random temp name.
  Measured: an exclusive create on Windows follows a planted link to a file that does not
  exist.
- Task 7 (behavioral-change): `port_free` raises when the socket listing cannot be had;
  programs are found on PATH without the current folder and started by full path.
- Task 6 (behavioral-change): a screenshot is taken only of a process in the session's own
  group; a window larger than an 8K screen is refused.
- Task 8 (behavioral-change): cdb runs without any of the debugger's environment
  variables; a busy channel file is "unusable, try again", not "tampered", on Windows.
- Task 3 (other): `win_safefs.usage` and `clear_tree` removed (nothing on Windows can call
  them); the parity test has a LINUX_ONLY table.
- Task 5 (behavioral-change, from Review Focus 3): `launch` on Windows refuses a runtime
  root whose path has a letter outside ASCII and names `FSO_HARNESS_RUNTIME` as the way
  out. Probe 12 showed the engine never comes up under such a folder (its own source says
  it does not support one); a space and an apostrophe work. Effect: a Windows user whose
  name has such a letter gets a clear refusal on the first launch instead of a three-minute
  wait and an empty log. Making such a folder WORK is DEF-XB-23.
- Task 5 (behavioral-change): a runner counts as alive only if its process has not ended
  (an ended one can still be looked up for a moment on Windows; seen in the VM as a wrong
  note on an ordinary stop).
- Every OS (behavioral-change, small): when `stop` cannot write its last record, only a
  record that is gone is reported as "its run folder was removed"; any other failure is
  named.

Fourth wave, from the second review round and the two outside reviewers (models of two other families), who
read the three fix commits above. Both outside reviewers found the first item on their own:
- Task 5 (behavioral-change): what "gone" means on Windows was still too loose. From another
  Windows login, a session whose runner could not be asked about, or whose engine had
  outlived its runner, read as gone, and a cleanup there would delete its record. Now a
  record with no end is gone only when its runner AND its engine are KNOWN gone; a record
  that has its end is gone unless one of them is known alive. The runner records the
  engine's start time for this (a fourth new record key, `engine_start`). `stop` from
  another login refuses when only the engine is left, and no longer writes "stopped" before
  the runner has acted (the reply says "asked" and keeps the record's status).
- Task 5 (behavioral-change): the runner's failure path can no longer be stopped by its own
  log line (a full disk made that write fail before the engine was ended).
- Task 5 (behavioral-change): after a command was sent, a busy channel file never ends in
  "try the command again": the reply read rides it out, a reply that cannot be deleted still
  gives its result, and a command file in use at the timeout is "taken, outcome unknown".
- Task 5 (behavioral-change): the run-folder refusal now comes before the runtime root is
  made; its hint asks for a folder only the user can write and for `register.ps1` to be run
  again, which now stores `FSO_HARNESS_RUNTIME`.
- Task 6 (behavioral-change): work on the user's own desktop (a `--visible` screenshot) has
  the same 60-second limit as work on a session's desktop.
- Task 8 (behavioral-change): a dump stamped after the session's recorded end is not taken
  for its own (process ids are reused); a crash report asked from another login says the
  session still runs there.
- Cleanup's answer lists a session it could not end (the MCP server drops stderr).
- Every OS (behavioral-change): `stop` goes on to stop the process group when it cannot
  write its marker, and says so; a failed group stop says that the request was left.
- Smaller: a PATH entry must have a drive or a share in front on Windows, whatever the
  Python version; the Windows file guard removes a temp file whose move failed; `ask_stop`
  does not write a marker that is already there.

Fifth wave, from the tests seat and the state seat, who read the fourth (their first run
was cut off by a usage limit; the owner lifted it and they ran again on Mythos):
- Task 5 (behavioral-change): "runs in another Windows login" was also said in the
  session's OWN login, for the half second a runner outlives an engine that exited or
  crashed, while a session starts, and while a runner is stuck; a crash report then
  returned no report. It is now said only when the group is not found here.
- Task 5 (behavioral-change): `stop` asks whether it sees the group before it stops
  anything, so a process in its last moment after a forced end cannot turn the reply into
  "asked" or a refusal. From another login, a `stop` whose marker could not be written
  refuses (nothing was left and nothing was stopped).
- Task 5 (behavioral-change): a cleanup that ends a session someone had asked to stop
  records a stop, not "failed, runner lost".
- Task 5 (behavioral-change): after a command was sent, the two remaining deletes (a
  withdrawn command, a command nobody will take) no longer turn a certain answer into
  "unusable" when the file is busy.
- Every OS (behavioral-change, small): a mod name made only of dots and spaces is refused;
  the "a stop request was left" hint is given on Windows only (the Linux runner acts on a
  signal).

Closing commit, after the last pass (CI went red on the fifth commit; not in the plan):
- Task 5 (behavioral-change): on Windows the cleanup's removal of a run folder is tried
  again for a short, bounded time (`session._while_busy`, at most about a second). Why: on
  CI's Windows machine the cleanup that had just ended a session's processes got "in use"
  for its `runner.log`, three runs of three; measured in the third (run 44083251849), the
  job was empty, the engine's process not yet ended, the record still there, and a cleanup
  16 ms later removed the folder. The VM never showed it. The fifth wave's fixture change
  (the live test's log inside the run folder, TAF-14) is what brought it out. Effect: the
  cleanup's answer no longer lists a failure that is over before anyone reads it; a file
  that stays in use costs that cleanup about a second and is then reported as before.
  This change was made after the seats' last pass; the code review and state seats read
  it afterwards (synthesis, "After the last pass"): no blocker or major, five notes. One
  was acted on at once: the code comment said the next cleanup would have retried in any
  case, which holds for the measured run only.
- Tests only: the test that the record is not asked where groups are seen from every
  login now also holds the tools and `stop` to it (STFB-2); it fails with the flag dropped
  from either line.
- Documents: the last pass's ten notes are in DEF-XB-20 and DEF-XB-24, each with its place
  to revisit.

Linux behaviour that changes (each small; none was in the plan):
- the runner's command line gains `-P` (the caller's folder is no longer on its module
  search path; a folder named fsoctl or a json.py there was loaded by the host-side runner);
- a mod name with a backslash or a colon is refused; a VP entry with either is refused;
- the Lua pre-check lists a load from a path starting with two slashes as unchecked instead
  of reading it;
- `list_sessions` no longer raises when a record vanishes between the look and the read;
- found by the review as unlisted, from the plan's own tasks: `status` and `session.json`
  carry four new keys (`runner_start`, `engine_start`, `desktop`, `visible`); Microsoft diagnostics are
  parsed in Linux build logs too; the Lua pre-check reads a doubled backslash in a loaded
  path as one; the bridge's delete-then-rename fallback runs on any system when the first
  rename leaves its temp file behind; `--config` is passed to cmake on Linux; `_build_lock`
  no longer chains the lock error.

Documentation: README Limits (Windows) gained eight entries and lost the advice to use an
administrator prompt; the project notes and the agent guide carry the same facts.

## Appendix: Deferments originated in Plan B

Each was found while executing this plan (2026-11-16/17, commits 59372d3 to the close-out)
and knowingly left. "Signal" is what shows the item is still open.

| ID | Sev | What | Found by | Defer target | Why left | Fix direction | Signal | Obsolete when |
|---|---|---|---|---|---|---|---|---|
| DEF-XB-04 | MEDIUM | The Lua pre-check reads only `loadfile("...")`/`dofile('...')` with a quoted path. A long-bracket string, a call without parentheses and a path in a variable are neither read nor listed as unchecked, so "passed" overstates what was looked at (every OS; older than this plan) | a live check on Windows that happened to use the long-bracket form (session sf4785860 died) | the next change to `fsoctl/lua_check.py` | not a Windows matter; the pre-check's exemptions change only with a red test and its own review | list every load whose argument is not a quoted absolute path as unchecked; then read long-bracket strings | `test_known_hole_a_load_not_written_as_a_quoted_path_is_neither_read_nor_listed` passes | that test is replaced by one that asserts the load is listed |
| DEF-XB-05 | MEDIUM | On Windows `api_docs`, `frame_stats` and `replay` need the right to create symbolic links (Developer Mode or administrator); without it they refuse | first Windows run of the unit tier (error 1314) | Plan C | the refusal is clear and one Windows setting lifts it; the alternative needs its own design and a live check | folder junctions (no right needed) plus hard links for `.vp` files on the same volume, copies otherwise | the refusal text "does not let this account create links" | a Windows release makes link creation unprivileged by default |
| DEF-XB-06 | LOW | Nine verbs were never run on Windows: `answer_popup`, `compare`, `renderer_compare`, `journal`, `post_event`, `replay`, `scratch_mod`, `telemetry`, `vp` | Task 11 Step 2 | Plan C's first engine-tier pass | none is refused; each needs a session scenario or a tool the VM lacks (ImageMagick) | run `tests/test_compare_engine.py` with ImageMagick installed and one scenario per remaining verb | README "Not run on Windows yet" | that README paragraph is gone |
| DEF-XB-07 | LOW | Windows runs about half the unit tier: 21 files are Linux-only as written and 71 single tests carry the `linux` mark, some of them for one Linux-shaped line in an otherwise neutral test. The review (TA-4, TA-6, TA-13) moved the lock and platform basics to a file Windows collects and added the every-system halves of the mission-name, multiplayer-value and port-ownership tests; still behind a mark: the audit copy that outlives the run folder, the CLI's stop handler (the Windows unit tier has no test of it; one live test covers a single Ctrl-Break), a created pilot reaching the main menu, and the neutral launcher rules inside test_b_launcher.py | Task 11 | whenever a marked file is next edited | a finer split is test rework with no product change | split each marked test into its neutral part and its Linux part | `grep -c "pytest.mark.linux" tests/*.py` | the count falls to tests of Linux-only features |
| DEF-XB-08 | MEDIUM | When the MCP server has no console at all, a timed-out `fsoctl` is ended at once and its cleanup does not run; sessions it started end at their own limits | the seven-case Ctrl-Break probe (Task 10) | when a console-less MCP host is seen in use | Claude Code in a terminal has a console, and a windowless console also works; the case may not occur | a stop request that needs no console, e.g. a stop file the CLI watches | the timeout's hint text | such a channel exists, or Windows gains a signal one process can send another |
| DEF-XB-09 | LOW | What the engine and the compiler do when a job's memory limit makes their allocations fail is not measured; Windows builds carry no out-of-memory hint | Task 4 and Task 9 | Plan C | needs a run that fills memory on purpose | a small VM run with a low limit; then a hint keyed to what is printed | `_result`'s docstring says "not measured" | measured and hinted |
| DEF-XB-10 | LOW | `register.ps1`'s real `claude mcp add` was not run, and which console situation Claude Code starts its MCP servers in on Windows is not known | Task 10 | the first Windows machine with Claude Code | the VM has none | run it; note the result in README | README "Not run" line | run and recorded |
| DEF-XB-11 | LOW | `api_docs` keeps its cache under `~/.cache/fso-harness` on Windows, the Linux default, not under `%LOCALAPPDATA%` | the live `api_docs` run (Task 11) | Plan C | it works; moving it changes where existing caches are found | `platform.dirs` gains a cache folder | the path in `api_docs`' reply | moved |

| DEF-XB-12 | MEDIUM | PROPOSAL, needs the owner's ruling: let the RUNNER hold the last handle of a session's job, with kill-on-close, and start the engine without handle inheritance. Everything in the job would then end the moment the runner ends for any reason, with no cleanup rule and no README limit. It reverses a load-bearing decision ("never set KILL_ON_JOB_CLOSE on a session's job") and is unmeasured | security seat, Phase B of the review (496f197) | owner, then Plan C | a redesign of the containment path at the end of a review; the smaller rule (cleanup ends a group whose runner is known gone) closes the common case now | probe with probes/xplat/hidden_run.py's pieces: runner killed -> engine gone; `status`/`stop` from another process while the runner lives; the desktop outliving the launcher | every `ended_by: "runner_lost"` in a record or audit log is a case this design would have handled at once | built, or ruled out |
| DEF-XB-13 | LOW | A launch ended between creating the first process and putting it in the job leaves a suspended process outside any group, holding the log, the job handle and the desktop | state seat ST-7 | Plan C | a window of microseconds; the fix (the job named in the process's attribute list, Windows 10+) changes the start path that all live tests rest on | add PROC_THREAD_ATTRIBUTE_JOB_LIST to the attribute list already built | the same folder in "automatic cleanup could not remove" at every launch | the process is born in its job |
| DEF-XB-14 | LOW | `fsoctl build` ends every process in its job when it ends, by design; MSBuild reuses idle worker nodes between builds and compilers share a symbol server, so a build started outside the harness at that moment might lose a tool (inferred, not seen) | state seat ST-11 | Plan C | needs two builds at once to show; no report | set MSBUILDDISABLENODEREUSE=1 for the build, or measure first | a build outside the harness failing just as an `fsoctl build` ends | measured harmless, or the variable set |
| DEF-XB-15 | LOW | The MCP server's checks run in CI on neither system (no job makes its venv), and tests/mcp_check.py has no branch for a server with no console | test seat TA-14 | the next CI change | verified where setup was run (the VM, this machine); a CI venv step needs a network install on both runners | a CI step that makes the venv with the pinned package; branch the marker assertion on `_has_console()` | "skipped" for test_mcp_wrapper in the CI log | the checks run in CI |
| DEF-XB-16 | LOW | Test data against real Windows: the crash tests build their own minidump (a real dump's first bytes would pin the layout); netstat's TCP rows are matched by the English word LISTENING (TCP is unused on Windows until matches come); the live capture test fills the window with one colour, so row order is not pinned | test seat TA-18, code review CR-5 | Plan C (dump, capture), Plan E (netstat) | none affects a Plan B session | a data file cut from a real dump; match TCP rows by shape; a two-colour window | the three places named | each done |
| DEF-XB-17 | LOW | The Windows engine gets the whole environment of the command that started `fsoctl` | security seat SA-3 (its optional part) | Plan C | it stops no hostile build (nothing confines a Windows session); it would keep tokens out of an honest engine's logs and dumps | a reduced environment, as the Linux sandbox builds one | README Limits: "the whole environment ... in reach" | built |
| DEF-XB-18 | MEDIUM | What a session started from an administrator prompt looks like from an ordinary one is not measured (the code treats any failed job open but "not found" as "may be there"); and a session of another Windows login is named only by a note on `stop`, since the record does not hold which login made it | state seat ST-4, test seat TA-12, review Phase B | Plan C | needs an elevated session on the logged-in desktop; README now says not to use an administrator prompt | one VM run (launch elevated; status, stop, cleanup ordinary) and record the error codes; store the Windows login session in the record and refuse by name from another | README Limits: "not measured"; `list` showing a live session as not alive | measured, and the record names its login |
| DEF-XB-19 | MEDIUM | Which renderer a Windows session reports rests, for the vendors' OpenGL drivers and Vulkan, on library names never seen in a run; `launch --vulkan` passes or fails on them | scope audit AR-2, test seat TA-17 | with the graphics-card run below | cannot be measured without the hardware | after the run: keep, correct, or refuse `--vulkan` on Windows by name until measured | renderer.py's docstring and the comment over the classify test | measured on a machine with a graphics card |
| DEF-XB-20 | LOW | Smaller items left as they are: `lock_wait` waits in whole seconds on Windows (msvcrt's waiting mode); a record written by this branch has three keys an older checkout cannot load (the record class rejects unknown keys, older than this plan); build logs under the runtime root are never removed on Windows; the runner's working folder is the caller's (nothing is loaded from it since `-P`, but Windows keeps the folder in use); callers still read the OS name in four modules (the package docstring lists them for Plan D); five small pieces of code the goal does not need (CR-14 b to f: a test-only parameter in `lua_check._loaded_paths`, a `hasattr` in `_check_role` that Windows cannot reach yet, an unreachable `raise` in the heartbeat reader, the one-line `win_runner._ask_close`, one hint sentence held twice in crash.py); the desktop name is written to the record in `_prepare`, outside the launch's own failure handling (a failure there leaves a "starting" record that the next cleanup removes); duplication the fix waves added (round-2 CRF-6: the refusable launch fields are listed twice, the "full path on Windows, bare name on Linux" choice twice, the socket listing's command twice, the "plain name" rule three times, and `linux_procs.pid_ended` has no caller on Linux); a folder with a session's name and no record costs 40 ms per listing on Windows (round-2 CRF-8), and such folders only grow: a removal that fails halfway has deleted the record first and no later cleanup comes back to the folder (STF-7; sweep them past the starting grace, as sandbox orphans are swept). CI showed the cause is real: a file of a just-ended process is "in use" for an instant (run 44083251849; there the record survived only because the busy file was listed before it). The cleanup's removal now waits that out, which makes a half-removed folder rarer, not impossible; probe 11 could count the reads that needed a second look, the direct evidence for that half of the record fix (STF-8); `win_runner._say` is held by tests at two of its six call sites (TAF-12), and two of the other four sit before a kill, where a plain write put back would leave an engine running on a full disk; three ends write no end time (cleanup's mark, `stop`'s, and the runner when the engine outlived both waits), so the "not after the end" bound on a crash dump does not apply to those records (STFB-4: write an end time wherever a status is ended); small test gaps from the last pass: the joined form of the group-stop hint is unasserted, `test_find_skips_an_entry_with_no_drive_on_windows` can fail only on Python 3.12, the two `channel._drop` tests do not assert that the refusal happened, and MUF-10's equivalent in `_cleanup_locked` holds only while `_end_unattended` is called first; `_stop_group` reads the OS name for what is a property (the runner is asked through the marker); the cleanup's bounded retry costs about a second per folder whose file stays in use, at every cleanup, and every launch runs one under the launch lock (CRFC-1: one time budget for the whole cleanup would bound it; by the state seat's count it takes about sixty such folders to fail one waiting launch); the retry does not cover a holder that permits deletion, where the file stays pending and the final folder removal can fail "not empty" (STFC-1, inferred; check: hold a file open with delete sharing in a second process and call `remove_tree`); removing `session.json` LAST would make "the next cleanup retries" true in every case by the code's own order and stop STF-7's folders being made, a better fix than a sweep (STFC-1); `scratch_mod remove` and `_cleanup_side_folders` still delete in one try (closing read; the second is kept clear of a just-ended session by its one-hour floor) | code review CR-13, CR-14, CR-21; state seat ST-14; security seat SA-1 | whenever the code is next touched | none changes what a session does | named in each place | the comments in win_locks.py and platform/__init__.py | each done |
| DEF-XB-21 | MEDIUM | On Windows the bridge refresh is "refused while another session is running" only for sessions of the same runtime root and login: jobs cannot be listed by name, so `active_units` asks about this root's run folders. A session under another root can have its bridge replaced under it | code review CR-17, state seat ST-6 (converged) | Plan C | on one machine with the default root the guard holds; the gap needs two roots on one game folder (a test run beside real use) | a per-game-folder record of live sessions that does not depend on the runtime root (a marker file per session beside the bridge, removed at session end, checked with the recorded runner's pid and start time) | README Limits "The bridge refresh looks at one runtime root"; a session failing with a changed bridge while another root's tests run | the guard reads a per-game-folder record |
| DEF-XB-22 | MEDIUM | A real graphics card and Vulkan on a hidden desktop are not measured, though the spec says each probe's result is recorded before the code that rests on it (scope audit AR-1). Needs the owner's ruling: accept as documented, or run it on his Windows 10 boot | scope audit AR-1 | owner | no VM can show it; `--visible` is the documented way out and was run with software rendering only | `python probes\xplat\win_hidden_desktop.py` and one `fsoctl launch` + `screenshot` on a PC with a graphics card, hidden and `--visible`, OpenGL and `--vulkan`; record in the results note | README Limits "Hidden sessions were verified with software rendering only"; a black screenshot reported by a Windows user | measured |
| DEF-XB-23 | LOW | A runtime root with a letter outside ASCII is refused on Windows, not made to work; and a GAME folder or a MOD name with such a letter is neither measured nor refused (tests seat, round 2, TAF-10: probe 12 takes `--fs2path` for it); the `icacls` line README gives for a private run folder names the account as `%USERNAME%` and was run only under an account with a plain name (security seat, last pass, SAFB-2) | Review Focus 3, probe 12 (this review) | Plan C, or when the engine gains Unicode paths | the refusal is clear and one environment variable lifts it; making it work needs a measured route | try Windows short path names for the preferences folder (may be switched off per volume), or a per-user ASCII fallback root with its own permissions question; probe 12 is the test | the refusal's message; README Limits "The run folder's path must be plain ASCII" | probe 12 passes under "José" |
| DEF-XB-24 | LOW | On Windows the launcher asks about a session's recorded runner and engine even in the caller's own login, where "no job of that name" is already the whole answer (every process of a session holds a handle to its job). A record that names a long-lived process (a hostile or damaged record; or a pid reused by a process that cannot be asked about, for a record with no end) keeps that session counted against the cap and its folder kept until that process ends. Two kinds, kept apart (last pass, SAFB-3 and the code review seat): a record whose pid passed to a process that CANNOT be asked about is freed by a `stop` (it writes "stopped", and a record with its end is gone unless a process of its is known alive); a record that names a process which ANSWERS as alive with the recorded start time is not: `stop` sees no group, finds the "runner" alive, answers "asked" and writes nothing. The same blindness in the other direction (state seat, round 2, STF-4): an engine whose runner is gone is NOT ended by the automatic cleanup when the runner's process number has passed to a process this account may not ask about, and a record left without its end by a restart keeps its slot until someone runs `stop` | security seat, round 2 (SAF-1); state seat (STF-4); last pass (CRFB-1, CRFB-2, TAFB-1, SAFB-1, SAFB-3, STFB-1, STFB-3) | Plan C | needs one measurement (does `ProcessIdToSessionId` answer across logins without extra rights?) and new Win32 surface; a hostile build is unconfined on Windows in any case; on Linux the record is no longer asked at all | ask the record only for a process in another login session than the caller (`ProcessIdToSessionId` of the pid against this process's); probe from the ssh login first. FIRST, because it needs no measurement across logins and no process to be opened (code review, security and state seats, last pass): where the group is visible, ask the job for its member processes; a runner not among them is gone, and in the caller's own login that also answers the forged-record case. Second (state seat): cleanup also ends a group still active past `created + runtime + a grace`, the Windows twin of the Linux time limit, which covers the pid that cannot be asked about and the stuck runner alike; `status` could carry `_runs_out_of_sight` as a field. For records from before a restart: a record made before this boot has no live process. From the last pass, all belonging here: (1) Three grades, kept apart (state seat, closing read). MEASURED on CI's Windows machine (run 44083251849): after a job ended by force is empty, its engine still reads as not ended for an instant. READ from the code, seen by nobody: in the session's own login `_runs_out_of_sight` is then true for one call and `_gone` keeps an ended record for one cleanup; both right themselves (CRFB-2, TAFB-1, STFB-1). NOT measured: the same instant after a runner's own exit, which is the tool-call path. `scope.is_gone` gives one answer for "not found here" and "found, with nothing in it". A group answer for "no such name" alone closes the instant only if the dying process still holds its job handle then (likely at the measured instant, where it still held its log; the code review seat's inference); asking the record only for another session does not depend on that. (2) A stop marker is never removed, so a cleanup after any stop attempt, a refused one included, records "stopped", the run counts as ended cleanly (kept 1 day, not 7) and `crash_report` loses its "runner lost" pointer; the audit line keeps the reason (CRFB-1, SAFB-1, STFB-3: let the audit line say both, or keep a `runner_lost` end out of the clean set). (3) A runner ended by force between starting its engine and recording it leaves a record with no engine, which reads as gone from another login while that engine runs (the tests seat's cell beside TAF-2). (4) By Microsoft's "Kernel object namespaces" page each Windows SESSION has its own namespace for job objects and services use session zero (fetched by the state seat, not re-read by the head), so "another Windows login" in the code and README means another session. That an administrator prompt on the same desktop is the same session is the state seat's inference from memory, not on that page; DEF-XB-18's run settles it. (5) No test marks the known cost that `stop` cannot free a record naming a live process, nor README's "then only `stop` ends that engine" (tests seat) | a session that `list` shows as not alive, that `cleanup --older-than 0` will not remove and whose recorded pids belong to other programs; for the engine left running, a session still alive past its runtime limit | built, or measured impossible |

Still unmeasured, not deferred (it needs hardware): a real graphics card and Vulkan on a
hidden desktop (the "Unmeasured" section above). Open with the owner, not a
deferment: `launch` keeps 8 GB for the desktop, the Linux rule, which refuses on a 16 GB PC
with less than about 10 GB free.

Earlier items: DEF-XB-01 and DEF-XB-02 named Task 5 as their target. Task 5 wrote the
product modules (`win_desktop`, `win_capture`, `win_scope`) with their own tests instead of
reusing the probe files, so the probes stay as dated records with their copies of the
helpers; DEF-XB-02's FRED2 half and DEF-XB-03 stay with Plan C.

## Deviations and deferments from the probe review (2026-11-16)

Deviation (other): the probes this plan rests on were reviewed twice after the plan was
written (fso-harness-artifacts/qa-xplat-vm-probes/synthesis.md). No task, scope line or call
above changed. What changed is the evidence wording: call 2 now cites three engine cells and
the "brought to the front" control, and the results note no longer claims two witnesses for
every FRED2 row. Effect on functionality: none; nothing is built yet.

Inherited deferments (full detail in the synthesis file):

| ID | Sev | What | Defer target | Signal | Revisit when |
|---|---|---|---|---|---|
| DEF-XB-01 (CR-7) | LOW | The probe files each carry their own polling helper, Win32 loader, capture and key constants (only `attempt` is shared) | Task 5, which turns hidden_run.py and the capture code into `fsoctl/platform` modules | `grep -c "def wait_" probes/xplat/*.py` stays above 1 | Task 5 starts, or a new probe file is added |
| DEF-XB-02 (ST-6) | MEDIUM | No test double for hidden_run.run, fred2_live.save_and_read and the row-building code (text_routes, combo_routes, mark_test_ship, answer_boxes, foreground_check); only the VM runs exercise them | Task 5 (launcher) and Plan C's first probe (FRED2 rows) | the mutation run lists them as "not checked" | Task 5 Step 1 writes the launcher's tests first |
| DEF-XB-03 (ST-7) | LOW | Whether an old-style dialog's unnumbered label reads back as 65535 (then a plain OK/Cancel dialog would be answered as a message box) was not checked on Windows | Plan C's first probe | none until checked; is_message_box's test pins today's rule | Plan C starts |
