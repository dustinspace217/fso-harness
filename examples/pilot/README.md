# A scripted fighter pilot, and a drill to measure it

`pilot.lua` flies the player ship from Lua: it aims, fires, uses missiles and countermeasures,
manages throttle and avoids collisions, using player controls only (stick and throttle through
`FLIGHT_CONTROL_LUA_FULL`, the trigger through `firePrimary()`/`fireSecondary()`, bound one-shot
controls through `useButtonControl`). `drill.lua` and `pilotdrill.fs2` are a firing range for
measuring a pilot without a mission's randomness. AGENT-GUIDE "Fly a mission" has the rules in
short; probe-results section 22 has the engine sources behind each one.

## Run the drill

1. `scratch_mod create`, copy `pilotdrill.fs2` into its `data/missions/`.
2. `launch` with `mission: "pilotdrill.fs2"`, `mods: <scratch mod>`, `pilot: "create:Drill"`,
   every `skip_*` flag, `dismiss_popups`, `allow_eval: true`; `wait_for` `GS_STATE_GAME_PLAY`.
3. `lua`: `loadfile("<abs>/drill.lua")` then `pcall` it; the same for `pilot.lua`.
4. `lua`: `DR.label = "my-change"; return DR.start_track({secs = 60, speed = 45})` (a scripted,
   invulnerable, unarmed drone flying a tilted, weaving circle), or
   `DR.start_duel({n = 4, cls = "SF Dragon", limit = 90})` (one AI fighter at a time, spawned
   1200 m out ahead / right / behind / above in turn; the player is healed, rearmed and recentred
   between duels).
5. Read `return DR.report()`: one line per drill (and `DR.error`, set if a drill ended on an
   error such as a ship class that could not be created). Track lines give the share of in-range frames
   with the nose within 1/2/3 degrees of a straight-line lead (a yardstick, not the pilot's own
   aim, which leads for acceleration on purpose) and primary hits/shots. Duel lines give the
   outcome (kill / died / loss under 15% hull / timeout), seconds, damage taken (shields + hull),
   and primary and secondary hits/shots.

Hits are the ground truth: the scoring counters include hits on an invulnerable ship. The
degree bands are not a proxy for hits: they ranked v2-without-acceleration-lead above v2 while
hits ranked them the other way round. The fire gate also sets how many shots are fired, so a
hit percentage compares aim AND trigger discipline. Record the frame rate with each run
(`A1.fq.frames` over mission time): the filters are time-based, but countermeasure checks are
counted in frames.

## Use the pilot in a mission

Set the escort list before loading, or leave it out to fight the nearest hostile fighter:
`A1 = A1 or {}; A1.cfg = { guard = { "Iota 1", "Iota 2" }, guard_away = "Psamtik" }`. Set fields
on `A1.cfg` rather than replacing `A1`: it holds the flags that stop a reload adding a second hook.
Switch it off with `A1.enabled = false`. `A1.log` holds notable moments, `A1.stats` counts bolts fired.

Mission modes (all optional `A1.cfg` fields; the drill has a matching exercise for each):

- **Escort** (`guard`): threats near the guarded ships come first, bombers score
  `bomber_bonus` (2000 m) closer than they are, and nothing farther than `leash` (2500 m) from
  the guarded ships is chased. With `intercept` (default on) a bomb homing on a guarded ship
  is shot down before anything else. `intercept = false, bomber_bonus = 0, leash = 1e9` is the
  plain dogfighter, for comparison. Any small ship of another team whose target is a guarded
  ship counts as an enemy, whatever its team's standing (turncoats stay Neutral). Options:
  - `aimed_bonus` (1500 m): an enemy targeting a guarded ship counts that much closer.
  - `guard_weakest = true`: with nothing to fight, hold station by the guarded ship with the
    least hull instead of the first on the list.
  - `spare = { wing, count, release, disable }`: once `count` ships of that wing have appeared
    and one is left, target it and order all fighters to disable it (`disable = true`; repeated
    every 3 s) or ignore it, and never shoot it, until a ship named `release ...` appears. For
    missions whose next wave is cued on a wave's destruction.
  - `suspects = {names}`, `screen_ahead` (m), `ab_chase` (m), `missile_block = true`: tried in
    The Hammer and the Anvil and found no help (table below); kept for comparison.
- **Strike** (`strike = "<ship>"`): fly at the named ship, fire the selected secondary
  every 0.25 s from 500 to 2000 m with the nose within 6 degrees (the engine refuses an
  unlocked bomb, ship.cpp:14265, so the refusals are the lock check), then break away past it.
  Jinking is off during the run.
- **Recon** (`scan = { "<ship>", ... }`): scan each in order. The engine scans a ship held
  within max(150 m, its radius + 100 m) with the nose within ~18 degrees for its scan time
  (2 s by default), and restarts when either slips, so the pilot flies in and holds station
  nose-on. Without `stealth` it fights first and scans when no target is left.
- **Stealth** (`stealth = true`, `avoid = { "<ship>", ... }`, `avoid_radius` 750 m): never
  engage, keep farther than `avoid_radius` from each listed ship, and scan. In FSPort's "Playing
  Judas" being seen is scripted, until the Lucifer arrives: within 486 m of a patrol fighter or
  sentry gun below Medium, 586 m at Medium, and 610 m from a fighter (486 m from a sentry) at Hard
  and Insane, or pressing a fire key (sm2-08a.fs2, events "Stop Ignoring (Easy)", "Stop ignoring",
  "Stop Ignoring (Hard)").
- **Mission plan** (for a scripted mission like Playing Judas):
  - `scan_tiers = { {names}, ... }`: scan the first tier before the next (order by what leaves
    first); a tier with `front_first = true` takes the ship furthest along its direction of
    travel first (catching up from behind is slow).
  - `avoid_until = "<ship>"` stops avoiding once that ship is in the mission; with
    `avoid_lapse = {names}` only those names stop being avoided.
  - `ambush = { after, delay, first, max_delay, target, at, face, hold_until }`: from `delay`
    seconds after `after` appears (and once the `first` ships are scanned, or after `max_delay`),
    wait at `at` facing `face` until `target` appears; with `hold_until` stay until the target is
    that close.
  - `go_to = { ship, subsystem, within }`: fly into that point when the scan list is done
    (lined up 400 m outside it first); `jump_out = true` then presses Enter Subspace every 2 s
    while flying away.
  - `idle_at = {x, y, z}`: where to wait when there is nothing to do; otherwise the pilot holds
    where the idle spell began, still keeping clear of the avoid list.
  - Diagnosis: `trace = "<ship>"` logs, once a second while scanning that ship, the range, the
    nearest avoided ship, and the controls finally sent after every override; any avoided ship
    inside `avoid_radius` is logged as a "close pass".
- **ETS** (`ets`): `"weapons"` (the v2 default) moves energy from shields to guns and caps the
  Myrmidon at 56 of 75 m/s; `"balanced"` leaves it as the ship starts.

`DR.set_skill(level)` sets the difficulty (0 Very Easy to 4 Insane) through the game option, and
`DR.set_class(name)` swaps the player's ship class. `DR.queue_drills({...})` runs drills one after
another; `{"cfg", {label = ..., set = {...}}}` and `{"class", name}` steps between them change
settings, so A/B runs can alternate in one queue.
`sm1_status.lua` is a scoreboard for SM1-01 whose every field was checked against the API docs.

## Results (FSO 26.1.0 release, retail FS2 data, Easy, llvmpipe 20-35 fps, 2026-10-15)

Every row is a single run unless n is given; nothing was repeated enough for a significance
claim (1 kill in 4 against 4 in 4 is p ~ 0.14, Fisher). Frame rate 20-35 fps (machine load).

**Track drill** (scripted, invulnerable, unarmed SF Mara; hits/shots):

| Pilot | Drone 60 m/s, 90 s | Drone 45 m/s, 60 s |
|---|---|---|
| v1 (2026-10-12 controller) | 0/6 | 0/30 |
| v2 without acceleration lead | | 105/396 (26%) |
| v2 | | 271/412 (65%) |
| v2 final (recorded run) | 506/690 (73%) | 273/406 (67%) |
| v2 after the QA fixes (~21 fps) | | 232/388 (59%) |

**Duels** (one AI fighter at a time, Easy; n duels, one per spawn bearing):

| Pilot | SF Mara | SF Dragon |
|---|---|---|
| v1 | n=4: 1 kill, 3 timeouts | n=7 (two sets): 3 kills, 3 died/lost, 1 timeout |
| v2, guns only | n=4: 4 kills (7-32 s) | n=4: 4 kills (8-79 s) |
| v2, heat seekers inside 20 degrees | | n=4: 4 kills; Rockeyes 2/23 |
| v2 final (recorded run) | n=4: 4 kills (8-61 s) | n=4: 3 kills, 1 timeout |
| v2 after the QA fixes | n=2: 1 kill (5 s), 1 death forced by sexp to test scoring | n=4: 4 kills (6-67 s) |
| v2 after the cross-family fixes | | n=3: 3 kills (7-24 s) |

Caveats: v1 and the early v2 sets ran in one engine session while the drill itself changed
(spawn bearings, damage counter, rearm and death handling were added during it); v1's first
Dragon set lost a death to the old drill's restart bug. The v1 Mara set's first three duels all
spawned dead ahead and are not in the table.

**SM1-01 "Surrender, Belisarius!"** (escort two freighters; numbers at t ~ 420, final for every
run since the dogfight ends near t = 270):

| Run | Primary | Rockeyes | Kills | Iota 1 / Iota 2 hull | Alpha 1 hull |
|---|---|---|---|---|---|
| v1, 5 runs (2026-10-12 demo report) | 27-40% | 0-26% | 0-3 | 43-67 / 61-72 | 73-100 |
| v2, missiles inside 20 deg (read at t=272) | 58% | 5/5 | 4 | 67 / 53 | 100 |
| v2, tail-only missiles | 60% | 0/1 | 3 | 53 / 52 | 90 |
| v2, missiles by agility | 66% | 7/48 | 4 | 61 / 68 | 95 |
| v2, by agility + one in flight per target (shipped) | 44% | 5/28 | 3 (+1 assist) | 43 / 22 | 98 |

Better gunnery has not yet meant better escort: the freighters fared no better under v2, and the
shipped configuration had the worst freighter result. Escort is a target-priority problem the
drill does not measure.

**Learned gun offset**, GTF Myrmidon / Subach HL-7: right 0.08 m, up 1.67 m (corrected learner,
12 bolts; a second session learned 0.07 / 1.66 m). The first learner, without back-projection and while turning, gave 0.11 / 1.81 m.

Videos of the first (v1) and last (v2 final) drill sessions are kept outside the repository.

## Results by mission type (Medium, Vulkan sessions ~60-76 fps, 2026-10-15)

Small samples (2-8 runs per arm), alternating arms in one session so session drift hits both.

| Drill | Change | With | Without |
|---|---|---|---|
| Escort, solo (2 Nephilim + 1 Mara per wave, stock 2000-hull freighter) | any of today's escort rules | freighter lost in all 14 runs | |
| Escort, 3 wingmen ordered to protect via the comm menu | shooter-of-the-freighter priority | freighter kept 7/7; pilot died 0/7 | freighter kept 6/7; pilot died 3/7 (each time to the escort fighter it ignored) |
| Strike, Ursa + Cyclops vs Fenris | gun down the weapons subsystem first | cruiser destroyed 3/3, 35-788 damage taken | release at 900 m: 1/2 destroyed, 12% hull in the other; at 2 km: every bomb shot down |
| Recon, 4 containers + 2 patrols | keep out of patrol range | 3/3 all scanned, never detected | detected at 13 s every run, 1-3 scanned |
| Duel vs SF Mara, Myrmidon with class default guns | gun banks linked | 7/8 kills | 6/8 kills; the misses were the duels started with the Mara behind |

Class study (4 duels each vs SF Mara, class default loadouts, banks NOT linked yet): Ulysses,
Hercules, Hercules Mark II, Erinyes, Loki, Pegasus, Perseus 4/4; Ares 3/4; Myrmidon 1/4 in that
session (6-8/8 when re-run later; not explained). Hercules hit rates were the lowest (11-49%).

### FSPort "Playing Judas" (sm2-08a, Medium, `--mods fsport-3.7.3`, Vulkan)

Goal: success debrief with all three goals (scan the 4 warships; the 7 transports and
freighters; the 4 cargo containers), undetected, then enter the Lucifer's fighter bay and jump
out. The mission was never changed; `judas_setup.lua` (kept outside the repository) holds the
pilot settings. One thing was changed per run, each from what the last run's log showed:

| Runs | Outcome | What the log showed -> change |
|---|---|---|
| first attempts | cargo scanned, transports and Lucifer missed | the transport groups leave before all are scanned; the Lucifer warped in on top of the pilot -> scan by what leaves first; clear the path of warping-in and moving capital ships |
| it 2 | warships done, died at the fighter bay | most likely Rama wing, which launches from that bay, while the pilot waited for its warp drive (not confirmed) -> flee away from the visited ship when jumping out |
| it 3 | Kali leader missed; rammed a freighter | catching a 55 m/s transport from behind is slow; 71 m/s among docked freighters -> `front_first`; half throttle within 300 m of big hulls |
| it 4 | **success debrief** (secondary 6/7) | every switch to the next convoy member set off a sidestep from the one just scanned -> convoy members treated like the scan target by the ram check; scan hold widened to 70% of range |
| it 5-9 | detected; Eva unscanned | an Arjuna patrol crossed the route to the Eva waiting point (detection at 381 s, found by bisecting `is-event-true-delay`); Arjunas fly 88 m/s, the Dragon 75, so running does not work once one heads at you |
| it 10-11 | undetected; Eva unscanned | 1 Hz position tracks: the Eva crosses the area at up to ~300 m/s after warp-in and parks by the escape vector for ~20 s -> wait beside its parking spot, hold until it is within 1.6 km |
| it 12 | Eva scanned; detected at 440 s | the pilot had no idle behaviour; the default cruise carried it into a sentry's range while it waited for the Lucifer -> hold position, keep avoiding |
| it 13, recorded rerun | **all three goals, undetected, Meritorious Unit** | recorded run: no restarts, one light hull contact, 100% hull at the bay |

Honest limits: the waiting points come from the mission file (arrival coordinates, patrol
waypoints) and from tracks of earlier runs, which is a veteran's knowledge, not a first-time
player's. Two successful runs is not a success rate. The idle-hold change applies to every mode
and the escort and duel drills were not re-run after it.

Re-recorded with MediaVPs (`--mods fsport-mediavps-4.7.2,fsport-3.7.3,MVPS-4.7.3`, the newest
FSPort MediaVPs and the MediaVPs version it requires): tries 1-4 died in the escape after the
fighter bay, try 5 got all three goals. The deaths were Manticore rams (collision damage skips
shields: hull 100% to 51% in one contact, with shields untouched), found with a per-frame hit
tally after a 1 Hz sampler had missed them. Escape changes: afterburner and energy to shields
while the warp drive is out, continuous jinking, Alt-J only into clear space (no ship within
800 m inside 25 degrees of the nose). No table difference between the stacks explains why the
MediaVPs runs died more often; the HD models' collision shapes are unmeasured, and 0 of 4
against 2 of 3 is within chance.

### FSPort "The Hammer and the Anvil" (sm1-08a, Medium, MediaVPs stack as above)

Escort three Poseidon freighters, each docked to a 600-hit-point "Shield Prototype" container,
14.6 km at 25 m/s; primary: one container departs, bonus: all three. Five sessions at a time,
one setting each; `hammer_watch.lua` (outside the repository) logged arrivals, deaths and a
per-frame tally of the weapons that hit each container.

| Round (5 runs each) | Setting | Containers out | What it showed |
|---|---|---|---|
| 1 | pilot off / guard / wing re-ordered / guard + freighters / repeat | off: 0; pilot: 1 every run | the 4 Basilisks of Krishna (~202 s) take Gamma, the Rama Scorpions Alpha, both mostly with MX-50 heat seekers (15-18 hits per lost container), which lasers cannot shoot down |
| 2 | 1 km screen ahead, `aimed_bonus` 4000, afterburner chase | 2 in one run (`aimed_bonus` alone) | Krishna dies within 3-15 s of arriving, but its first volley is already in flight |
| 3 | `aimed_bonus` 4000 +/- `guard_weakest` | 2 in one run | the Vasudan Pi and Mu wings arrive friendly beside the convoy, turn Neutral ~40 s later and finish the weakest container (8-27 Avenger hits) |
| 4 | + `suspects` (shadow the turncoats) | 0 or 1 | shadowing does not stop five ships killing a 5-40% container within seconds |
| 5 | + `missile_block` | 1 or 0 | 20-35 missiles a run were picked to block; the MX-50 hit count does not move (29-33 vs 32) |
| 6 | + `spare` the last Rama | **all three in one run**, 2 in three | the mission cues Durga 25 s after the last Rama dies (else 220 s after Rama arrives) and the turncoats 150 s after Durga: a living Rama pushes the turncoats past the convoy's jump |
| recorded | `aimed_bonus` 4000, `guard_weakest`, `spare` (disable) | **all three**, Vasudan Alliance Medal (Gamma at 1% hull) | 1 of 5 attempts; a batch with the "ignore" order instead lost the spare within 18 s in 5/5 |

Honest limits: all three out in 2 of the 10 runs with the spare tactic as recorded (round 6 and
the recorded batch). The tactic rests on the mission's cue chain (read from the .fs2), a
veteran's knowledge.

Why "disable" keeps the Rama alive (probe of 4 runs, 2026-10-15, flag read with
`ship:getFlag("protect-ship")`): the disable goal sets the target's Protected flag (fighters are
"protected on cripple" in objecttypes.tbl; aigoals.cpp, AI_GOAL_DISABLE_SHIP), and AI ships hold
their guns on a Protected ship once its hull is below the targeted subsystem's hit points + 50
(aicode.cpp, "enemy is protected ... hull is low"). The Scorpion's engine is 35% of 180 = 63 hit
points, so the wing stopped near 113 hp; with bolts already in flight it settled at 50% hull and
stayed there for 90+ s with 8 friendlies targeting it, never disabled. The comm "Ignore my
target" is AI_GOAL_IGNORE_NEW (hudsquadmsg.cpp), which sets no Protected flag: in both probe
runs 3-7 friendlies kept shooting and it died in 4-25 s. A ship spared at 1% hull still died.
The recorded run also exposed two jump-out bugs, fixed: Alt-J pressed again while the drive
engages cancels the jump (now one press, then 15 s), and the afterburner held the speed above
the warp speed the engine waits for (PCM_WARPOUT_STAGE1, within 5%; playercontrol.cpp).

## Known limits

- Lua cannot read the HUD's missile lock or a missile's homing type: heat seekers are listed by
  class name in `A1.cfg.heat`. The selected secondary bank is read from `weaponbank.Armed`.
- The gains are derived from the ship's `$Rotdamp` (Kp 6.0, Kd 1.3 at the Myrmidon's 0.35 s) and
  were flown on the Myrmidon only. The afterburner is used only to run for the jump (and with
  `ab_chase`); Lua cannot hold the key, so `physics.AfterburnerActive` stands in for it.
- The 0.25 s history gates reset the lead and rate estimates every frame above ~4x time
  compression: fly at 1x.
- After a mission restart the pilot resets itself (the clock going backwards is the signal);
  the drill scores a death it sees as "died". A restart the drill did not see coming is scored
  "died" with its numbers marked lost; that path was not exercised.
- The gun-offset learner reads a bolt's age as lifetime used; for a weapon whose table sets a
  `$Life` range each bolt's life is randomized, so its offset could be learned wrong (retail
  lasers have a fixed life).
- Difficulty is set with `DR.set_skill`; the early results tables above were flown on Easy, the
  mission-type results and Playing Judas on Medium. Enemy AI aim and turn rate scale with it.
