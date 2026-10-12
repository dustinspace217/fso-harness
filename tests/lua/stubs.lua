-- Stand-ins for the engine's Lua libraries (cf, mn, ba, hv, ui and a few globals), so the bridge
-- can run under plain luajit (QA finding TA-10: the bridge had no unit tier). Each test builds a
-- fresh world with stubs.install(), loads a fresh copy of the bridge, then drives H.frame().
--
-- Fidelity notes, each taken from the engine source (fs2open code/scripting/api):
--   * cf.renameFile returns true on FAILURE and false on success (cfile.cpp returns
--     cf_rename(...) != 0, and CF_RENAME_SUCCESS is 0; probed on the engine 2026-10-12). The stub
--     copies that, so bridge code that trusts the return value fails here as it would there.
--   * The name indexers (mn.Events, mn.Wings, mn.ShipRegistry, mn.Goals) fall back to atoi(name)
--     as a 1-based index, and return an invalid handle for anything else (mission.cpp).
--   * The global error() is the engine's fatal ba.error (forwarders.lua). The stub records any
--     call in world.fatal so a test fails if the bridge ever calls it.
--   * Lists are userdata (newproxy) because Lua 5.1 tables ignore __len, and the bridge uses #.
--   * Reading a field a handle does not have is an engine-raised error, which pcall cannot catch
--     under -noninteractive (probe results; CLAUDE.md). Stub handles are strict: such a read
--     records world.fatal, so the test fails even when the bridge's pcall swallows the error
--     (QA finding TA2-2).
local M = {}

local real_error = error

-- Make `t` a strict handle: reading a key it does not hold is fatal (see the note above). Keys
-- passed to M.unset read as nil instead, for fields the engine reports as nil.
local function strict(t, what)
	local nils = {}
	return setmetatable(t, { nils = nils, __index = function(_, k)
		if nils[k] then return nil end
		local msg = "read missing field " .. tostring(k) .. " on " .. what
		if M.world and not M.world.fatal then M.world.fatal = msg end
		real_error(msg, 2)
	end })
end

-- Give handle `h` a field that exists but reads nil (e.g. an unreadable value).
function M.unset(h, key)
	rawset(h, key, nil)
	getmetatable(h).nils[key] = true
end

-- Return an invalid handle: isValid() false and nothing else.
local function invalid()
	return strict({ isValid = function() return false end }, "an invalid handle")
end

-- Wrap a plain table as a valid handle.
function M.handle(t)
	t.isValid = function() return true end
	return strict(t, "handle " .. tostring(rawget(t, "Name")))
end

-- An engine-style list: indexable by 1-based number or by name (case-insensitive), with #.
-- by_name=false gives a list that only takes numbers (mn.Ships by number, for example).
function M.list(items, by_name)
	local p = newproxy(true)
	local mt = getmetatable(p)
	mt.__len = function() return #items end
	mt.__index = function(_, key)
		if by_name ~= false and type(key) == "string" then
			for _, it in ipairs(items) do
				if string.lower(it.Name) == string.lower(key) then return it end
			end
			key = tonumber(key) or 0   -- the engine's atoi fallback
		end
		return items[key] or invalid()
	end
	return p
end

-- A ship handle with the fields ship_entry reads.
function M.ship(name, extra)
	local s = M.handle({
		Name = name, HitpointsMax = 100, HitpointsLeft = 50,
		Shields = { CombinedMax = 0, CombinedLeft = 0 },
		Class = { Name = "GTF Ulysses" }, Team = { Name = "Friendly" },
		Position = { x = 1, y = 2, z = 3 }, Orders = {}, Target = invalid(),
		getfvec = function() return { x = 0, y = 0, z = 1 } end,
		isDying = function() return false end,
		firePrimary = function() return 1 end,
		fireSecondary = function() return 0 end,
		fireCountermeasure = function() end,
	})
	for k, v in pairs(extra or {}) do s[k] = v end
	return s
end

-- An in-memory folder for one cf path type.
local function folder()
	return {}
end

-- The extensions the engine indexes per path type (cfile.cpp Pathtypes[]): cf cannot open a file
-- with any other extension through that type. Modelled so a test catches a file placed where the
-- engine will never see it (the version file once sat in data/scripts as .txt and read "unknown").
local INDEXED = {
	["data/scripts"] = { ".lua", ".lc", ".fnl" },
	["data/text"] = { ".txt", ".net" },
}

local function indexed(name, path)
	local exts = INDEXED[path]
	if not exts then return true end   -- types not modelled here accept any name
	for _, ext in ipairs(exts) do
		if name:sub(-#ext):lower() == ext then return true end
	end
	return false
end

-- Install fresh globals and return the world table tests inspect and change.
function M.install()
	local W = {
		files = { ["data/cache"] = folder(), ["data/scripts"] = folder(), ["data/text"] = folder() },
		state = "GS_STATE_GAME_PLAY", in_mission = true, mission_time = 10,
		sexps = {}, sexp_results = {}, runs = {}, posted = {}, printed = {},
		control_mode = "NORMAL", submits = {}, fatal = nil,
		frametime_overall = 100, mission_frametime = 0.02, real_frametime = 0.02, listed = {},
	}
	M.world = W
	local function dir(path)
		local d = W.files[path]
		if not d then real_error("stub cf: unknown path " .. tostring(path)) end
		return d
	end

	cf = {
		openFile = function(name, mode, path)
			local d = dir(path)
			if mode == "r" then
				if d[name] == nil or not indexed(name, path) then return invalid() end
				local text = d[name]
				return M.handle({ read = function() return text end, close = function() end })
			end
			local buf = {}
			return M.handle({
				write = function(_, s) buf[#buf + 1] = s end,
				close = function() d[name] = table.concat(buf) end,
			})
		end,
		fileExists = function(name, path) return dir(path)[name] ~= nil end,
		deleteFile = function(name, path)
			local d = dir(path)
			local had = d[name] ~= nil
			d[name] = nil
			return had
		end,
		renameFile = function(old, new, path)
			local d = dir(path)
			if d[old] == nil then return true end   -- engine: failure reads as true
			d[new], d[old] = d[old], nil
			return false                             -- engine: success reads as false
		end,
		-- The engine checks that the filter starts with * and matches folder files with
		-- fnmatch(filter, name) (cfilesystem.cpp cf_get_list_of_files); W.listed records each result.
		listFiles = function(path, filter)
			if string.sub(filter, 1, 1) ~= "*" then real_error("stub cf: filter must start with *") end
			local pat = "^" .. string.gsub(filter, "[%^%$%(%)%%%.%[%]%+%-%?%*]", function(c)
				if c == "*" then return ".*" end
				if c == "?" then return "." end
				return "%" .. c
			end) .. "$"
			local out = {}
			for name in pairs(dir(path)) do
				if string.match(name, pat) then out[#out + 1] = name end
			end
			W.listed[#W.listed + 1] = out
			return out
		end,
	}

	FLIGHT_CONTROL_NORMAL, FLIGHT_CONTROL_LUA_FULL = "NORMAL", "FULL"
	COMMIT_SUCCESS, COMMIT_FAIL = "COMMIT_SUCCESS", "COMMIT_FAIL"

	ba = {
		getCurrentGameState = function() return M.handle({ Name = W.state }) end,
		GameEvents = setmetatable({}, { __index = function(_, k)
			if string.match(k, "^GS_EVENT_") then return M.handle({ Name = k }) end
			return invalid()
		end }),
		postGameEvent = function(ev) W.posted[#W.posted + 1] = ev.Name return true end,
		setControlMode = function(mode)
			if mode ~= nil then W.control_mode = mode end
			return W.control_mode
		end,
		getControlInfo = function() W.controls = {} return W.controls end,
		println = function(s) W.printed[#W.printed + 1] = s end,
		getFrametimeOverall = function() return W.frametime_overall end,
		getMissionFrametime = function() return W.mission_frametime end,
		getRealFrametime = function() return W.real_frametime end,
	}

	-- Sexp stubs record every text they are given; the result comes from W.sexp_results[text]
	-- (default false / 0), or from W.sexp_fn(text) when a test sets one.
	local function sexp_result(text, default)
		W.sexps[#W.sexps + 1] = text
		if W.sexp_fn then return W.sexp_fn(text) end
		local r = W.sexp_results[text]
		if r == nil then return default end
		return r
	end
	W.events = { M.handle({ Name = "Arrival" }), M.handle({ Name = "Destroy Iota" }) }
	W.goals = { M.handle({ Name = "Protect Iota", isGoalSatisfied = 2 }) }
	W.ships = { M.ship("Alpha 1"), M.ship("Iota") }
	W.registry = { M.handle({ Name = "Alpha 1" }), M.handle({ Name = "Iota" }),
	               M.handle({ Name = "Gone 1" }) }
	W.wings = { M.handle({ Name = "Alpha" }) }
	mn = {
		isInMission = function() return W.in_mission end,
		getMissionTime = function() return W.mission_time end,
		getMissionFilename = function() return "sm1-01" end,
		evaluateSEXP = function(t) return sexp_result(t, false) end,
		evaluateNumericSEXP = function(t) return sexp_result(t, 0) end,
		runSEXP = function(t) W.runs[#W.runs + 1] = t return sexp_result(t, true) end,
	}
	-- Lists read W's tables at the moment they are indexed, so tests can change them later.
	setmetatable(mn, { __index = function(_, k)
		if k == "Events" then return M.list(W.events) end
		if k == "Goals" then return M.list(W.goals) end
		if k == "Ships" then return M.list(W.ships) end
		if k == "ShipRegistry" then return M.list(W.registry) end
		if k == "Wings" then return M.list(W.wings) end
	end })

	W.player = W.ships[1]
	hv = setmetatable({ Submit = function(c) W.submits[#W.submits + 1] = c end }, {
		__index = function(_, k)
			if k == "Player" then return W.player end
			if k == "Text" then return W.popup_text end
		end })

	W.created, W.selected = {}, {}
	ui = {
		PilotSelect = {
			createPilot = function(name) W.created[#W.created + 1] = name return true end,
			selectPilot = function(name) W.selected[#W.selected + 1] = name end,
			WarningCount = 0, ErrorCount = 0,
		},
		Briefing = { commitToMission = function() W.committed = true return COMMIT_SUCCESS end },
	}

	-- The engine's error() is fatal; record it and stop the frame like the engine would.
	error = function(m)
		W.fatal = tostring(m)
		real_error("FATAL error() called: " .. tostring(m))
	end
	return W
end

-- Undo install()'s error override (the runner calls this between tests).
function M.restore()
	error = real_error
end

M.real_error = real_error
return M
