-- drill.lua: a repeatable firing range for scripted-pilot work, loaded into pilotdrill.fs2
-- (scratch mod) through the harness `lua` tool: loadfile("<abs path>") then pcall.
--
-- WHY this exists: the 2026-10-12 tuning flights in SM1-01 measured 29-39% primary hit rate for
-- IDENTICAL controller settings; the mission's AI wings varied more than the controller did. A
-- drill removes that variance: the TRACK drill moves a drone along a fixed, scripted path, and the
-- DUEL drill is one AI fighter against the player, repeated, with the player reset between duels.
--
-- This file is TEST SETUP. It creates, moves, heals and removes ships, which a pilot cannot do.
-- The pilot script (pilot.lua) stays player-controls-only; nothing here touches its inputs
-- except resetting the player ship (position, hull, shields, ammo, energy) between drills.
--
-- Results land in DR.res (bounded); read them with a `lua` call: return DR.report().

DR = DR or {}
DR.res = DR.res or {}
local MAXRES = 60                     -- Power-of-Ten rule 3: bounded results list

local function player()
	local p = hv.Player
	if not (p and p:isValid()) then return nil end
	local s = mn.Ships[p.Name]
	if s and s:isValid() then return s end
	return nil
end

-- Mission-scope scoring counters for the current player (fired, hit; primary and secondary).
local function counters()
	local st = ba.getCurrentPlayer().Stats
	return { pf = st.MissionPrimaryShotsFired, ph = st.MissionPrimaryShotsHit,
	         sf = st.MissionSecondaryShotsFired, sh = st.MissionSecondaryShotsHit }
end

local function push(r)
	DR.res[#DR.res + 1] = r
	if #DR.res > MAXRES then table.remove(DR.res, 1) end
end

-- Create ship `name` (class name `cls`) or return an error string. A leftover of the same name is
-- vanished first, and a ship that does not get its name back is vanished again: createShip
-- silently renames when the name is still registered (a ship vanished or dying this frame), and
-- a drill that then looked the name up found the dying old ship and scored the run as over at
-- 0 s (2026-10-15). The "try again" error makes the queue retry the drill on later frames, for up
-- to 60 s (a destroyed cruiser held its name for more than 15 s).
local function create_named(name, cls, orient, pos, team)
	local old = mn.Ships[name]
	if old and old:isValid() then old:vanish() end
	local s = mn.createShip(name, tb.ShipClasses[cls], orient, pos, mn.Teams[team])
	if not (s and s:isValid()) then return "createShip failed for " .. name end
	if s.Name ~= name then
		s:vanish()
		return name .. " came out as " .. s.Name .. " (name still taken); try again next frame"
	end
	return s
end

-- Intercept point for a bolt of speed spd fired from `me` at ship `t`. Duplicated on purpose:
-- the drill's yardstick must not depend on the code it is measuring.
-- WHY the target's ABSOLUTE velocity: retail lasers fly at their table speed along the firing
-- direction and do not inherit the shooter's velocity (weapons.cpp:7401-7414; the additive flag
-- in ai_profiles defaults to NO, def_files/ai_profiles.tbl:243). The 2026-10-12 pilot subtracted
-- its own velocity here, which moves the aim point by own-speed x flight-time (~30 m at 500 m).
local function intercept(me, t, spd)
	local rp = t.Position - me.Position
	local rv = t.Physics.Velocity
	local a = rv:getDotProduct(rv) - spd * spd
	local b = 2 * rp:getDotProduct(rv)
	local c = rp:getDotProduct(rp)
	local tt = -1
	if math.abs(a) > 1e-3 then
		local disc = b * b - 4 * a * c
		if disc >= 0 then
			local r = math.sqrt(disc)
			local t1, t2 = (-b - r) / (2 * a), (-b + r) / (2 * a)
			tt = (t1 > 0 and t2 > 0) and math.min(t1, t2) or math.max(t1, t2)
		end
	end
	if tt <= 0 or tt > 5 then tt = rp:getMagnitude() / spd end
	return t.Position + rv * tt
end

-- Angle in degrees between the player's nose and world point p.
local function nose_err(me, p)
	local d = (p - me.Position):getNormalized()
	local c = d:getDotProduct(me.Orientation:getFvec())
	if c > 1 then c = 1 elseif c < -1 then c = -1 end
	return math.deg(math.acos(c))
end

local function primary_speed(me)
	local b = me.PrimaryBanks[1]
	if b and b:isValid() and b.WeaponClass:isValid() then return b.WeaponClass.Speed end
	return 600
end

-- Remove every weapon in flight (both sides' bolts, missiles and flares). Called between duels:
-- a vanished bandit's missiles otherwise hit the next duel, and our heat seekers re-acquire the
-- next bandit and score hits for shots fired before its baseline (QA XFA-1). Handles are
-- collected first because vanishing while walking mn.Weapons could skip entries.
local function clear_projectiles()
	local list = {}
	for i = 1, #mn.Weapons do
		local w = mn.Weapons[i]
		if w:isValid() then list[#list + 1] = w end
	end
	for _, w in ipairs(list) do
		if w:isValid() then w:vanish() end
	end
end

-- Put the player back at the origin, facing +z, stopped, at full hull, shields, ammo and energy.
local function reset_player(me)
	me.Position = ba.createVector(0, 0, 0)
	me.Orientation = ba.createOrientation()
	me.Physics.Velocity = ba.createVector(0, 0, 0)
	me.HitpointsLeft = me.HitpointsMax
	me.Shields.CombinedLeft = me.Shields.CombinedMax
	-- Rearm: missiles fired in one drill must not starve the next (the v2 track drills spent
	-- every Rockeye on the drone, so the next duels fired none).
	for i = 1, #me.SecondaryBanks do
		local b = me.SecondaryBanks[i]
		if b and b:isValid() then b.AmmoLeft = b.AmmoMax end
	end
	-- And everything else a long duel drains, so each duel starts from the same state (QA TA-6/
	-- SL-6). Not restored: subsystem damage (no single field for it).
	me.WeaponEnergyLeft = me.WeaponEnergyMax
	me.CountermeasuresLeft = me.Class.CountermeasuresMax
	me.AfterburnerFuelLeft = me.AfterburnerFuelMax
	me.Physics.RotationalVelocity = ba.createVector(0, 0, 0)
end

-- ---------------------------------------------------------------- skill level
-- Set the game's skill level, 0 (Very Easy) to 4 (Insane), through FSO's options system: the
-- pilot file stores it as the range option "Game.SkillLevel" (pilotfile/plr.cpp:897-899), and the
-- Lua API offers no other setter (ba.getGameDifficulty only reads it). AI created after the change
-- uses the new level; ships already in the mission may keep values chosen at creation.
-- Returns the level the game reports afterwards (1-5) or an error string.
function DR.set_skill(level)
	for _, o in pairs(opt.Options) do
		if o.Key == "Game.SkillLevel" then
			o.Value = o:getValueFromRange(level / 4)
			o:persistChanges()
			return "difficulty now " .. ba.getGameDifficulty() .. " (1 Very Easy .. 5 Insane)"
		end
	end
	return "no Game.SkillLevel option in this build"
end

-- Change the player's ship class (test setup; the class's default loadout comes with it), via
-- the mission sexp change-ship-class. Returns the class now flown, or an error string.
function DR.set_class(name)
	if not tb.ShipClasses[name]:isValid() then return "unknown ship class: " .. name end
	local me = player()
	if not me then return "no player" end
	mn.runSEXP("( change-ship-class !" .. name .. "! !" .. me.Name .. "! )")
	me = player()
	reset_player(me)
	return "flying " .. me.Class.Name
end

-- ---------------------------------------------------------------- flight-stability metrics
-- Per drill: the longest unbroken stretch with pitch or yaw above 90% of max rate ("stuck spin"
-- if long: a hard turn lasts a few seconds, a controller stuck at full stick much longer), the
-- number of pitch/yaw rate reversals (wiggle: sign flips of a rate above 15% of max), and the
-- player's contacts (ships, debris, asteroids), counted once per contact start.
DR.contacts = DR.contacts or 0
local function stab_new()
	return { spin = 0, spin_max = 0, flips = 0, secs = 0, sy = 0, sp = 0, c0 = DR.contacts,
		lflips = 0, ly = 0, lp = 0, elow = 0 }
end
local function stab_tick(st, me, dt)
	local rv, mx = me.Physics.RotationalVelocity, me.Physics.RotationalVelocityMax
	local fy = (mx.y > 0) and rv.y / mx.y or 0
	local fp = (mx.x > 0) and rv.x / mx.x or 0
	if math.abs(fy) > 0.9 or math.abs(fp) > 0.9 then
		st.spin = st.spin + dt
		if st.spin > st.spin_max then st.spin_max = st.spin end
	else
		st.spin = 0
	end
	local sy = (fy > 0.15) and 1 or ((fy < -0.15) and -1 or 0)
	if sy ~= 0 then
		if st.sy ~= 0 and sy ~= st.sy then st.flips = st.flips + 1 end
		st.sy = sy
	end
	local sp = (fp > 0.15) and 1 or ((fp < -0.15) and -1 or 0)
	if sp ~= 0 then
		if st.sp ~= 0 and sp ~= st.sp then st.flips = st.flips + 1 end
		st.sp = sp
	end
	-- The same count on the line-of-sight rate to the target (r x v_rel / r^2, on our up and right
	-- axes): a pilot following a weaving target must reverse when the target does, so wiggle
	-- minus this baseline is the part that is the controller's own oscillation (the track drone
	-- alone weaves ~0.29 reversals/s; 2026-10-15).
	local t = me.Target
	if t and t:isValid() and t:getBreedName() == "Ship" then
		local r = t.Position - me.Position
		local d2 = r:getMagnitudeSquared()
		if d2 > 1 and d2 < 1500 * 1500 then
			local w = r:getCrossProduct(t.Physics.Velocity - me.Physics.Velocity) / d2
			local o = me.Orientation
			local ly = (mx.y > 0) and w:getDotProduct(o:getUvec()) / mx.y or 0
			local lp = (mx.x > 0) and w:getDotProduct(o:getRvec()) / mx.x or 0
			local s1 = (ly > 0.15) and 1 or ((ly < -0.15) and -1 or 0)
			if s1 ~= 0 then
				if st.ly ~= 0 and s1 ~= st.ly then st.lflips = st.lflips + 1 end
				st.ly = s1
			end
			local s2 = (lp > 0.15) and 1 or ((lp < -0.15) and -1 or 0)
			if s2 ~= 0 then
				if st.lp ~= 0 and s2 ~= st.lp then st.lflips = st.lflips + 1 end
				st.lp = s2
			end
		end
	end
	-- Gun energy under 5% of max: firePrimary() then fires nothing, so a low hit count can be an
	-- empty capacitor rather than bad aim (Myrmidon with Prometheus R, 0.6 energy a shot, 2026-10-15).
	if me.WeaponEnergyMax > 0 and me.WeaponEnergyLeft < 0.05 * me.WeaponEnergyMax then st.elow = st.elow + dt end
	st.secs = st.secs + dt
end
-- Copy the stability numbers into a result row r.
local function stab_into(r, st)
	r.spin_max = st.spin_max
	r.wiggle = (st.secs > 0) and st.flips / st.secs or 0
	r.los_wiggle = (st.secs > 0) and st.lflips / st.secs or 0
	r.energy_low = (st.secs > 0) and st.elow / st.secs or 0
	r.contacts = DR.contacts - st.c0
end

-- ---------------------------------------------------------------- TRACK drill
-- Drone path: a circle of radius R around (0, 0, 1200), tilted 30 degrees about x, with a
-- vertical weave of amplitude W and period 7 s on top. Speed v m/s along the circle.
local function track_pos(cfg, t)
	local w = cfg.speed / cfg.radius
	local a = w * t
	local x, z = cfg.radius * math.cos(a), cfg.radius * math.sin(a)
	local y = cfg.weave * math.sin(2 * math.pi * t / 7)
	local tilt = math.rad(30)
	return ba.createVector(x, y * math.cos(tilt) - z * math.sin(tilt), 1200 + y * math.sin(tilt) + z * math.cos(tilt))
end

-- Start a tracking drill. cfg fields (all optional): secs 60, radius 500, speed 60, weave 120,
-- cls "SF Mara". The drone cannot be destroyed and cannot shoot; it only moves.
function DR.start_track(cfg)
	cfg = cfg or {}
	cfg.secs = cfg.secs or 60
	cfg.radius = cfg.radius or 500
	cfg.speed = cfg.speed or 60
	cfg.weave = cfg.weave or 120
	cfg.cls = cfg.cls or "SF Mara"
	if not tb.ShipClasses[cfg.cls]:isValid() then return "unknown ship class: " .. cfg.cls end
	local me = player()
	if not me then return "no player" end
	reset_player(me)
	local p0 = track_pos(cfg, 0)
	local d = mn.createShip("Drone", tb.ShipClasses[cfg.cls], ba.createOrientation(), p0, mn.Teams["Hostile"])
	if not (d and d:isValid()) then return "createShip failed" end
	mn.runSEXP("( ship-invulnerable !Drone! )")
	mn.runSEXP("( lock-primary-weapon !Drone! )")
	mn.runSEXP("( lock-secondary-weapon !Drone! )")
	DR.cur = { kind = "track", cfg = cfg, t0 = mn.getMissionTime(), c0 = counters(), st = stab_new(),
	           frames = 0, inrange = 0, e1 = 0, e2 = 0, e3 = 0, esum = 0 }
	return "track started"
end

local function tick_track(cur, now, me)
	-- Clock went backwards: the mission restarted under the drill. Its counters now belong to a
	-- different run, so end the drill as aborted instead of scoring it (QA SL-1).
	if now < cur.t0 then
		cur.aborted = true
		return true
	end
	local d = mn.Ships["Drone"]
	if not (d and d:isValid()) then return true end
	local t = now - cur.t0
	local p = track_pos(cur.cfg, t)
	local v = (track_pos(cur.cfg, t + 0.05) - track_pos(cur.cfg, t - 0.05)) * 10
	d.Position = p
	d.Orientation = v:getOrientation()
	d.Physics.Velocity = v
	local spd = primary_speed(me)
	local dist = p:getDistance(me.Position)
	cur.frames = cur.frames + 1
	if dist < 800 then
		local e = nose_err(me, intercept(me, d, spd))
		cur.inrange = cur.inrange + 1
		cur.esum = cur.esum + e
		if e < 1 then cur.e1 = cur.e1 + 1 end
		if e < 2 then cur.e2 = cur.e2 + 1 end
		if e < 3 then cur.e3 = cur.e3 + 1 end
	end
	return t >= cur.cfg.secs
end

local function finish_track(cur, now)
	if cur.aborted then
		push({ kind = "track", label = DR.label, aborted = true })
		return
	end
	local c = counters()
	local n = math.max(cur.inrange, 1)
	local r = { kind = "track", label = DR.label, secs = now - cur.t0,
		frames = cur.frames, inrange_pct = math.floor(100 * cur.inrange / math.max(cur.frames, 1)),
		within1 = math.floor(100 * cur.e1 / n), within2 = math.floor(100 * cur.e2 / n),
		within3 = math.floor(100 * cur.e3 / n), mean_err = cur.esum / n,
		fired = c.pf - cur.c0.pf, hit = c.ph - cur.c0.ph }
	r.hit_pct = (r.fired > 0) and math.floor(100 * r.hit / r.fired) or 0
	stab_into(r, cur.st)
	push(r)
	local d = mn.Ships["Drone"]
	if d and d:isValid() then d:vanish() end
end

-- ---------------------------------------------------------------- DUEL drill
-- n duels in a row against one AI fighter of class cls, spawned 1200 m out (ahead, right, behind,
-- above in turn) and ordered to attack the player. Each duel ends when the bandit dies, the
-- player dies ("died"), the player's hull falls under 15% ("loss", healed), or `limit` seconds pass
-- ("timeout"). The player is reset between duels.
function DR.start_duel(cfg)
	cfg = cfg or {}
	cfg.n = cfg.n or 3
	cfg.cls = cfg.cls or "SF Mara"
	cfg.limit = cfg.limit or 120
	-- A misspelt class would otherwise fail to spawn on every frame (QA XFA-2).
	if not tb.ShipClasses[cfg.cls]:isValid() then return "unknown ship class: " .. cfg.cls end
	DR.cur = { kind = "duel", cfg = cfg, done = 0, seq = 0, next_spawn = mn.getMissionTime() + 1 }
	return "duel started"
end

local function spawn_bandit(cur, me)
	cur.seq = cur.seq + 1
	local name = "Bandit " .. cur.seq
	clear_projectiles()
	reset_player(me)
	-- Rotate the start geometry so no single pass (a head-on joust) decides every duel:
	-- ahead, right, behind, above, facing the player, 1200 m out.
	local starts = { {0, 0, 1}, {1, 0, 0}, {0, 0, -1}, {0, 1, 0} }
	local d = starts[((cur.seq - 1) % #starts) + 1]
	local pos = ba.createVector(d[1] * 1200, d[2] * 1200, d[3] * 1200)
	local s = mn.createShip(name, tb.ShipClasses[cur.cfg.cls], (pos * -1):getOrientation(), pos, mn.Teams["Hostile"])
	if not (s and s:isValid()) then return false end
	s:giveOrder(ORDER_ATTACK, me)
	cur.bandit, cur.t0, cur.c0, cur.st = name, mn.getMissionTime(), counters(), stab_new()
	cur.taken = 0                       -- shield + hull points lost, summed frame to frame
	cur.hp_prev = me.HitpointsLeft + me.Shields.CombinedLeft
	return true
end

-- Record one duel's result. `unknown` = the mission restarted before the death was seen, so the
-- scoring counters were reset and this duel's numbers cannot be recovered (fields left nil).
local function end_duel(cur, now, me, outcome, unknown)
	local r = { kind = "duel", label = DR.label, cls = cur.cfg.cls, outcome = outcome }
	if not unknown then
		local c = counters()
		r.secs = now - cur.t0
		r.hull_left = math.floor(100 * me.HitpointsLeft / me.HitpointsMax)
		r.taken = math.floor(cur.taken)
		r.fired, r.hit = c.pf - cur.c0.pf, c.ph - cur.c0.ph
		r.sfired, r.shit = c.sf - cur.c0.sf, c.sh - cur.c0.sh
		r.hit_pct = (r.fired > 0) and math.floor(100 * r.hit / r.fired) or 0
		stab_into(r, cur.st)
	end
	push(r)
	local s = mn.Ships[cur.bandit]
	if s and s:isValid() and not s:isDying() then s:vanish() end
	clear_projectiles()
	cur.bandit = nil
	cur.done = cur.done + 1
	cur.next_spawn = now + 3
	-- Heal now, not at the next spawn: the vanished bandit's missiles are still flying, and one
	-- killed a 14%-hull player in the 3 s gap, which restarted the mission (2026-10-15). Never
	-- heal a dying ship; the mission restart that follows resets it.
	if outcome ~= "died" then reset_player(me) end
end

local function tick_duel(cur, now, me)
	-- Order matters (QA CR-1/SL-1/TA-5: the first version cleared the duel on a restart before
	-- the death check could see it, so deaths vanished from the results and a spare duel ran).
	-- 1. The clock went backwards with a duel open: a death we did not see restarted the mission.
	if cur.bandit and now < cur.t0 then
		end_duel(cur, now, me, "died", true)
		cur.next_spawn = now + 3
		return false
	end
	-- 2. Count this frame's damage before any verdict, so a death includes its fatal hit (XFA-5).
	if cur.bandit then
		local hp = me.HitpointsLeft + me.Shields.CombinedLeft
		if hp < cur.hp_prev then cur.taken = cur.taken + (cur.hp_prev - hp) end
		cur.hp_prev = hp
	end
	-- 3. The player is dying: score "died" while the scoring counters still belong to this duel.
	if cur.bandit and (me:isDying() or me.HitpointsLeft <= 0) then
		end_duel(cur, now, me, "died")
		return false
	end
	-- 4. After a restart, a spawn time left over from the old clock would never come.
	if cur.next_spawn > now + 10 then cur.next_spawn = now + 1 end
	if not cur.bandit then
		if cur.done >= cur.cfg.n then return true end
		if now >= cur.next_spawn and not me:isDying() and not spawn_bandit(cur, me) then
			DR.error = "createShip failed for " .. cur.cfg.cls     -- end the drill, loudly
			return true
		end
		return false
	end
	local s = mn.Ships[cur.bandit]
	if not (s and s:isValid()) or s:isDying() then
		end_duel(cur, now, me, "kill")
	elseif me.HitpointsLeft < 0.15 * me.HitpointsMax then
		end_duel(cur, now, me, "loss")
	elseif now - cur.t0 > cur.cfg.limit then
		end_duel(cur, now, me, "timeout")
	end
	return false
end

-- ---------------------------------------------------------------- ESCORT drill
-- A friendly freighter ("Convoy") flies straight along +z at cfg.speed (scripted, so the run is
-- repeatable); cfg.waves waves arrive 3 km from it, each cfg.bombers bombers ordered to attack the
-- freighter and cfg.fighters fighters ordered to attack the player. A wave arrives when the
-- previous one is gone or after cfg.wave_limit seconds. The drill ends when the last wave is gone,
-- the freighter dies, or the player dies. The pilot must be told to guard it:
--   A1.cfg.guard = { "Convoy" }  (before loading pilot.lua).
-- Bombs are tracked by signature: one that disappears within the freighter's radius + 100 m of
-- it counts as a hit, any other as stopped (shot down, or ran out of life).
local ESCORT_BEARINGS = { {0, 0, 1}, {1, 0, 0.3}, {-1, 0.2, 0}, {0, -0.3, -1}, {0.7, 0.7, 0} }

function DR.start_escort(cfg)
	cfg = cfg or {}
	cfg.waves = cfg.waves or 3
	cfg.bombers = cfg.bombers or 2
	cfg.fighters = cfg.fighters or 1
	-- Nephilim, the retail bomber of FS2 escort missions. The drill once used the lighter Nahema
	-- because a lone Myrmidon could not kill Nephilims; that made the mission easier instead of
	-- the pilot better, and was reverted (2026-10-15).
	cfg.bomber_cls = cfg.bomber_cls or "SB Nephilim"
	-- cfg.wingmen (default 3) AI fighters "Alpha 2".."Alpha 4" in cfg.wing_cls with that class's
	-- default loadout, as a retail escort mission gives the player a wing. They start with no
	-- orders; giving them one is the pilot's job (A1.cfg.order_guard), as it is a player's.
	cfg.wingmen = cfg.wingmen or 3
	cfg.wing_cls = cfg.wing_cls or "GTF Myrmidon"
	cfg.fighter_cls = cfg.fighter_cls or "SF Mara"
	cfg.freighter_cls = cfg.freighter_cls or "GTFR Poseidon"
	-- createShip gives a class its default loadout, and the Nephilim's carries no bomb (Rockeye,
	-- Piranha, Trebuchet; seen 2026-10-15), so the bombers would never launch one. Arm them.
	cfg.bomb = cfg.bomb or (tb.WeaponClasses["Unknown Bomb"]:isValid() and "Unknown Bomb" or "Cyclops")
	cfg.speed = cfg.speed or 15
	cfg.wave_limit = cfg.wave_limit or 90
	-- Stock hull unless cfg.convoy_hp says otherwise: the pass condition is the one a player
	-- faces, the freighter surviving every wave.
	for _, c in ipairs({ cfg.bomber_cls, cfg.fighter_cls, cfg.freighter_cls, cfg.wing_cls }) do
		if not tb.ShipClasses[c]:isValid() then return "unknown ship class: " .. c end
	end
	if not tb.WeaponClasses[cfg.bomb]:isValid() then return "unknown weapon: " .. cfg.bomb end
	for _, w in ipairs(cfg.loadout or {}) do
		if not tb.WeaponClasses[w]:isValid() then return "unknown weapon: " .. w end
	end
	local me = player()
	if not me then return "no player" end
	-- cfg.loadout = {"Trebuchet", ...}: the secondaries a player would pick for this mission,
	-- one per bank, full. Without it the ship keeps what it has.
	for bi, w in ipairs(cfg.loadout or {}) do
		local sb = me.SecondaryBanks[bi]
		if sb and sb:isValid() then
			sb.WeaponClass = tb.WeaponClasses[w]
			sb.AmmoLeft = sb.AmmoMax
		end
	end
	clear_projectiles()
	reset_player(me)
	-- A dead freighter from the previous run can still hold the name "Convoy" (its explosion
	-- takes seconds), and the new run then read it as already lost (2026-10-15). Remove it first.
	local cv = create_named("Convoy", cfg.freighter_cls, ba.createOrientation(), ba.createVector(0, 0, 600), "Friendly")
	if type(cv) == "string" then return cv end
	if cfg.convoy_hp then
		cv.HitpointsMax = cfg.convoy_hp
		cv.HitpointsLeft = cfg.convoy_hp
	end
	cfg.convoy_max = cv.HitpointsMax
	local wing = {}
	for i = 1, cfg.wingmen do
		local name = "Alpha " .. (i + 1)
		local prev = mn.Ships[name]
		if prev and prev:isValid() then prev:vanish() end
		local w = mn.createShip(name, tb.ShipClasses[cfg.wing_cls], ba.createOrientation(),
			ba.createVector(80 * i * ((i % 2 == 0) and 1 or -1), -40 * i, -60 * i), mn.Teams["Friendly"])
		if w and w:isValid() then wing[#wing + 1] = w.Name end
	end
	DR.cur = { kind = "escort", cfg = cfg, t0 = mn.getMissionTime(), c0 = counters(), st = stab_new(),
		wave = 0, wave_t = -1, wave_ships = {}, all_ships = {}, spawned = { bomber = 0, fighter = 0 },
		bombs = {}, launched = 0, hit = 0, stopped = 0, taken = 0, shots_at = {}, near = {},
		hp_prev = me.HitpointsLeft + me.Shields.CombinedLeft,
		wing = wing, kills = {}, wing_watch = {}, watch_t = -1 }
	return "escort started"
end

local function escort_wave(cur, cv)
	cur.wave = cur.wave + 1
	cur.wave_t = mn.getMissionTime()
	cur.wave_ships = {}
	local b = ESCORT_BEARINGS[((cur.wave - 1) % #ESCORT_BEARINGS) + 1]
	local dir = ba.createVector(b[1], b[2], b[3]):getNormalized()
	local base = cv.Position + dir * 3000
	local side = dir:getCrossProduct(ba.createVector(0, 1, 0))
	if side:getMagnitude() < 0.1 then side = ba.createVector(1, 0, 0) end
	side = side:getNormalized()
	local n = 0
	local me = player()
	for _, role in ipairs({ "bomber", "fighter" }) do
		local count = (role == "bomber") and cur.cfg.bombers or cur.cfg.fighters
		local cls = (role == "bomber") and cur.cfg.bomber_cls or cur.cfg.fighter_cls
		for _ = 1, count do
			n = n + 1
			local name = string.format("W%d %s %d", cur.wave, role, n)
			local pos = base + side * (n * 120)
			local s = mn.createShip(name, tb.ShipClasses[cls], (cv.Position - pos):getOrientation(), pos, mn.Teams["Hostile"])
			if s and s:isValid() then
				if role == "bomber" then
					for bi = 1, #s.SecondaryBanks do
						local sb = s.SecondaryBanks[bi]
						if sb and sb:isValid() then
							sb.WeaponClass = tb.WeaponClasses[cur.cfg.bomb]
							sb.AmmoLeft = sb.AmmoMax
						end
					end
					s:giveOrder(ORDER_ATTACK, cv)
				elseif me then
					s:giveOrder(ORDER_ATTACK, me)
				end
				-- s.Name, not name: createShip silently renames a ship whose name is still registered
				-- (a ship vanished this frame; seen 2026-10-15), and tracking by the asked-for
				-- name then lost the ship.
				cur.wave_ships[#cur.wave_ships + 1] = { name = s.Name, role = role }
				cur.all_ships[#cur.all_ships + 1] = { name = s.Name, role = role }
				cur.spawned[role] = cur.spawned[role] + 1
			end
		end
	end
end

-- Also tallies, by weapon class, every hostile shot that ends within the freighter's radius + 20 m
-- (cur.shots_at[name]): bombs are not the only threat, and with bombs scored alone the freighter
-- died "with 0 bomb hits" and nothing said what killed it (2026-10-15).
local function escort_bombs(cur, cv)
	local now_seen, near = {}, {}
	local me = player()
	for i = 1, #mn.Weapons do
		local w = mn.Weapons[i]
		if w:isValid() and me and w.Team:attacks(me.Team) and not w.Class:isCountermeasure() then
			local sig = w:getSignature()
			local d = w.Position:getDistance(cv.Position)
			if w.Class.Bomb then
				if not cur.bombs[sig] then cur.launched = cur.launched + 1 end
				now_seen[sig] = d
			end
			if d < cv.Radius + 300 then near[sig] = { d = d, name = w.Class.Name } end
		end
	end
	for sig, last in pairs(cur.bombs) do
		if not now_seen[sig] then
			if last < cv.Radius + 100 then cur.hit = cur.hit + 1 else cur.stopped = cur.stopped + 1 end
		end
	end
	cur.bombs = now_seen
	for sig, w in pairs(cur.near or {}) do
		if not near[sig] and w.d < cv.Radius + 20 then
			cur.shots_at[w.name] = (cur.shots_at[w.name] or 0) + 1
		end
	end
	cur.near = near
end

local function finish_escort(cur, now, outcome)
	local me = player()
	local cv = mn.Ships["Convoy"]
	local alive = { bomber = 0, fighter = 0 }
	for _, ws in ipairs(cur.all_ships) do
		local s = mn.Ships[ws.name]
		if s and s:isValid() and not s:isDying() then alive[ws.role] = alive[ws.role] + 1 end
	end
	local r = { kind = "escort", label = DR.label, outcome = outcome, secs = now - cur.t0,
		convoy = (cv and cv:isValid()) and math.floor(100 * cv.HitpointsLeft / cv.HitpointsMax) or 0,
		convoy_lost = (cv and cv:isValid()) and math.floor(cur.cfg.convoy_max - cv.HitpointsLeft) or cur.cfg.convoy_max,
		launched = cur.launched, bomb_hits = cur.hit, stopped = cur.stopped, shots_at = (function()
			local t = {}
			for k, v in pairs(cur.shots_at) do t[#t + 1] = k .. " " .. v end
			table.sort(t)
			return table.concat(t, ", ")
		end)(),
		bombers = string.format("%d/%d", cur.spawned.bomber - alive.bomber, cur.spawned.bomber),
		fighters = string.format("%d/%d", cur.spawned.fighter - alive.fighter, cur.spawned.fighter),
		taken = math.floor(cur.taken),
		hull_left = me and math.floor(100 * me.HitpointsLeft / me.HitpointsMax) or 0 }
	local kt = {}
	for k, v in pairs(cur.kills) do kt[#kt + 1] = k .. " " .. v end
	table.sort(kt)
	r.kills = table.concat(kt, ", ")
	local wt = {}
	for n, st in pairs(cur.wing_watch) do
		local w = mn.Ships[n]
		wt[#wt + 1] = string.format("%s%s: targets bomber %d%% fighter %d%% bomb %d%% none %d%%, mean %.0f m from freighter, %d%% within 1 km",
			n, (w and w:isValid() and not w:isDying()) and "" or " (lost)",
			100 * st.bomber / st.secs, 100 * st.fighter / st.secs, 100 * st.bomb / st.secs,
			100 * st.none / st.secs, st.dist / st.secs, 100 * st.near / st.secs)
	end
	table.sort(wt)
	r.wing = table.concat(wt, "; ")
	stab_into(r, cur.st)
	push(r)
	clear_projectiles()
	for _, n in ipairs(cur.wing) do
		local w = mn.Ships[n]
		if w and w:isValid() and not w:isDying() then w:vanish() end
	end
	for _, ws in ipairs(cur.all_ships) do
		local s = mn.Ships[ws.name]
		if s and s:isValid() and not s:isDying() then s:vanish() end
	end
	if cv and cv:isValid() then cv:vanish() end
end

-- Who killed each ship that dies during an escort run: the killer is the shooter, or the
-- shooter of the weapon that landed the blow. Hook objects arrive typed by breed (ade.cpp
-- ade_object_to_odata), so fields are read only after getBreedName() says which type it is.
if not DR.kill_hooked then
	engine.addHook("On Ship Death Started", function()
		local ok, err = pcall(function()
			local cur = DR.cur
			if not (cur and cur.kind == "escort") then return end
			local dead, k = hv.Ship, hv.Killer
			if not (dead and dead:isValid()) then return end
			local who = "?"
			if k and k:isValid() then
				local b = k:getBreedName()
				if b == "Ship" then who = k.Name
				elseif b == "Weapon" and k.Parent:isValid() and k.Parent:getBreedName() == "Ship" then who = k.Parent.Name end
			end
			local what = dead.Name:match("^W%d+ (%a+)") or dead.Name
			local key = who .. ">" .. what
			cur.kills[key] = (cur.kills[key] or 0) + 1
		end)
		if not ok then DR.error = "kill hook: " .. tostring(err) end
	end)
	DR.kill_hooked = true
end

-- Once a second, note what each wingman is doing: its target's kind (bomber, fighter, bomb,
-- other, none) and its distance from the freighter, summed per wingman, so their guard tactics
-- can be read back and compared with the player's.
local function watch_wing(cur, now, cv)
	if now - cur.watch_t < 1 then return end
	cur.watch_t = now
	local list = { "Alpha 1" }
	for _, n in ipairs(cur.wing) do list[#list + 1] = n end
	for _, n in ipairs(list) do
		local w = mn.Ships[n]
		if w and w:isValid() and not w:isDying() then
			local st = cur.wing_watch[n] or { secs = 0, dist = 0, bomber = 0, fighter = 0, bomb = 0, other = 0, none = 0, near = 0 }
			cur.wing_watch[n] = st
			local t = w.Target
			local kind = "none"
			if t and t:isValid() then
				local b = t:getBreedName()
				if b == "Weapon" then kind = "bomb"
				elseif b == "Ship" and t.Class.Type:isValid() then
					local tn = t.Class.Type.Name
					kind = (tn == "Bomber") and "bomber" or ((tn == "Fighter") and "fighter" or "other")
				else kind = "other" end
			end
			local d = w.Position:getDistance(cv.Position)
			st.secs = st.secs + 1
			st.dist = st.dist + d
			if d < 1000 then st.near = st.near + 1 end
			st[kind] = st[kind] + 1
		end
	end
end

local function tick_escort(cur, now, me)
	if now < cur.t0 then cur.aborted = true; return true end
	local cv = mn.Ships["Convoy"]
	if not (cv and cv:isValid()) or cv:isDying() then finish_escort(cur, now, "convoy lost"); return true end
	if me:isDying() or me.HitpointsLeft <= 0 then finish_escort(cur, now, "died"); return true end
	local hp = me.HitpointsLeft + me.Shields.CombinedLeft
	if hp < cur.hp_prev then cur.taken = cur.taken + (cur.hp_prev - hp) end
	cur.hp_prev = hp
	-- scripted freighter: straight line along +z
	cv.Position = ba.createVector(0, 0, 600 + cur.cfg.speed * (now - cur.t0))
	cv.Orientation = ba.createOrientation()
	cv.Physics.Velocity = ba.createVector(0, 0, cur.cfg.speed)
	escort_bombs(cur, cv)
	watch_wing(cur, now, cv)
	local alive = 0
	for _, ws in ipairs(cur.wave_ships) do
		local s = mn.Ships[ws.name]
		if s and s:isValid() and not s:isDying() then alive = alive + 1 end
	end
	if cur.wave == 0 or alive == 0 or now - cur.wave_t > cur.cfg.wave_limit then
		if cur.wave >= cur.cfg.waves then
			finish_escort(cur, now, (alive == 0) and "all waves done" or "time")
			return true
		end
		escort_wave(cur, cv)
	end
	return false
end

-- ---------------------------------------------------------------- STRIKE drill
-- The player is put in a bomber (cfg.player_cls, every secondary bank loaded with cfg.bomb) and
-- a hostile capital ship ("Strike Target", cfg.target_cls) sits cfg.dist metres ahead, turrets
-- live. Ends when the target dies, the player dies, the bombs run out (plus 20 s for the last
-- ones to land), or cfg.limit seconds pass. The pilot must be told what to bomb:
--   A1.cfg.strike = "Strike Target"  (before loading pilot.lua, or set it any time after).
function DR.start_strike(cfg)
	cfg = cfg or {}
	cfg.player_cls = cfg.player_cls or "GTB Ursa"
	cfg.bomb = cfg.bomb or "Cyclops"
	cfg.target_cls = cfg.target_cls or "GTC Fenris"
	cfg.dist = cfg.dist or 4000
	cfg.limit = cfg.limit or 300
	for _, c in ipairs({ cfg.player_cls, cfg.target_cls }) do
		if not tb.ShipClasses[c]:isValid() then return "unknown ship class: " .. c end
	end
	if not tb.WeaponClasses[cfg.bomb]:isValid() then return "unknown weapon: " .. cfg.bomb end
	local msg = DR.set_class(cfg.player_cls)
	local me = player()
	if not me then return "no player" end
	for i = 1, #me.SecondaryBanks do
		local b = me.SecondaryBanks[i]
		if b and b:isValid() then
			b.WeaponClass = tb.WeaponClasses[cfg.bomb]
			b.AmmoLeft = b.AmmoMax
		end
	end
	clear_projectiles()
	local pos = ba.createVector(0, 0, cfg.dist)
	local t = create_named("Strike Target", cfg.target_cls, ba.createVector(1, 0, 0):getOrientation(), pos, "Hostile")
	if type(t) == "string" then return t end
	DR.cur = { kind = "strike", cfg = cfg, t0 = mn.getMissionTime(), c0 = counters(), st = stab_new(),
		taken = 0, hp_prev = me.HitpointsLeft + me.Shields.CombinedLeft, out_t = nil }
	return msg .. ", strike started"
end

local function secondary_ammo(me)
	local n = 0
	for i = 1, #me.SecondaryBanks do
		local b = me.SecondaryBanks[i]
		if b and b:isValid() then n = n + b.AmmoLeft end
	end
	return n
end

local function finish_strike(cur, now, outcome)
	local me = player()
	local t = mn.Ships["Strike Target"]
	local c = counters()
	local r = { kind = "strike", label = DR.label, outcome = outcome, secs = now - cur.t0,
		target = (t and t:isValid() and not t:isDying()) and math.floor(100 * t.HitpointsLeft / t.HitpointsMax) or 0,
		sfired = c.sf - cur.c0.sf, shit = c.sh - cur.c0.sh, fired = c.pf - cur.c0.pf, hit = c.ph - cur.c0.ph,
		taken = math.floor(cur.taken),
		hull_left = me and math.floor(100 * me.HitpointsLeft / me.HitpointsMax) or 0 }
	stab_into(r, cur.st)
	push(r)
	clear_projectiles()
	if t and t:isValid() and not t:isDying() then t:vanish() end
end

local function tick_strike(cur, now, me)
	if now < cur.t0 then cur.aborted = true; return true end
	local t = mn.Ships["Strike Target"]
	if not (t and t:isValid()) or t:isDying() then finish_strike(cur, now, "target destroyed"); return true end
	if me:isDying() or me.HitpointsLeft <= 0 then finish_strike(cur, now, "died"); return true end
	local hp = me.HitpointsLeft + me.Shields.CombinedLeft
	if hp < cur.hp_prev then cur.taken = cur.taken + (cur.hp_prev - hp) end
	cur.hp_prev = hp
	if secondary_ammo(me) == 0 and not cur.out_t then cur.out_t = now end
	if cur.out_t and now - cur.out_t > 20 then finish_strike(cur, now, "out of bombs"); return true end
	if now - cur.t0 > cur.cfg.limit then finish_strike(cur, now, "timeout"); return true end
	return false
end

-- ---------------------------------------------------------------- RECON drill
-- Four cargo containers ("Cargo 1".."Cargo 4") to scan, spread 1-3 km ahead, and two hostile
-- patrol fighters ("Patrol 1", "Patrol 2") flying scripted circles through the field. Detection
-- is scripted the way FSPort's "Playing Judas" does it: coming within cfg.detect metres of a
-- patrol counts as being seen, and from then on the patrols are AI fighters ordered to attack.
-- Ends when all four are scanned, the player dies, or cfg.limit seconds pass. The pilot needs:
--   A1.cfg.scan = {"Cargo 1","Cargo 2","Cargo 3","Cargo 4"}, A1.cfg.avoid = {"Patrol 1","Patrol 2"},
--   A1.cfg.stealth = true (and optionally A1.cfg.avoid_radius).
local RECON_CARGO = { {-300, 0, 1200}, {500, 200, 1800}, {-600, -200, 2500}, {200, 100, 3100} }

local function patrol_pos(i, t)
	local a = t * 0.08 + (i - 1) * math.pi
	local r = 700
	return ba.createVector(r * math.cos(a), 50 * (i - 1.5), 2100 + r * math.sin(a))
end

function DR.start_recon(cfg)
	cfg = cfg or {}
	cfg.player_cls = cfg.player_cls or "GTF Pegasus"
	cfg.cargo_cls = cfg.cargo_cls or "TC 2"
	cfg.patrol_cls = cfg.patrol_cls or "SF Dragon"
	cfg.detect = cfg.detect or 500
	cfg.limit = cfg.limit or 240
	for _, c in ipairs({ cfg.player_cls, cfg.cargo_cls, cfg.patrol_cls }) do
		if not tb.ShipClasses[c]:isValid() then return "unknown ship class: " .. c end
	end
	local msg = DR.set_class(cfg.player_cls)
	local me = player()
	if not me then return "no player" end
	clear_projectiles()
	for i, c in ipairs(RECON_CARGO) do
		local name = "Cargo " .. i
		local s = create_named(name, cfg.cargo_cls, ba.createOrientation(), ba.createVector(c[1], c[2], c[3]), "Hostile")
		if type(s) == "string" then return s end
		mn.runSEXP("( alter-ship-flag !scannable! ( true ) ( false ) !" .. name .. "! )")
	end
	for i = 1, 2 do
		local name = "Patrol " .. i
		local s = create_named(name, cfg.patrol_cls, ba.createOrientation(), patrol_pos(i, 0), "Hostile")
		if type(s) == "string" then return s end
	end
	DR.cur = { kind = "recon", cfg = cfg, t0 = mn.getMissionTime(), st = stab_new(), taken = 0,
		hp_prev = me.HitpointsLeft + me.Shields.CombinedLeft, detected_t = nil, scanned = {} }
	return msg .. ", recon started"
end

local function recon_scans(cur)
	local n = 0
	for i = 1, #RECON_CARGO do
		local s = mn.Ships["Cargo " .. i]
		if s and s:isValid() and s:getFlag("cargo-known") then
			n = n + 1
			if not cur.scanned[i] then cur.scanned[i] = mn.getMissionTime() - cur.t0 end
		end
	end
	return n
end

local function finish_recon(cur, now, outcome)
	local me = player()
	local times = {}
	for i = 1, #RECON_CARGO do times[#times + 1] = cur.scanned[i] and string.format("%.0f", cur.scanned[i]) or "-" end
	local r = { kind = "recon", label = DR.label, outcome = outcome, secs = now - cur.t0,
		scans = recon_scans(cur), scan_times = table.concat(times, "/"),
		detected = cur.detected_t and string.format("at %.0fs", cur.detected_t) or "never",
		taken = math.floor(cur.taken),
		hull_left = me and math.floor(100 * me.HitpointsLeft / me.HitpointsMax) or 0 }
	stab_into(r, cur.st)
	push(r)
	clear_projectiles()
	for _, n in ipairs({ "Cargo 1", "Cargo 2", "Cargo 3", "Cargo 4", "Patrol 1", "Patrol 2" }) do
		local s = mn.Ships[n]
		if s and s:isValid() and not s:isDying() then s:vanish() end
	end
end

local function tick_recon(cur, now, me)
	if now < cur.t0 then cur.aborted = true; return true end
	if me:isDying() or me.HitpointsLeft <= 0 then finish_recon(cur, now, "died"); return true end
	local hp = me.HitpointsLeft + me.Shields.CombinedLeft
	if hp < cur.hp_prev then cur.taken = cur.taken + (cur.hp_prev - hp) end
	cur.hp_prev = hp
	for i = 1, 2 do
		local p = mn.Ships["Patrol " .. i]
		if p and p:isValid() and not p:isDying() then
			if not cur.detected_t then
				local t = now - cur.t0
				local pos = patrol_pos(i, t)
				local v = (patrol_pos(i, t + 0.05) - patrol_pos(i, t - 0.05)) * 10
				p.Position, p.Orientation, p.Physics.Velocity = pos, v:getOrientation(), v
				if pos:getDistance(me.Position) < cur.cfg.detect then
					cur.detected_t = t
					for j = 1, 2 do
						local q = mn.Ships["Patrol " .. j]
						if q and q:isValid() then q:giveOrder(ORDER_ATTACK, me) end
					end
				end
			end
		end
	end
	if recon_scans(cur) == #RECON_CARGO then finish_recon(cur, now, "all scanned"); return true end
	if now - cur.t0 > cur.cfg.limit then finish_recon(cur, now, "timeout"); return true end
	return false
end

-- ---------------------------------------------------------------- frame hook and report
-- Run several drills back to back: DR.queue_drills({ {"track", {secs = 60}}, {"duel", {n = 4}} }).
-- Each starts when the previous one has finished; the list is bounded by what the caller passes.
DR.queue = DR.queue or {}
function DR.queue_drills(list)
	for _, item in ipairs(list) do DR.queue[#DR.queue + 1] = item end
	return "queued " .. #DR.queue
end
local function start_next()
	local item = table.remove(DR.queue, 1)
	if not item then return end
	-- {"cfg", {label = "...", set = {key = value, ...}}}: relabel and change pilot settings
	-- (A1.cfg) between drills, for alternating A/B comparisons in one queue.
	-- {"class", "GTF Ulysses"}: fly that class from the next drill on (ETS settings carry over,
	-- ship.cpp change_ship_type keeps the recharge indexes).
	if item[1] == "class" then
		local msg = DR.set_class(item[2])
		if msg:sub(1, 6) ~= "flying" then DR.error = msg end
		return
	end
	if item[1] == "cfg" then
		if item[2].label then DR.label = item[2].label end
		if A1 and A1.cfg then
			for k, v in pairs(item[2].set or {}) do A1.cfg[k] = v end
		end
		return
	end
	local starters = { track = DR.start_track, duel = DR.start_duel, escort = DR.start_escort,
		strike = DR.start_strike, recon = DR.start_recon }
	local start = starters[item[1]]
	local msg = start and start(item[2]) or ("unknown drill kind " .. tostring(item[1]))
	local now = mn.getMissionTime()
	item.first_try = item.first_try or now
	if not DR.cur and tostring(msg):find("try again next frame", 1, true) and now - item.first_try < 60 and now >= item.first_try then
		table.insert(DR.queue, 1, item)
	elseif not DR.cur then
		DR.error = "queued drill did not start: " .. tostring(msg)
	end
end

function DR.tick()
	if not DR.cur and #DR.queue > 0 then start_next() end
	local cur = DR.cur
	if not cur then return end
	local me = player()
	if not me then return end
	local now = mn.getMissionTime()
	local dt = now - (DR.last_t or now)
	DR.last_t = now
	local st = (cur.kind ~= "duel") and cur.st or (cur.bandit and cur.st)
	if st and dt > 0 and dt < 0.5 then stab_tick(st, me, dt) end
	if cur.kind == "recon" then
		if tick_recon(cur, now, me) then
			if cur.aborted then push({ kind = "track", label = DR.label, aborted = true }) end
			DR.cur = nil
		end
	elseif cur.kind == "strike" then
		if tick_strike(cur, now, me) then
			if cur.aborted then push({ kind = "track", label = DR.label, aborted = true }) end
			DR.cur = nil
		end
	elseif cur.kind == "escort" then
		if tick_escort(cur, now, me) then
			if cur.aborted then push({ kind = "track", label = DR.label, aborted = true }) end
			DR.cur = nil
		end
	elseif cur.kind == "track" then
		if tick_track(cur, now, me) then finish_track(cur, now); DR.cur = nil end
	elseif cur.kind == "duel" then
		if tick_duel(cur, now, me) then DR.cur = nil end
	end
end

-- One line per result, oldest first; `lua` returns it as text.
function DR.report()
	local out = {}
	for _, r in ipairs(DR.res) do
		if r.kind == "recon" then
			out[#out + 1] = string.format("[%s] recon %s in %.0fs: scanned %d/4 (at %s s), detected %s, took %d dmg, hull %d%% | max-rate %.1fs, wiggle %.2f/s (line of sight %.2f), contacts %d, guns dry %d%%",
				tostring(r.label), r.outcome, r.secs, r.scans, r.scan_times, r.detected, r.taken, r.hull_left, r.spin_max, r.wiggle, r.los_wiggle or 0, r.contacts, 100 * (r.energy_low or 0))
		elseif r.kind == "strike" then
			out[#out + 1] = string.format("[%s] strike %s in %.0fs: target hull %d%%, bombs %d/%d hit, guns %d/%d, took %d dmg, hull %d%% | max-rate %.1fs, wiggle %.2f/s (line of sight %.2f), contacts %d, guns dry %d%%",
				tostring(r.label), r.outcome, r.secs, r.target, r.shit, r.sfired, r.hit, r.fired, r.taken, r.hull_left, r.spin_max, r.wiggle, r.los_wiggle or 0, r.contacts, 100 * (r.energy_low or 0))
		elseif r.kind == "escort" then
			out[#out + 1] = string.format("[%s] escort %s in %.0fs: convoy lost %d hp (%d%% left), bombs %d launched / %d hit / %d stopped, bombers %s, fighters %s killed, took %d dmg, hull %d%% | max-rate %.1fs, wiggle %.2f/s (line of sight %.2f), contacts %d, guns dry %d%% | freighter hit by: %s",
				tostring(r.label), r.outcome, r.secs, r.convoy_lost or -1, r.convoy, r.launched, r.bomb_hits, r.stopped, r.bombers, r.fighters, r.taken, r.hull_left, r.spin_max, r.wiggle, r.los_wiggle or 0, r.contacts, 100 * (r.energy_low or 0), r.shots_at or "?")
				.. "\n    kills: " .. (r.kills or "") .. "\n    wing: " .. (r.wing or "")
		elseif r.aborted then
			out[#out + 1] = string.format("[%s] track ABORTED (mission restarted)", tostring(r.label))
		elseif r.kind == "track" then
			out[#out + 1] = string.format("[%s] track %.0fs: in-range %d%%, nose within 1/2/3 deg %d/%d/%d%%, mean err %.1f, hits %d/%d (%d%%) | max-rate %.1fs, wiggle %.2f/s (line of sight %.2f), contacts %d, guns dry %d%%",
				tostring(r.label), r.secs, r.inrange_pct, r.within1, r.within2, r.within3, r.mean_err, r.hit, r.fired, r.hit_pct, r.spin_max, r.wiggle, r.los_wiggle or 0, r.contacts, 100 * (r.energy_low or 0))
		elseif r.secs == nil then
			out[#out + 1] = string.format("[%s] duel %s %s (numbers lost: mission restarted)", tostring(r.label), r.cls, r.outcome)
		else
			out[#out + 1] = string.format("[%s] duel %s %s in %.0fs: took %d dmg, hull %d%%, prim %d/%d (%d%%), sec %d/%d | max-rate %.1fs, wiggle %.2f/s (line of sight %.2f), contacts %d, guns dry %d%%",
				tostring(r.label), r.cls, r.outcome, r.secs, r.taken, r.hull_left, r.hit, r.fired, r.hit_pct, r.shit, r.sfired, r.spin_max, r.wiggle, r.los_wiggle or 0, r.contacts, 100 * (r.energy_low or 0))
		end
	end
	return table.concat(out, "\n")
end

-- Count the player's contacts for the stability metrics (same filter as the harness bridge:
-- weapon and beam hits also fire this hook; a new contact = the same other object not seen in
-- the last 0.5 s).
DR.seen = DR.seen or {}
function DR.on_contact()
	if hv.Weapon ~= nil or hv.Beam ~= nil then return end
	local p = hv.Player
	if not (p and p:isValid()) then return end
	local sig, a, b = p:getSignature(), hv.Ship, hv.ShipB
	local other
	if a and a:isValid() and a:getSignature() == sig then other = b
	elseif b and b:isValid() and b:getSignature() == sig then other = a
	else return end
	local key = (other and other:isValid()) and other:getSignature() or -1
	local now = mn.getMissionTime()
	local last = DR.seen[key]
	if not last or now - last > 0.5 or now < last then DR.contacts = DR.contacts + 1 end
	if not last then
		DR.seen_n = (DR.seen_n or 0) + 1
		if DR.seen_n > 500 then DR.seen, DR.seen_n = {}, 0 end   -- bounded (Power of Ten rule 3)
	end
	DR.seen[key] = now
end
if not DR.coll_hooked then
	engine.addHook("On Ship Collision", function()
		local ok, err = pcall(DR.on_contact)
		if not ok then DR.error = tostring(err) end
	end)
	DR.coll_hooked = true
end

if not DR.hooked then
	engine.addHook("On Frame", function()
		if not (ba.getCurrentGameState().Name == "GS_STATE_GAME_PLAY") then return end
		local ok, err = pcall(DR.tick)
		if not ok then DR.cur = nil; DR.error = tostring(err) end
	end)
	DR.hooked = true
end
return "drill loaded"
