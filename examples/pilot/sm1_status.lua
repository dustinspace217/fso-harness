-- sm1_status.lua: one-line SM1-01 scoreboard for the pilot test. Every field below was checked
-- against this build's scripting.lua (scoring_stats, ship, object) before use: an unknown field on
-- a handle aborts the engine even inside pcall (it killed session s69977995 on 2026-10-15).
local st = ba.getCurrentPlayer().Stats
local function hp(name)
	local s = mn.Ships[name]
	if s and s:isValid() then return math.floor(100 * s.HitpointsLeft / s.HitpointsMax) end
	return -1
end
return string.format("t=%.0f prim %d/%d (ff %d) sec %d/%d (ff %d) kills %d assists %d | Iota %d/%d | Alpha %d | err=%s",
	mn.getMissionTime(), st.MissionPrimaryShotsHit, st.MissionPrimaryShotsFired, st.MissionPrimaryFriendlyHit,
	st.MissionSecondaryShotsHit, st.MissionSecondaryShotsFired, st.MissionSecondaryFriendlyHit,
	st.MissionTotalKills, st.MissionAssists, hp("Iota 1"), hp("Iota 2"), hp("Alpha 1"), tostring(A1 and A1.error))
