-- Unit tier for the bridge (QA finding TA-10). Runs the real fsoharness.lua under luajit against
-- the stand-in engine in stubs.lua.
--   luajit tests/lua/run.lua <path to fsoharness.lua> --list     print every test name
--   luajit tests/lua/run.lua <path to fsoharness.lua> <name>     run one test (exit 1 on failure)
-- tests/test_bridge_lua.py runs each test as its own pytest case.
local here = string.match(arg[0], "^(.*)/[^/]*$") or "."
package.path = here .. "/?.lua;" .. package.path
local json = require("dkjson")   -- loaded before stubs replace error(); dkjson keeps its own copy
local stubs = require("stubs")

local BRIDGE = arg[1]
local T = {}       -- name -> function(W, H)
local ORDER = {}   -- names in definition order, for --list

local function test(name, fn)
	T[name] = fn
	ORDER[#ORDER + 1] = name
end

-- ---- helpers -------------------------------------------------------------------------------

local function fail(msg) stubs.real_error(msg, 2) end

local function eq(got, want, what)
	if got ~= want then
		fail(string.format("%s: got %s, want %s", what or "value", tostring(got), tostring(want)))
	end
end

local function truthy(v, what) if not v then fail((what or "condition") .. " was false") end end

local function has(text, part, what)
	if not string.find(tostring(text), part, 1, true) then
		fail(string.format("%s: %q does not contain %q", what or "text", tostring(text), part))
	end
end

local function cache(W) return W.files["data/cache"] end

-- Leave command `seq` in the channel folder, the way channel.py does.
local function send(W, seq, cmd, args)
	cache(W)[string.format("cmd-%06d.json", seq)] =
		json.encode({ seq = seq, cmd = cmd, args = args or json.null })
end

-- Return the decoded reply to `seq`, or nil when there is none yet.
local function reply(W, seq)
	local text = cache(W)[string.format("res-%06d.json", seq)]
	return text and json.decode(text)
end

local function frames(H, n, in_dialog)
	for _ = 1, n do H.frame(in_dialog == true) end
end

-- Send one command, run one frame, return its reply (which must exist).
local function call(W, H, seq, cmd, args)
	send(W, seq, cmd, args)
	frames(H, 1)
	local r = reply(W, seq)
	truthy(r, "a reply to " .. cmd)
	return r
end

local function heartbeat(W) return json.decode(cache(W)["ready.json"]) end

-- Load a fresh bridge into a fresh world; switches (a table) go into switches.txt first.
local function boot(W, switches)
	if switches then cache(W)["switches.txt"] = json.encode(switches) end
	local H = dofile(BRIDGE)
	H.init()
	return H
end

-- ---- SA-1 / SF-2: names that go into sexps ----------------------------------------------------

test("quoted_refuses_delimiters_and_control_characters", function(W)
	local H = boot(W)
	for _, bad in ipairs({ "a!b", 'a"b', "a(b", "a)b", "a\nb", "a\1b", "" }) do
		truthy(not pcall(H.quoted, bad), "refusal of " .. string.format("%q", bad))
	end
	eq(H.quoted("Alpha 1"), "!Alpha 1!", "plain name")
end)

test("check_injection_never_reaches_the_engine", function(W)
	local H = boot(W)
	local inject = 'x" ( script-eval-num "return 0" ) "y'
	for i, kind in ipairs({ "event_true", "event_false", "destroyed", "departed" }) do
		local r = call(W, H, i, "check", { kind = kind, value = inject })
		eq(r.ok, false, kind .. " ok")
		eq(r.kind, "refused", kind .. " kind")
	end
	eq(#W.sexps, 0, "sexps evaluated")
end)

test("check_refuses_unknown_names_before_building_a_sexp", function(W)
	local H = boot(W)
	local r = call(W, H, 1, "check", { kind = "event_true", value = "No Such Event" })
	has(r.error, "no event named No Such Event")
	r = call(W, H, 2, "check", { kind = "destroyed", value = "Nobody" })
	has(r.error, "no ship named Nobody")
	-- "1" is not an event name even though the engine's indexer would read it as index 1.
	r = call(W, H, 3, "check", { kind = "event_true", value = "1" })
	has(r.error, "no event named 1")
	eq(#W.sexps, 0, "sexps evaluated")
end)

test("check_accepts_events_ships_wings_and_departed_ships", function(W)
	local H = boot(W)
	W.sexp_results["( is-event-true-delay !Destroy Iota! 0 )"] = true
	eq(call(W, H, 1, "check", { kind = "event_true", value = "Destroy Iota" }).result.met, true,
	   "event")
	eq(call(W, H, 2, "check", { kind = "destroyed", value = "Alpha" }).ok, true, "wing")
	eq(call(W, H, 3, "check", { kind = "departed", value = "Gone 1" }).ok, true, "departed ship")
	eq(call(W, H, 4, "check", { kind = "destroyed", value = "alpha 1" }).ok, true, "case")
end)

test("final_sexp_text_is_held_to_the_eval_opt_in", function(W)
	-- An event whose own name contains "eval": the built text runs no Lua, but the bridge checks
	-- the final text, so it is refused without the opt-in and audited with it.
	W.events[#W.events + 1] = stubs.handle({ Name = "Re-evaluate" })
	local H = boot(W)
	local r = call(W, H, 1, "check", { kind = "event_true", value = "Re-evaluate" })
	eq(r.ok, false, "without opt-in")
	eq(#W.sexps, 0, "sexps evaluated")
	cache(W)["allow_eval"] = ""
	H = boot(W)
	eq(call(W, H, 2, "check", { kind = "event_true", value = "Re-evaluate" }).ok, true, "opted in")
	has(W.printed[1], "FSOH-AUDIT sexp-script ( is-event-true-delay !Re-evaluate! 0 )")
end)

test("sexp_refuses_text_the_engine_buffer_cannot_hold", function(W)
	local H = boot(W)
	local r = call(W, H, 1, "sexp", { expr = "( true " .. string.rep(" ", 8000) .. ")" })
	has(r.error, "characters")
	eq(#W.sexps, 0, "sexps evaluated")
end)

test("sexp_false_carries_a_note_that_it_may_not_have_parsed", function(W)
	local H = boot(W)
	local r = call(W, H, 1, "sexp", { expr = "( is-destroyed-delay 0 !Alpha 1! " })
	eq(r.result.value, false, "value")
	has(r.result.note, "did not parse")
	W.sexp_results["( true )"] = true
	eq(call(W, H, 2, "sexp", { expr = "( true )" }).result.note, nil, "note on true")
end)

test("lua_and_post_event_are_audited_in_the_log", function(W)
	cache(W)["allow_eval"] = ""
	local H = boot(W)
	eq(call(W, H, 1, "lua", { code = "return 1 + 1" }).result.value, "2", "lua value")
	call(W, H, 2, "post_event", { name = "GS_EVENT_QUIT_GAME" })
	eq(W.printed[1], "FSOH-AUDIT lua return 1 + 1", "lua audit")
	eq(W.printed[2], "FSOH-AUDIT post_event GS_EVENT_QUIT_GAME", "event audit")
end)

-- ---- CR-12 / SF-15: the state report ------------------------------------------------------------

test("state_reports_an_unquotable_event_as_unknown", function(W)
	W.events[#W.events + 1] = stubs.handle({ Name = "Say \"hi\"!" })
	local H = boot(W)
	local r = call(W, H, 1, "state", {})
	eq(r.ok, true, "report ok")
	eq(r.result.events[3].status, "unknown", "bad name")
	eq(r.result.events[1].status, "pending", "good name")
end)

test("state_unreadable_fields_are_null_and_ship_list_says_truncated", function(W)
	stubs.unset(W.ships[1], "HitpointsMax")                    -- the engine reports nil
	W.ships[1].isDying = function() local t = nil return t.x end  -- a plain Lua error
	W.ships[1].Class = {}                                         -- no Name to read
	local H = boot(W)
	local r = call(W, H, 1, "state", { limit = 1 })
	local s = r.result.ships[1]
	eq(s.hull_pct, nil, "hull_pct")           -- JSON null decodes to nil
	eq(s.dying, nil, "dying")
	eq(s.class, nil, "class")
	eq(#r.result.ships, 1, "listed")
	eq(r.result.ships_total, 2, "ships_total")
	eq(r.result.ships_truncated, true, "ships_truncated")
end)

-- ---- goals ------------------------------------------------------------------------------------

test("goal_conditions_parse_name_equals_status", function(W)
	local H = boot(W)
	eq(call(W, H, 1, "check", { kind = "goal", value = "Protect Iota=incomplete" }).result.met,
	   true, "incomplete")
	eq(call(W, H, 2, "check", { kind = "goal", value = "Protect Iota=complete" }).result.met,
	   false, "complete")
	has(call(W, H, 3, "check", { kind = "goal", value = "Protect Iota" }).error, "NAME=status")
	has(call(W, H, 4, "check", { kind = "goal", value = "A=B=complete" }).error,
	    "no goal named A=B")
end)

test("goal_status_values_map_and_unknown_values_are_not_guessed", function(W)
	-- QA finding TA-12: every status value must map to its own word; a value outside the map
	-- reads "unknown", not the "incomplete" the old fallback produced.
	W.goals = { stubs.handle({ Name = "G0", isGoalSatisfied = 0 }),
	            stubs.handle({ Name = "G1", isGoalSatisfied = 1 }),
	            stubs.handle({ Name = "G2", isGoalSatisfied = 2 }),
	            stubs.handle({ Name = "G9", isGoalSatisfied = 9 }) }
	local H = boot(W)
	local want = { G0 = "failed", G1 = "complete", G2 = "incomplete", G9 = "unknown" }
	for i, name in ipairs({ "G0", "G1", "G2", "G9" }) do
		local r = call(W, H, i, "check", { kind = "goal", value = name .. "=" .. want[name] })
		eq(r.result.met, true, name)
		eq(r.result.detail, want[name], name .. " detail")
	end
	local goals = call(W, H, 9, "state", { ships = false, events = false }).result.goals
	eq(goals[2].status, "complete", "state G1")
	eq(goals[4].status, "unknown", "state G9")
end)

-- ---- channel ----------------------------------------------------------------------------------

test("commands_run_in_number_order_across_gaps", function(W)
	local H = boot(W)
	local seen = {}
	H.handlers.rec = function(_, seq) seen[#seen + 1] = seq return {} end
	send(W, 7, "rec")
	send(W, 3, "rec")
	send(W, 5, "rec")
	frames(H, 1)
	eq(table.concat(seen, ","), "3,5,7", "order")
	truthy(reply(W, 3) and reply(W, 5) and reply(W, 7), "three replies")
	eq(cache(W)["cmd-000003.json"], nil, "command file removed")
end)

test("a_command_runs_only_if_its_withdrawal_failed", function(W)
	-- channel.py withdraws a timed-out command by renaming it to .withdrawn and reports "it did
	-- not run" when that rename succeeds (QA finding SL-6). So the bridge must take a command by
	-- renaming it before reading it. Model the race: the caller's rename happens right after the
	-- bridge's first file operation on the command.
	local H = boot(W)
	local ran, withdrew, raced = false, false, false
	H.handlers.rec = function() ran = true return {} end
	send(W, 1, "rec")
	local name = "cmd-000001.json"
	local function caller_withdraws()
		if raced then return end
		raced = true
		if cache(W)[name] ~= nil then
			cache(W)[name .. ".withdrawn"], cache(W)[name] = cache(W)[name], nil
			withdrew = true
		end
	end
	local open, rename = cf.openFile, cf.renameFile
	cf.openFile = function(n, ...)
		local f = open(n, ...)
		if n == name then caller_withdraws() end
		return f
	end
	cf.renameFile = function(old, ...)
		local r = rename(old, ...)
		if old == name then caller_withdraws() end
		return r
	end
	frames(H, 1)
	truthy(raced, "the bridge touched the command")
	truthy(ran ~= withdrew, "exactly one side won (ran=" .. tostring(ran) .. ", withdrawn="
	       .. tostring(withdrew) .. ")")
end)

test("files_are_replaced_without_a_moment_of_absence", function(W)
	-- QA finding XK-6: the heartbeat must never be missing between two writes. rename replaces
	-- the target (cf_rename is rename(2)), so the bridge must not delete it first.
	local H = boot(W)
	frames(H, 1)
	truthy(cache(W)["ready.json"], "first heartbeat")
	local delete, deleted = cf.deleteFile, false
	cf.deleteFile = function(name, path)
		if name == "ready.json" then deleted = true end
		return delete(name, path)
	end
	frames(H, H.HEARTBEAT_EVERY * 2)
	eq(deleted, false, "ready.json deleted before being replaced")
	eq(heartbeat(W).frame, H.HEARTBEAT_EVERY * 2 + 1, "latest heartbeat")
	eq(cache(W)["ready.json.tmp"], nil, "temp file left")
end)

test("a_command_whose_claim_fails_is_not_run", function(W)
	-- If the bridge's rename does not take the file (the caller withdrew it first), the bridge
	-- must not read or run it.
	local H = boot(W)
	local ran = false
	H.handlers.rec = function() ran = true return {} end
	send(W, 1, "rec")
	cf.renameFile = function() return true end   -- engine-style failure: nothing moved
	frames(H, 1)
	eq(ran, false, "ran")
	eq(reply(W, 1), nil, "reply")
end)

test("replies_say_refused_or_script_error", function(W)
	cache(W)["allow_eval"] = ""
	local H = boot(W)
	eq(call(W, H, 1, "nope").kind, "refused", "unknown command")
	local r = call(W, H, 2, "lua", { code = "local t = nil return t.x" })
	eq(r.kind, "script_error", "runtime error in caller Lua")
	eq(call(W, H, 3, "lua", { code = "error('stop')" }).kind, "script_error", "error() in caller Lua")
	eq(call(W, H, 4, "lua", { code = "return (" }).kind, "script_error", "compile error")
end)

test("popup_log_is_bounded", function(W)
	local H = boot(W, { dismiss_popups = true })
	W.popup_text = "Warning"
	frames(H, H.POPUP_LOG_MAX + 5, true)
	eq(#H.popups, H.POPUP_LOG_MAX, "kept")
	local _, lines = string.gsub(cache(W)["popups.txt"], "\n", "")
	eq(lines, H.POPUP_LOG_MAX, "lines in popups.txt")
	eq(#W.submits, H.POPUP_LOG_MAX + 5, "every popup answered")
end)

-- ---- SF-1: hooks, heartbeat, stage switches ----------------------------------------------------

test("each_state_hook_is_guarded_and_retried_a_few_times", function(W)
	local H = boot(W)
	local a_calls, b_calls = 0, 0
	H.on_state("GS_STATE_MAIN_MENU", function() a_calls = a_calls + 1 local t = nil return t.x end)
	H.on_state("GS_STATE_MAIN_MENU", function() b_calls = b_calls + 1 end)
	W.state = "GS_STATE_MAIN_MENU"
	frames(H, 10)
	eq(b_calls, 1, "second hook ran once")
	eq(a_calls, H.HOOK_TRIES, "failing hook tries")
	eq(H.errors, H.HOOK_TRIES, "errors counted")
end)

test("heartbeat_is_written_even_when_the_frame_body_fails", function(W)
	local H = boot(W)
	cf.listFiles = function() local t = nil return t.x end
	frames(H, 1)
	local hb = heartbeat(W)
	truthy(hb, "ready.json")
	eq(hb.frame, 1, "frame")
	eq(H.errors, 1, "errors")
end)

test("stage_switches_report_pending_then_done", function(W)
	W.state = "GS_STATE_MAIN_MENU"
	local H = boot(W, { pilot_mode = "create", skip_command_briefing = true, skip_briefing = true })
	frames(H, 1)
	local hb = heartbeat(W)
	eq(hb.switches.pilot_mode, "pending", "pilot before")
	eq(hb.errors, 0, "errors")
	eq(hb.last_error, nil, "last_error")
	W.state = "GS_STATE_INITIAL_PLAYER_SELECT"
	frames(H, 1)
	W.state = "GS_STATE_CMD_BRIEF"
	frames(H, 1)
	W.state = "GS_STATE_BRIEFING"
	frames(H, 10)
	W.state = "GS_STATE_GAME_PLAY"
	frames(H, 30)
	hb = heartbeat(W)
	eq(hb.switches.pilot_mode, "done", "pilot")
	eq(hb.switches.skip_command_briefing, "done", "command briefing")
	eq(hb.switches.skip_briefing, "done", "briefing")
end)

test("a_failed_stage_switch_is_reported_with_its_reason", function(W)
	ui.Briefing.commitToMission = function() return COMMIT_FAIL end
	local H = boot(W, { skip_briefing = true })
	W.state = "GS_STATE_BRIEFING"
	frames(H, 10)
	frames(H, 30)
	local hb = heartbeat(W)
	has(hb.switches.skip_briefing, "failed: ")
	eq(hb.errors, 1, "errors")
	has(hb.last_error, "skip_briefing")
end)

test("no_switches_still_heartbeat_an_empty_object", function(W)
	local H = boot(W)
	frames(H, 1)
	has(cache(W)["ready.json"], '"switches":{}')
end)

test("briefing_commit_does_not_count_dialog_frames", function(W)
	local H = boot(W, { skip_briefing = true })
	W.state = "GS_STATE_BRIEFING"
	frames(H, 1)                        -- state hook registers the commit timer
	frames(H, H.BRIEFING_COMMIT_FRAME + 5, true)
	eq(W.committed, nil, "committed during a popup")
	frames(H, H.BRIEFING_COMMIT_FRAME)
	eq(W.committed, true, "committed after it closed")
end)

-- ---- SL-5 / SF-3 / SF-14: pilot, cancel, time, autopilot ---------------------------------------

test("a_throwing_pilot_tick_releases_the_latch_and_control", function(W)
	local H = boot(W)
	W.player.firePrimary = function() local t = nil return t.x end
	send(W, 1, "pilot", { primary = true, seconds = 5 })
	frames(H, 3)
	eq(W.control_mode, "NORMAL", "control mode")
	eq(H.piloting, false, "latch")
	local r = reply(W, 1)
	truthy(r, "a reply")
	eq(r.ok, false, "ok")
	eq(r.kind, "script_error", "kind")
	truthy(H.errors >= 1, "error counted")
	send(W, 2, "pilot", { seconds = 1 })
	frames(H, 1)
	eq(reply(W, 2), nil, "no refusal for a new pilot")
	eq(H.piloting, true, "a new pilot is flying")
end)

test("pilot_reports_completed_and_why_it_stopped", function(W)
	local H = boot(W)
	send(W, 1, "pilot", { seconds = 1 })
	frames(H, 2)
	W.mission_time = W.mission_time + 2
	frames(H, 1)
	local r = reply(W, 1).result
	eq(r.completed, true, "completed")
	eq(r.stopped_because, "time", "stopped_because")
	send(W, 2, "pilot", { seconds = 1 })
	frames(H, 1)
	W.player = { isValid = function() return false end }
	frames(H, 1)
	r = reply(W, 2).result
	eq(r.completed, false, "completed")
	eq(r.stopped_because, "player_dead", "stopped_because")
end)

test("pilot_ends_at_a_frame_bound_when_mission_time_stops", function(W)
	local H = boot(W)
	send(W, 1, "pilot", { seconds = 1 })
	frames(H, 1)
	local bound = math.ceil((1 * 10 + 10) * H.PILOT_FPS_BOUND)
	frames(H, bound)
	local r = reply(W, 1)
	truthy(r, "a reply within the bound")
	eq(r.result.stopped_because, "frame_limit", "stopped_because")
	eq(W.control_mode, "NORMAL", "control mode")
end)

test("pilot_cancel_releases_a_running_flight", function(W)
	local H = boot(W)
	eq(call(W, H, 1, "pilot_cancel", { seq = 2 }).result.cancelled, false, "nothing to cancel")
	send(W, 2, "pilot", { seconds = 30 })
	frames(H, 2)
	eq(W.control_mode, "FULL", "flying")
	eq(call(W, H, 3, "control_mode").result.flight_seq, 2, "control_mode names the flight")
	eq(call(W, H, 4, "pilot_cancel", { seq = 2 }).result.cancelled, true, "cancelled")
	eq(W.control_mode, "NORMAL", "control mode")
	eq(H.piloting, false, "latch")
	eq(reply(W, 2).result.stopped_because, "cancelled", "flight's own reply")
	frames(H, 2)
	eq(#H.timers, 0, "timer gone")
end)

test("pilot_cancel_only_cancels_the_flight_it_names", function(W)
	-- R5 / Y-4: a late cancel for flight A must not stop flight B that started after A ended.
	local H = boot(W)
	send(W, 5, "pilot", { seconds = 30 })
	frames(H, 1)
	local r = call(W, H, 6, "pilot_cancel", { seq = 4 }).result
	eq(r.cancelled, false, "other seq")
	eq(r.reason, "no matching flight", "reason")
	eq(call(W, H, 7, "pilot_cancel", {}).result.cancelled, false, "no seq")
	eq(W.control_mode, "FULL", "still flying")
	eq(reply(W, 5), nil, "flight still running")
end)

test("a_flight_stops_as_paused_when_the_game_pauses", function(W)
	-- SL2-4: mission time stops while paused, so the flight must end then, not hold the controls.
	local H = boot(W)
	send(W, 1, "pilot", { seconds = 30 })
	frames(H, 2)
	W.state = "GS_STATE_GAME_PAUSED"
	frames(H, 1)
	local r = reply(W, 1)
	truthy(r, "a reply")
	eq(r.result.stopped_because, "paused", "stopped_because")
	eq(r.result.completed, false, "completed")
	eq(W.control_mode, "NORMAL", "control mode")
	eq(H.piloting, false, "latch")
end)

test("a_flight_stops_as_paused_under_a_dialog", function(W)
	local H = boot(W)
	send(W, 1, "pilot", { seconds = 30 })
	frames(H, 2)
	frames(H, 1, true)
	eq(reply(W, 1).result.stopped_because, "paused", "stopped_because")
	eq(W.control_mode, "NORMAL", "control mode")
end)

test("the_poll_lists_only_command_files", function(W)
	-- R5 / SL2-8: replies nobody collected must not grow the per-frame scan.
	local H = boot(W)
	for i = 1, 20 do cache(W)[string.format("res-%06d.json", i)] = "{}" end
	frames(H, 2)
	truthy(#W.listed > 0, "listFiles called")
	for _, list in ipairs(W.listed) do
		for _, name in ipairs(list) do has(name, "cmd-", "listed name") end
	end
end)

test("pilot_is_refused_while_paused_or_under_a_dialog", function(W)
	local H = boot(W)
	W.state = "GS_STATE_GAME_PAUSED"
	has(call(W, H, 1, "pilot", { seconds = 1 }).error, "paused")
	W.state = "GS_STATE_GAME_PLAY"
	send(W, 2, "pilot", { seconds = 1 })
	frames(H, 1, true)
	has(reply(W, 2).error, "dialog")
	eq(W.control_mode, "NORMAL", "control mode")
end)

test("time_replies_with_values_read_back_after_the_change", function(W)
	local H = boot(W)
	send(W, 1, "time", { compression = 400 })
	frames(H, 1)
	eq(reply(W, 1), nil, "no reply on the same frame")
	W.mission_frametime, W.real_frametime = 0.08, 0.02
	frames(H, H.TIME_READBACK_FRAMES)
	local r = reply(W, 1).result
	eq(r.compression, 400, "read-back compression")
	eq(r.requested_compression, 400, "requested")
	eq(r.paused, false, "paused")
	eq(r.state, "GS_STATE_GAME_PLAY", "state")
	send(W, 2, "time", { pause = true })
	frames(H, 1)
	W.state = "GS_STATE_GAME_PAUSED"
	frames(H, H.TIME_READBACK_FRAMES)
	r = reply(W, 2).result
	eq(r.paused, true, "paused after")
	eq(r.compression, nil, "compression unreadable while paused")
end)

test("autopilot_reports_the_sexp_result", function(W)
	local H = boot(W)
	mn.runSEXP = function() return false end
	local r = call(W, H, 1, "autopilot", { on = true }).result
	eq(r.on, true, "on")
	eq(r.applied, false, "applied")
end)

-- ---- C4 / C5: clock and version ---------------------------------------------------------------

test("clock_reports_overall_and_mission_time", function(W)
	local H = boot(W)
	local r = call(W, H, 1, "clock").result
	eq(r.frametime_overall, 100, "overall")
	eq(r.mission_time, 10, "mission")
	W.in_mission = false
	eq(call(W, H, 2, "clock").result.mission_time, nil, "no mission")
end)

test("clock_counts_only_normal_gameplay_frames", function(W)
	-- profiling.csv has one row per gameplay frame flip; popups during gameplay run their own
	-- loop without the frame timer, and other states are not traced at all. So the counter that
	-- maps to CSV rows skips dialog frames and non-gameplay states.
	local H = boot(W)
	frames(H, 4)
	frames(H, 3, true)
	W.state = "GS_STATE_MAIN_MENU"
	frames(H, 2)
	W.state = "GS_STATE_GAME_PLAY"
	eq(call(W, H, 1, "clock").result.gameplay_frames, 5, "4 normal gameplay frames + the call's")
end)

test("version_comes_from_the_installed_version_file", function(W)
	W.files["data/text"]["fsoharness-version.txt"] = "abc123\n"
	local H = boot(W)
	eq(H.VERSION, "abc123", "VERSION")
	eq(call(W, H, 1, "bridge_info").result.version, "abc123", "bridge_info")
	W.files["data/text"]["fsoharness-version.txt"] = nil
	eq(boot(W).VERSION, "unknown", "missing file")
end)

test("a_version_file_in_data_scripts_is_invisible_like_in_the_engine", function(W)
	-- The engine indexes only .lua/.lc/.fnl under data/scripts (cfile.cpp:86); the first
	-- implementation put the .txt there and the real engine reported "unknown".
	W.files["data/scripts"]["fsoharness-version.txt"] = "abc123\n"
	eq(boot(W).VERSION, "unknown", "txt under data/scripts")
end)

-- ---- wave 2: frame guard, heartbeat cadence, switch end state, table wiring --------------------

test("a_failing_state_read_never_escapes_the_frame", function(W)
	-- CR2-7: everything H.frame does runs inside a pcall; an escaping error is an engine error.
	local H = boot(W)
	ba.getCurrentGameState = function() local t = nil return t.x end
	truthy(pcall(H.frame, false), "H.frame raised")
	truthy(H.errors >= 1, "error counted")
end)

test("heartbeat_is_written_at_least_every_second_at_low_frame_rates", function(W)
	-- SL2-3 / TA2-5: at 4 fps, 30 frames is 7.5 s, past the 5 s stale threshold.
	local H = boot(W)
	W.real_frametime = 0.25
	frames(H, 5)
	eq(heartbeat(W).frame, 5, "heartbeat after 1 s of frames")
	frames(H, 4)
	eq(heartbeat(W).frame, 9, "and the next one")
end)

test("switches_never_shown_read_not_shown_once_in_gameplay", function(W)
	-- CR2-9: a switch whose screen the run never visited must not stay "pending" forever.
	W.state = "GS_STATE_MAIN_MENU"
	local H = boot(W, { pilot_mode = "create", skip_command_briefing = true, skip_briefing = true })
	frames(H, 1)
	W.state = "GS_STATE_INITIAL_PLAYER_SELECT"
	frames(H, 1)
	W.state = "GS_STATE_GAME_PLAY"
	frames(H, 1)
	eq(H.switch_status.pilot_mode, "done", "visited")
	eq(H.switch_status.skip_command_briefing, "not shown", "command briefing")
	eq(H.switch_status.skip_briefing, "not shown", "briefing")
end)

test("every_bridge_function_the_table_calls_exists", function(W)
	-- TA2-8: the .tbm wires engine hooks to FsoHarness.<name>; a renamed function would only
	-- fail inside the engine.
	local tbm = string.gsub(BRIDGE, "scripts/fsoharness%.lua$", "tables/fsoharness-sct.tbm")
	local f = io.open(tbm, "r")
	truthy(f, "open " .. tbm)
	local text = f:read("*a")
	f:close()
	local H = boot(W)
	local n = 0
	for name in string.gmatch(text, "FsoHarness%.([%w_]+)") do
		n = n + 1
		eq(type(H[name]), "function", "FsoHarness." .. name)
	end
	truthy(n >= 3, "references found (" .. n .. ")")
end)

-- ---- runner -----------------------------------------------------------------------------------

if arg[2] == "--list" then
	for _, name in ipairs(ORDER) do print(name) end
	os.exit(0)
end
local fn = T[arg[2]]
if not fn then
	io.stderr:write("no test named " .. tostring(arg[2]) .. "\n")
	os.exit(2)
end
local W = stubs.install()
local ok, err = pcall(fn, W)
stubs.restore()
if ok and W.fatal then ok, err = false, "bridge called the fatal error(): " .. W.fatal end
if not ok then
	io.stderr:write(tostring(err) .. "\n")
	os.exit(1)
end
print("ok " .. arg[2])
