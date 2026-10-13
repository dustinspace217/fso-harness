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

-- ---- collision alerts and telemetry ---------------------------------------------------------

-- A strict {x, y, z} vector: reading any other field is fatal, as on an engine vector (QA CR-6).
local function vec(x, y, z) return stubs.handle({ x = x, y = y, z = z }) end

-- A ship that can fly and be hit: signature, axes, physics and a target, as the new code reads.
-- Orientation and Physics are strict handles, so a misspelled field fails the test (QA CR-6).
-- Default axes are the world axes; `axes` = {r, u, f} vectors overrides them.
local function flyer(name, sig, extra, axes)
	axes = axes or { vec(1, 0, 0), vec(0, 1, 0), vec(0, 0, 1) }
	local e = {
		getSignature = function() return sig end,
		Orientation = stubs.handle({ getRvec = function() return axes[1] end,
		                             getUvec = function() return axes[2] end,
		                             getFvec = function() return axes[3] end }),
		Physics = stubs.handle({ Velocity = vec(0, 0, 50),
		                         RotationalVelocity = vec(0, 0, 0),
		                         RotationalVelocityMax = vec(2, 2, 2) }),
		Position = vec(0, 0, 0), HitpointsLeft = 75, HitpointsMax = 100,
	}
	for k, v in pairs(extra or {}) do e[k] = v end
	return stubs.ship(name, e)
end

-- Run the collision hook once with hook variables `hv` (the engine sets them per call).
local function collide(H, hv)
	stubs.world.hv = hv
	H.ship_collision()
	stubs.world.hv = nil
end

-- One player-vs-`other` contact in the current simulation frame, as the engine reports it: twice,
-- with Ship and ShipB swapped.
local function contact(H, W, other, hitpos)
	local p = hitpos or vec(0, 0, 5)
	collide(H, { Ship = W.player, ShipB = other, Hitpos = p })
	collide(H, { Ship = other, ShipB = W.player, Hitpos = p })
end

test("collision_ignores_weapons_beams_and_other_ships", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	local iota, leo = flyer("Iota", 2), flyer("Leo", 3)
	collide(H, { Ship = W.player, ShipB = iota, Weapon = iota })
	collide(H, { Ship = W.player, ShipB = iota, Beam = iota })
	collide(H, { Ship = leo, ShipB = iota })
	eq(H.collisions_total, 0, "ignored contacts")
	eq(H.last_collision, nil, "no record")
end)

test("collision_counts_contact_starts_not_contact_frames", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	local iota, leo = flyer("Iota", 2), flyer("Leo", 3)
	contact(H, W, iota)
	eq(H.collisions_total, 1, "first contact, reported twice")
	eq(H.last_collision.new, true, "new")
	for _ = 1, 5 do frames(H, 1); contact(H, W, iota) end   -- grinding along the hull
	eq(H.collisions_total, 1, "continuing contact is one contact")
	eq(H.last_collision.new, false, "ongoing")
	contact(H, W, leo)                                       -- a second ship, same frame
	eq(H.collisions_total, 2, "a different object counts")
	frames(H, 2)                                             -- a contact-free frame
	contact(H, W, iota)
	eq(H.collisions_total, 3, "touching again after a gap counts")
end)

test("collision_direction_is_in_the_ships_own_axes", function(W)
	local H = boot(W)
	-- Nose along world +x, right along world -z, up along world +y (QA TA-1).
	W.player = flyer("Alpha 1", 1, nil, { vec(0, 0, -1), vec(0, 1, 0), vec(1, 0, 0) })
	contact(H, W, flyer("Iota", 2), vec(5, 0, 0))
	local d = H.last_collision.direction
	eq(d.forward, 1, "ahead"); eq(d.right, 0, "right"); eq(d.up, 0, "up")
	frames(H, 2)
	contact(H, W, flyer("Leo", 3), vec(0, -6, 1))
	d = H.last_collision.direction
	eq(d.up, -0.99, "below"); eq(d.forward, 0, "forward"); eq(d.right, -0.16, "left")
end)

test("collision_record_fields", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1, { HitpointsMax = 200, HitpointsLeft = 75 })
	rawset(W.player.Physics, "Velocity", vec(1, 1, 1))
	W.mission_time = 12.345
	contact(H, W, flyer("Iota", 2))
	local c = H.last_collision
	eq(c.hull_pct, 37.5, "hull %"); eq(c.speed, 1.73, "speed"); eq(c.other, "Iota", "other")
	eq(c.other_kind, "ship", "kind"); eq(c.mission, "sm1-01", "mission")
	eq(c.mission_time, 12.35, "time"); eq(c.count, 1, "count")
end)

test("collision_kinds_without_a_ship", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	local cases = { { "Debris", "debris" }, { "Asteroid", "asteroid" } }
	for i, case in ipairs(cases) do
		frames(H, 2)
		local obj = stubs.handle({ Name = "x", getSignature = function() return 100 + i end })
		collide(H, { Ship = W.player, [case[1]] = obj, Hitpos = vec(1, 0, 0) })
		eq(H.last_collision.other_kind, case[2], case[1])
		eq(H.last_collision.other, json.null, case[1] .. " has no ship name")
		eq(H.collisions_total, i, case[1] .. " counted")
	end
	-- Two asteroids, one after the other with no gap: two contacts (cross-family XFA-1).
	frames(H, 2)
	local rock = function(sig) return stubs.handle({ Name = "r", getSignature = function() return sig end }) end
	collide(H, { Ship = W.player, Asteroid = rock(7), Hitpos = vec(1, 0, 0) })
	frames(H, 1)
	collide(H, { Ship = W.player, Asteroid = rock(8), Hitpos = vec(1, 0, 0) })
	eq(H.collisions_total, 4, "a second asteroid is a new contact")
	frames(H, 1)
	collide(H, { Ship = W.player, Asteroid = rock(8), Hitpos = vec(1, 0, 0) })
	eq(H.collisions_total, 4, "the same asteroid next frame is the same contact")
	-- The rammed ship destroyed in this frame: an invalid handle, no other variable set.
	frames(H, 2)
	local dead = flyer("Gone", 9)
	dead.isValid = function() return false end
	collide(H, { Ship = W.player, ShipB = dead, Hitpos = vec(1, 0, 0) })
	eq(H.last_collision.other_kind, "unknown", "invalid ship")
	-- No hit position (no engine call site does this): null direction, never an invented one.
	frames(H, 2)
	collide(H, { Ship = W.player, ShipB = flyer("Iota", 2) })
	eq(H.last_collision.direction, json.null, "no direction")
	frames(H, 2)
	collide(H, { Ship = W.player, ShipB = flyer("Leo", 3), Hitpos = vec(0, 0, 0) })
	eq(H.last_collision.direction, json.null, "contact at our own position has no direction")
end)

test("a_pause_or_dialog_does_not_turn_one_contact_into_two", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	local iota = flyer("Iota", 2)
	contact(H, W, iota)
	frames(H, 3, true)                                       -- dialog frames: no simulation
	W.state = "GS_STATE_GAME_PAUSED"; frames(H, 3); W.state = "GS_STATE_GAME_PLAY"
	eq(H.touching(), true, "still touching across the pause (cross-family XFA-4)")
	frames(H, 1)
	contact(H, W, iota)                                      -- the same contact, resumed
	eq(H.collisions_total, 1, "no new contact after the pause")
end)

test("state_shows_a_contact_from_this_very_frame_at_a_fractional_time", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	W.mission_time = 12.345                                  -- stored rounded up to 12.35
	frames(H, 1)
	contact(H, W, flyer("Iota", 2))
	local p = call(W, H, 1, "state", { ships = false, goals = false, events = false }).result.player
	eq(p.collision.other, "Iota", "shown (cross-family XFA-6)")
end)

test("a_failed_collision_record_changes_nothing", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	local iota = flyer("Iota", 2)
	rawset(W.player.Physics, "Velocity", "not a vector")   -- a plain Lua error, not a field read
	collide(H, { Ship = W.player, ShipB = iota, Hitpos = vec(0, 0, 5) })
	eq(H.collisions_total, 0, "count unchanged (QA SF-1)")
	eq(H.last_collision, nil, "no record")
	eq(H.collision_errors, 1, "error counted (QA SF-3)")
	truthy(H.collision_last_error, "error kept")
	rawset(W.player.Physics, "Velocity", vec(0, 0, 50))
	collide(H, { Ship = iota, ShipB = W.player, Hitpos = vec(0, 0, 5) })   -- the swapped call
	eq(H.collisions_total, 1, "the swapped call counts it once")
	local p = call(W, H, 1, "state", { ships = false, goals = false, events = false }).result.player
	eq(p.collision_errors, 1, "state shows the error count")
	local d = call(W, H, 2, "check", { kind = "collision", value = -1 }).result.detail
	eq(d.errors, 1, "check shows it"); truthy(d.last_error, "check shows the message")
end)

test("pilot_stops_on_a_new_contact_with_its_direction", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	send(W, 1, "pilot", { seconds = 30, forward = 1 })
	frames(H, 2)
	contact(H, W, flyer("Iota", 2), vec(0, 0, 5))
	frames(H, 1)
	local r = reply(W, 1).result
	eq(r.stopped_because, "collision", "stopped_because")
	eq(r.collision.direction.forward, 1, "direction")
	eq(r.collisions, 1, "contacts during the flight"); eq(r.touching, true, "still touching")
	eq(H.piloting, false, "latch released"); eq(W.control_mode, "NORMAL", "control handed back")
end)

test("pilot_reports_the_new_contact_not_one_still_grinding", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	local iota, leo = flyer("Iota", 2), flyer("Leo", 3)
	contact(H, W, iota); frames(H, 1); contact(H, W, iota)   -- grinding Iota
	send(W, 1, "pilot", { seconds = 30 })
	frames(H, 1)
	contact(H, W, leo, vec(-5, 0, 0))                        -- Leo hits, from the left
	contact(H, W, iota)                                      -- Iota still grinding, recorded last
	eq(H.last_collision.other, "Iota", "the latest record is the ongoing grind")
	frames(H, 1)
	local r = reply(W, 1).result
	eq(r.collision.other, "Leo", "the new contact (cross-family XFK-3)")
	eq(r.collision.direction.right, -1, "its direction")
	eq(call(W, H, 2, "check", { kind = "collision", value = -1 }).result.detail.last.other, "Leo",
	   "check reports it too")
end)

test("an_escape_flight_flies_through_continuing_contact", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	local iota = flyer("Iota", 2)
	contact(H, W, iota); frames(H, 1); contact(H, W, iota)   -- grinding before the flight
	send(W, 1, "pilot", { seconds = 30, forward = -1 })
	frames(H, 1)                                             -- the flight starts mid-contact
	for _ = 1, 4 do contact(H, W, iota); frames(H, 1) end    -- still grinding (QA SLA-1 / CR-1)
	eq(reply(W, 1), nil, "kept flying through the old contact")
	frames(H, 1)                                             -- broke free for a frame
	contact(H, W, iota)                                      -- then touched again: new
	frames(H, 1)
	eq(reply(W, 1).result.stopped_because, "collision", "a new contact stops it")
end)

test("a_flight_after_an_earlier_contact_is_not_stopped_by_it", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	contact(H, W, flyer("Iota", 2))
	frames(H, 3)                                             -- contact over (QA TA-2)
	send(W, 1, "pilot", { seconds = 0.1 })
	frames(H, 1)
	W.mission_time = W.mission_time + 1
	frames(H, 1)
	local r = reply(W, 1).result
	eq(r.stopped_because, "time", "stopped by time"); eq(r.collisions, 0, "no new contacts")
	eq(r.collision, nil, "no contact reported (null)"); eq(r.touching, false, "not touching")
end)

test("pilot_can_keep_flying_through_contacts_and_still_report_them", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	send(W, 1, "pilot", { seconds = 1, stop_on_collision = false })
	frames(H, 2)
	contact(H, W, flyer("Iota", 2))
	frames(H, 2)
	eq(reply(W, 1), nil, "still flying")
	W.mission_time = W.mission_time + 2
	frames(H, 1)
	local r = reply(W, 1).result
	eq(r.stopped_because, "time", "ended by time")
	eq(r.collisions, 1, "the contact is reported (QA CR-5)"); eq(r.collision.other, "Iota", "which")
end)

test("a_contact_that_kills_the_player_reports_player_dead", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	send(W, 1, "pilot", { seconds = 30 })
	frames(H, 1)
	contact(H, W, flyer("Iota", 2))
	W.player.isValid = function() return false end
	frames(H, 1)
	eq(reply(W, 1).result.stopped_because, "player_dead", "death before contact (QA CR-11)")
end)

test("a_contact_on_the_last_tick_is_reported_as_collision", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	send(W, 1, "pilot", { seconds = 0.1 })
	frames(H, 1)
	contact(H, W, flyer("Iota", 2))
	W.mission_time = W.mission_time + 1
	frames(H, 1)
	eq(reply(W, 1).result.stopped_because, "collision", "collision wins the tie")
end)

test("state_shows_contact_and_touching_for_this_run_only", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	local args = { ships = false, goals = false, events = false }
	frames(H, 1)
	contact(H, W, flyer("Iota", 2), vec(1, 0, 0))
	local p = call(W, H, 1, "state", args).result.player
	eq(p.collision.other, "Iota", "shown"); eq(p.collisions_total, 1, "total")
	eq(p.touching, true, "touching")
	p = call(W, H, 2, "state", args).result.player
	eq(p.touching, false, "a contact-free frame")
	W.mission_time = 10 + H.COLLISION_SHOW_SEC               -- the contact was at t = 10
	eq(call(W, H, 3, "state", args).result.player.collision.other, "Iota", "shown at the limit")
	W.mission_time = 10 + H.COLLISION_SHOW_SEC + 0.5
	eq(call(W, H, 4, "state", args).result.player.collision, nil, "expired (null)")
	W.mission_time = 1                                       -- the same mission restarted
	frames(H, 1)
	W.mission_time = 11
	eq(call(W, H, 5, "state", args).result.player.collision, nil, "previous run hidden (QA SLA-3)")
end)

test("check_collision_is_met_past_the_callers_baseline", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	local r = call(W, H, 1, "check", { kind = "collision", value = -1 }).result
	eq(r.met, true, "baseline query"); eq(r.detail.total, 0, "total"); eq(r.detail.errors, 0, "errors")
	eq(call(W, H, 2, "check", { kind = "collision", value = 0 }).result.met, false, "not yet")
	contact(H, W, flyer("Iota", 2))
	r = call(W, H, 3, "check", { kind = "collision", value = 0 }).result
	eq(r.met, true, "met"); eq(r.detail.last.other, "Iota", "detail")
end)

-- Run `n` gameplay frames, each at mission time t0 + i*dt, calling `each(i)` before each one.
local function timed_frames(H, W, n, t0, dt, each)
	for i = 0, n - 1 do
		W.mission_time = t0 + i * dt
		if each then each(i) end
		frames(H, 1)
	end
end

test("telemetry_records_nothing_while_off", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	frames(H, 5)
	eq(H.telem.n, 0, "rows while off")
	local r = call(W, H, 1, "telemetry", { action = "read" }).result
	eq(r.rows, 0, "read while off"); eq(r.on, false, "off"); eq(r.summary, nil, "no summary")
	eq(heartbeat(W).telemetry_on, false, "heartbeat says off")
end)

test("telemetry_read_window_and_downsampling", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	call(W, H, 1, "telemetry", { action = "on" })
	timed_frames(H, W, 20, 21, 1)                            -- rows at t = 21 .. 40
	H.telem.on = false               -- freeze: a read frame would add a row at an unchanged time
	local r = call(W, H, 2, "telemetry", { action = "read", seconds = 5, hz = 10 }).result
	eq(r.summary.frames, 6, "rows from t=35 to 40 (QA TA-4)")
	eq(r.samples[1][1], 35, "oldest row in the window"); eq(#r.samples, 6, "every row at 10 Hz")
	call(W, H, 3, "telemetry", { action = "on" })
	timed_frames(H, W, 17, 50, 0.125)                        -- t = 50 .. 52, exact in binary
	H.telem.on = false
	r = call(W, H, 4, "telemetry", { action = "read", seconds = 120, hz = 2 }).result
	eq(#r.samples, 5, "2 Hz over 2 s: 50, 50.5, 51, 51.5, 52")
	eq(r.samples[2][1], 50.5, "second sample")
	eq(r.summary.fps, 8, "16 intervals over 2 s (QA CR-3)")
end)

test("telemetry_summary_values", function(W)
	local H = boot(W)
	local tgt = stubs.handle({ Position = vec(0, 0, 100) })
	W.player = flyer("Alpha 1", 1, { Target = tgt })
	H.telem.on = true
	local aims = { 2, 4, 2, 4 }
	local iota = flyer("Iota", 2)
	for i = 0, 3 do
		W.mission_time = 30 + i * 0.1
		local a = math.rad(aims[i + 1])
		rawset(tgt, "Position", vec(100 * math.sin(a), 0, 100 * math.cos(a)))
		rawset(W.player.Physics, "RotationalVelocity",
		       vec((i % 2 == 0) and 0.08 or -0.08, (i % 2 == 0) and 0.6 or -0.6, 1.6))
		rawset(W.player.Physics, "Velocity", vec(0, 0, 10 * (i + 1)))
		if i == 2 then contact(H, W, iota) end
		frames(H, 1)
	end
	H.telem.on = false                                       -- freeze the rows before reading
	local s = call(W, H, 1, "telemetry", { action = "read", seconds = 120, hz = 10 }).result.summary
	eq(s.frames, 4, "frames"); eq(s.seconds, 0.3, "seconds"); eq(s.fps, 10, "fps (N-1)/secs")
	eq(s.aim_err_deg.mean, 3, "aim mean"); eq(s.aim_err_deg.min, 2, "aim min")
	eq(s.aim_err_deg.max, 4, "aim max"); eq(s.aim_err_deg.within_3deg_pct, 50, "within 3 deg")
	eq(s.wiggle_per_sec.heading, 10, "3 heading flips at 0.3 of max over 0.3 s")
	eq(s.wiggle_per_sec.pitch, 0, "pitch below the 5% threshold")
	eq(s.wiggle_per_sec.bank, 0, "steady bank")
	eq(s.peak_rate, 0.8, "peak = bank at 0.8 of max"); eq(s.mean_speed, 25, "mean speed")
	eq(s.collisions, 1, "one contact in the window")
end)

test("telemetry_one_row_has_no_rate_figures", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	H.telem.on = true
	frames(H, 1)
	H.telem.on = false
	local r = call(W, H, 1, "telemetry", { action = "read" }).result
	eq(r.summary.frames, 1, "one row")
	eq(r.summary.fps, nil, "fps null (QA CR-3)"); eq(r.summary.wiggle_per_sec, nil, "wiggle null")
	eq(r.samples[1][9], -1, "aim column is -1 without a target")
	eq(r.summary.aim_err_deg, nil, "no aim summary without a target")
end)

test("telemetry_ring_cap_and_new_runs", function(W)
	local H = boot(W)
	H.TELEM_CAP = 5
	W.player = flyer("Alpha 1", 1)
	call(W, H, 1, "telemetry", { action = "on" })
	timed_frames(H, W, 12, 21, 1)                            -- t = 21 .. 32
	eq(H.telem.n, 5, "capped")
	H.telem.on = false
	local r = call(W, H, 2, "telemetry", { action = "read", seconds = 120, hz = 10 }).result
	eq(r.samples[1][1], 28, "oldest kept row after wrap"); eq(r.samples[5][1], 32, "newest")
	eq(r.last_row_t, 32, "last row time"); eq(r.cleared, 0, "never cleared")
	H.telem.on = true
	W.mission_time = 1                                       -- the same mission restarted
	frames(H, 1)
	eq(H.telem.n, 1, "a new run empties it")
	eq(H.telem.cleared, 1, "and counts it")
	has(H.events[#H.events], "telemetry cleared", "event logged (QA SLA-4)")
	W.mission_file = "sm1-02"; W.mission_time = 5            -- another mission, later clock
	frames(H, 1)
	eq(H.telem.n, 1, "another mission empties it too (QA SF-4)"); eq(H.telem.cleared, 2, "count")
	r = call(W, H, 3, "telemetry", { action = "clear" }).result
	eq(r.rows, 0, "clear")
end)

test("a_telemetry_error_switches_it_off_without_blocking_commands", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	send(W, 1, "pilot", { seconds = 1 })
	call(W, H, 2, "telemetry", { action = "on" })
	rawset(W.player.Physics, "RotationalVelocity", "broken")   -- a plain Lua error each frame
	frames(H, 1)
	eq(H.telem.on, false, "switched itself off")
	local r = call(W, H, 3, "telemetry", { action = "off" }).result
	truthy(r.error ~= nil, "off reports the error")
	r = call(W, H, 4, "telemetry", { action = "read" }).result
	truthy(r.error ~= nil, "read reports the error")
	has(H.events[#H.events], "telemetry switched off", "event logged")
	W.mission_time = W.mission_time + 2
	frames(H, 1)
	eq(reply(W, 1).result.stopped_because, "time", "the flight kept ticking (QA SF-2)")
	rawset(W.player.Physics, "RotationalVelocity", vec(0, 0, 0))
	r = call(W, H, 5, "telemetry", { action = "on" }).result
	eq(r.on, true, "on again"); eq(r.error, nil, "error cleared (null)")
	local info = call(W, H, 6, "bridge_info", {}).result
	eq(info.telemetry_on, true, "bridge_info says on")
end)

test("telemetry_guards_neutral_values_and_skipped_frames", function(W)
	local H = boot(W)
	-- Target at our own position, and a ship that cannot turn on any axis (QA TA-9).
	W.player = flyer("Alpha 1", 1, { Target = stubs.handle({ Position = vec(0, 0, 0) }) })
	rawset(W.player.Physics, "RotationalVelocityMax", vec(0, 0, 0))
	rawset(W.player.Physics, "RotationalVelocity", vec(1, 1, 1))
	H.telem.on = true
	W.mission_time = 40; frames(H, 1)
	eq(H.telem.n, 1, "a row")
	local row = H.telem.buf[H.telem.head]
	eq(row[9], 0, "aim 0 at our own position"); eq(row[10], 0, "distance 0")
	eq(row[5], 0, "turn rate 0 when max is 0"); eq(row[7], 0, "bank rate 0")
	W.mission_time = 41; frames(H, 1, true)                   -- a dialog frame
	eq(H.telem.n, 1, "no row on dialog frames")
	W.player.isValid = function() return false end
	W.mission_time = 42; frames(H, 1)
	eq(H.telem.n, 1, "no row without a player ship")
	local r = call(W, H, 1, "telemetry", { action = "read" }).result
	eq(r.now, 42, "now"); eq(r.last_row_t, 40, "last row: the gap shows rows stopped")
	eq(call(W, H, 2, "telemetry", { action = "on" }).result.rows, 0, "on empties the ring")
end)

test("a_telemetry_error_after_rows_leaves_the_ring_intact", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	call(W, H, 1, "telemetry", { action = "on" })
	timed_frames(H, W, 3, 60, 1)                             -- rows at 60, 61, 62
	local n = H.telem.n
	rawset(W.player, "HitpointsMax", "broken")               -- the row fails late, while built
	W.mission_time = 63; frames(H, 1)
	eq(H.telem.on, false, "switched off"); eq(H.telem.n, n, "row count unchanged")
	local r = call(W, H, 2, "telemetry", { action = "read", seconds = 120, hz = 10 }).result
	eq(r.last_row_t, 62, "newest row is the last good one (cross-family XFA-3)")
	eq(#r.samples, n, "every good row readable"); truthy(r.error ~= nil, "error reported")
	truthy(call(W, H, 3, "telemetry", { action = "off" }).result, "off answers")
end)

test("telemetry_off_rows_are_dropped_when_a_new_run_starts", function(W)
	local H = boot(W)
	W.player = flyer("Alpha 1", 1)
	call(W, H, 1, "telemetry", { action = "on" })
	timed_frames(H, W, 3, 70, 1)
	call(W, H, 2, "telemetry", { action = "off" })
	W.mission_time = 2; frames(H, 1)                         -- restart while recording is off
	local r = call(W, H, 3, "telemetry", { action = "read" }).result
	eq(r.rows, 0, "old run's rows not returned (cross-family XFA-5)"); eq(r.cleared, 1, "counted")
end)

test("telemetry_refuses_an_unknown_action", function(W)
	local H = boot(W)
	local r = call(W, H, 1, "telemetry", { action = "start" })
	eq(r.ok, false, "refused")
	has(r.error, "unknown telemetry action", "message")
end)

test("multiplayer_roles_create_a_multiplayer_pilot_and_post_the_join_events", function(W)
	-- Plan Task 6 / probe-results 21.1-21.2: a client needs a multiplayer pilot, the join-screen
	-- event from the main hall, and Accept on the start-game screen.
	W.state = "GS_STATE_INITIAL_PLAYER_SELECT"
	local H = boot(W, { pilot_mode = "create", pilot_name = "Alpha", pilot_multi = true,
	                    multi_role = "client" })
	frames(H, 1)
	eq(W.created[1], "Alpha", "created")
	eq(W.created_multi[1], true, "multi pilot")
	eq(W.selected_multi[1], true, "multi select")
	eq(H.switch_status.multi_join, "pending", "join pending")
	W.state = "GS_STATE_MAIN_MENU"
	frames(H, 1)
	eq(W.posted[#W.posted], "GS_EVENT_MULTI_JOIN_GAME", "join event")
	eq(H.switch_status.multi_join, "done", "join done")
	W.state = "GS_STATE_MULTI_START_GAME"
	frames(H, 1)
	eq(W.posted[#W.posted], "GS_EVENT_MULTI_HOST_SETUP", "accept event")
end)

test("single_player_sessions_still_create_single_player_pilots_and_never_join", function(W)
	W.state = "GS_STATE_INITIAL_PLAYER_SELECT"
	local H = boot(W, { pilot_mode = "create" })
	frames(H, 1)
	eq(W.created_multi[1], false, "single pilot")
	eq(H.switch_status.multi_join, nil, "no join switch")
	W.state = "GS_STATE_MAIN_MENU"
	local before = #W.posted
	frames(H, 1)
	eq(#W.posted, before, "nothing posted")
end)

test("dismiss_popups_leaves_the_joining_netgame_box_alone", function(W)
	-- probe-results 21.1: its only button is Cancel, which abandons the join.
	local H = boot(W, { dismiss_popups = true })
	W.state = "GS_STATE_MULTI_JOIN_GAME"
	W.popup_text = "Joining netgame"
	frames(H, 3, true)
	eq(#W.submits, 0, "the join box is not answered")
	W.popup_text = "Some other box"
	frames(H, 1, true)
	eq(#W.submits, 1, "other boxes still are")
	eq(W.submits[1], 0, "with the first button")
end)

test("the_host_launches_from_the_sync_screen_only_after_the_briefing", function(W)
	W.state = "GS_STATE_MAIN_MENU"
	local H = boot(W, { multi_role = "client", multi_launch = true, skip_briefing = true })
	W.state = "GS_STATE_MULTI_MISSION_SYNC"     -- the mission-load sync, before the briefing
	frames(H, 1)
	eq(W.countdowns, nil, "no launch before the briefing")
	W.state = "GS_STATE_BRIEFING"
	frames(H, 10)
	W.state = "GS_STATE_MULTI_MISSION_SYNC"     -- the post-briefing sync
	frames(H, 1)
	eq(W.countdowns, 1, "launched once")
	frames(H, 5)
	eq(W.countdowns, 1, "not repeated while the state lasts")
end)

test("single_player_never_touches_the_sync_launch", function(W)
	W.state = "GS_STATE_MAIN_MENU"
	local H = boot(W, { skip_briefing = true })
	W.state = "GS_STATE_BRIEFING"
	frames(H, 10)
	W.state = "GS_STATE_MULTI_MISSION_SYNC"
	frames(H, 1)
	eq(W.countdowns, nil, "no launch")
end)

test("sync_launch_fires_only_in_the_session_that_carries_the_mission", function(W)
	-- QA SL-3 / XFA-2: a client that is not the intended host must never press Launch.
	W.state = "GS_STATE_MAIN_MENU"
	local H = boot(W, { multi_role = "client", skip_briefing = true })   -- no multi_launch
	W.state = "GS_STATE_BRIEFING"
	frames(H, 10)
	W.state = "GS_STATE_MULTI_MISSION_SYNC"
	frames(H, 1)
	eq(W.countdowns, nil, "no launch without the switch")
end)

test("sync_launch_latch_resets_for_the_next_mission", function(W)
	-- QA SL-2 / CR-4 / TA-9: the second mission's pre-briefing sync must not launch.
	W.state = "GS_STATE_MAIN_MENU"
	local H = boot(W, { multi_role = "client", multi_launch = true, skip_briefing = true })
	W.state = "GS_STATE_BRIEFING"
	frames(H, 10)
	W.state = "GS_STATE_MULTI_MISSION_SYNC"
	frames(H, 1)
	eq(W.countdowns, 1, "first launch")
	W.state = "GS_STATE_GAME_PLAY"
	frames(H, 5)
	W.state = "GS_STATE_DEBRIEF"
	frames(H, 1)
	W.state = "GS_STATE_MULTI_MISSION_SYNC"      -- mission 2, before its briefing
	frames(H, 1)
	eq(W.countdowns, 1, "no launch before the second briefing")
	W.state = "GS_STATE_BRIEFING"
	frames(H, 10)
	W.state = "GS_STATE_MULTI_MISSION_SYNC"
	frames(H, 1)
	eq(W.countdowns, 2, "second launch after the second briefing")
	-- an abandoned briefing (back to host setup) also clears it
	W.state = "GS_STATE_BRIEFING"
	frames(H, 10)
	W.state = "GS_STATE_MULTI_HOST_SETUP"
	frames(H, 1)
	W.state = "GS_STATE_MULTI_MISSION_SYNC"
	frames(H, 1)
	eq(W.countdowns, 2, "no launch after an abandoned briefing")
end)

test("the_join_event_is_posted_once_so_a_cancel_is_final", function(W)
	-- QA CR-5 / XFK-4: re-entering the main hall must not re-post the join.
	W.state = "GS_STATE_INITIAL_PLAYER_SELECT"
	local H = boot(W, { multi_role = "client" })
	W.state = "GS_STATE_MAIN_MENU"
	frames(H, 1)
	local n = #W.posted
	W.state = "GS_STATE_MULTI_JOIN_GAME"
	frames(H, 1)
	W.state = "GS_STATE_MAIN_MENU"
	frames(H, 1)
	eq(#W.posted, n, "not re-posted")
end)

test("dismiss_popups_leaves_any_joining_popup_alone", function(W)
	-- QA XFK-4: prefix match, so casing or punctuation drift in the engine text cannot turn
	-- the guard into a join-cancel loop.
	local H = boot(W, { dismiss_popups = true })
	W.state = "GS_STATE_MULTI_JOIN_GAME"
	W.popup_text = "Joining netgame..."
	frames(H, 2, true)
	eq(#W.submits, 0, "left alone")
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
