"""Unit tests for fsoctl/lua_check.py: the pre-send check for agent Lua.

The cases that matter most are the shapes that aborted the engine in the 2026-10-12 demo
(`ba.getControlMode`, `ci:useButtonControl`) and the false-refusal guards (comments, strings,
rebinding, declarations, agent tables, unchecked libraries): a check that refuses good code would
be worse than none. IDs in comments (CR-n, TA-n, MU-n) are the QA findings each test pins.
"""
import json
import os

import pytest

from fsoctl import docs, game, lua_check, session
from fsoctl.errors import EXIT_REFUSED, HarnessError

API = {"version": 2, "elements": [
    {"type": "library", "name": "Base", "shortName": "ba",
     "children": [{"name": "print"}, {"name": "getControlInfo"}, {"name": "GameEvents",
                                                                  "shortName": ""}]},
    {"type": "library", "name": "Mission", "shortName": "mn",
     "children": [{"name": "Ships"}, {"name": "getMissionTime"}]},
    {"type": "library", "name": "Controls", "shortName": "io", "children": [{"name": "getMouseX"}]},
    {"type": "library", "name": "Options", "shortName": "opt", "children": []},
    {"type": "library", "name": "HookVariables", "shortName": "hv", "children": []},
    {"type": "library", "name": "Sub", "shortName": "", "children": [{"name": "x"}]},
    {"type": "class", "name": "ship", "shortName": "ba", "children": [{"name": "Name"}]},
]}


def _p(code):
    """problems_in for one text, with that text's own declarations.

    WHY library_members runs here and not once at import: mutmut picks the tests for a mutant
    by coverage, and code run while the module is imported is credited to no test, so every
    library_members mutant survived the first mutation pass untested.
    """
    bare = lua_check.strip_comments_and_strings(code)
    return lua_check.problems_in(code, bare, lua_check.library_members(API),
                                 lua_check.declared([bare]))


def test_library_members_only_checked_libraries_by_short_and_full_name():
    libs = lua_check.library_members(API)
    assert libs["ba"] == libs["Base"] == {"print", "getControlInfo", "GameEvents"}
    assert set(libs) == {"ba", "Base", "mn", "Mission", "io", "Controls"}   # not opt/hv/"", class


def test_library_members_tolerates_missing_keys():
    assert lua_check.library_members({}) == {}
    assert lua_check.library_members({"elements": [{"type": "library", "name": "Base",
                                                    "shortName": "ba"}]}) == {"ba": set(),
                                                                              "Base": set()}


# ---- refusals ---------------------------------------------------------------------------------

@pytest.mark.parametrize("code, word", [
    ("local m = ba.getControlMode()", "getControlMode"),          # demo crash 2
    ("x = mn.getMisionTime()", "getMisionTime"),                  # typo
    ("if ba . getControlMode then end", "getControlMode"),        # spaces around the dot
    ('ba.print("mode "..ba.getControlMode())', "getControlMode"), # CR-1: after .. concat
    ("x = Base.getControlMode()", "getControlMode"),              # CR-11: full library name
    ('io.write("hi")', "write"),                                  # CR-8: io is FSO's Controls
    ("cfg.ba = 1; ba.getControlMode()", "getControlMode"),        # TA-8: field assign, no rebind
    ("xba = 1; ba.nope()", "nope"),                               # TA-8: longer name, no rebind
    ('x = "abc\nba.nope()', "nope"),                              # TA-11: short string ends at \n
    ("--[==[ x ]==] ba.nope()", "nope"),                          # TA-10: comment closes at ]==]
    ("--[==[x]==]ba.nope()", "nope"),                             # MU: exact end of the comment
    ("ba.print([==[ x ]==]) ba.nope()", "nope"),                  # TA-10: long string closes
    ("ba.print([[x]])ba.nope()", "nope"),                         # MU: exact end of long string
    ('ba.print("a\\"b")ba.nope()', "nope"),                       # escaped quote then code
    ('x = "\\\\" ba.nope()', "nope"),                              # MU: escaped backslash ends
    ('x = "" ba.nope()', "nope"),                                    # MU: empty string
    ("Xa = 1 ba.nope()", "nope"),                                    # MU: only quotes open strings
    ("t[a[1]] = 1 --[[ x ]] ba.nope()", "nope"),                     # MU: earlier ]] ignored
    ("t[a[1]] = [[x]] ba.nope()", "nope"),                           # MU: same, long string
    ("--[[a]] ba.nope() --[[b]]", "nope"),                           # MU: first close, not last
    ("x = [[a]] ba.nope() y = [[b]]", "nope"),                       # MU: same, long string
    ("ba.print('x') -- note\nba.nope()", "nope"),                 # MU: comment not at offset 0
])
def test_unknown_library_member_is_refused(code, word):
    found = _p(code)
    assert len(found) == 1 and word in found[0] and '["' in found[0]   # hint offers lib["x"]


@pytest.mark.parametrize("call", ["accessButtonInfo", "useButtonControl", "getButtonControlName",
                                  "getButtonControlNumber"])
def test_colon_call_on_no_self_function_is_refused(call):
    found = _p(f'ci:{call}(-1, "SHIELD_EQUALIZE")')
    assert len(found) == 1 and "dot" in found[0]


@pytest.mark.parametrize("code", ['ci : useButtonControl (1)',            # TA-12: spaced
                                  'ci:getButtonControlNumber"X"',         # CR-11: string call
                                  "ci:getButtonControlNumber[[X]]", "ci:accessButtonInfo{}"])
def test_colon_call_forms_are_refused(code):
    assert len(_p(code)) == 1


def test_same_mistake_twice_is_reported_once():
    assert len(_p("ba.nope() ba.nope()")) == 1


# ---- code that must pass --------------------------------------------------------------------

@pytest.mark.parametrize("code", [
    'ci.useButtonControl(-1, "SHIELD_EQUALIZE")',    # the correct form
    "local t = mn.getMissionTime(); ba.print(t)",
    "-- ba.getControlMode() in a comment",
    "x = 1 -- ba.nope() later on the line",
    "--[[ ba.getControlMode()\n ci:useButtonControl(1) ]] ba.print(1)",
    "--[==[ ba.nope ]] still comment ]==] ba.print(1)",
    'ba.print("ba.getControlMode() ci:useButtonControl(")',
    "ba.print('it''s ba.nope')",
    "ba.print([[ ba.nope ]]) ba.print([=[ ]] ba.nope ]=])",
    'ba.print("esc \\" ba.nope")',
    "local ba = {}; ba.anything = 1",                # rebound locally
    "local ba\nif x then ba.anything() end",         # TA-9: bare local declaration
    "local a, ba = 1, {}; ba.anything = 1",          # second name in a local list
    "ba = {}; ba.anything = 1",                      # rebound globally
    "local function f(ba) return ba.anything end",   # parameter
    "for k, ba in pairs(t) do x = ba.anything end",  # loop variable
    "function mn.helper() end mn.helper()",          # CR-2: declared on the library table
    "mn.cache = {}; x = mn.cache",                   # CR-2: assigned, then read
    "x = f().ba.nope; y = t[1].ba.nope; z = q.ba.nope",   # TA-7: fields of other things
    "x = hv.Player; y = opt.whatever; z = ts.x",     # not checked libraries
    "A1.fly = fly; A1.log = {}",                     # agent tables are never checked
    "s:firePrimary(); ci:getSomething(); ci:useButtonControlX(1)",   # TA-12: other colon calls
    "x = Base.print",                                # full name, real member
    "ci:useButtonControl",                           # a reference, not a call
])
def test_clean_code_passes(code):
    assert _p(code) == []


def test_comparison_is_not_a_rebinding():
    # `ba == x` must not count as `ba = ...`, or every compare would switch the check off.
    assert _p("if ba == nil then end ba.nope()")


def test_strip_keeps_offsets_and_newlines():
    code = 'a = "x\ny"\n-- c\nb = [[\nz]]'
    out = lua_check.strip_comments_and_strings(code)
    assert len(out) == len(code) and out.count("\n") == code.count("\n")
    assert "x" not in out and "z" not in out and "a =" in out and "b =" in out


@pytest.mark.parametrize("code", ['ba.print("open ba.nope', "--[[ open ba.nope",
                                  "ba.print([[ open ba.nope", "ba.print('open\\"])
def test_unterminated_string_and_comment_run_to_the_end(code):
    out = lua_check.strip_comments_and_strings(code)
    assert len(out) == len(code) and "open" not in out
    assert _p(code) == []


# ---- check(): the API file and loaded files ---------------------------------------------------

def _api_file(tmp_path):
    p = tmp_path / "scripting.json"
    p.write_text(json.dumps(API))
    return p


def _write(path, text):
    path.write_text(text)
    return path


def test_check_follows_loadfile_one_level(tmp_path):
    deeper = _write(tmp_path / "deeper.lua", "ba.deep_nope()")
    inner = _write(tmp_path / "pilot.lua", f'ci:useButtonControl(-1, "X")\nloadfile("{deeper}")')
    found, unchecked = lua_check.check(f'local f = loadfile("{inner}"); f()', _api_file(tmp_path))
    assert len(found) == 1 and str(inner) in found[0] and unchecked == []   # TA-4: not deeper


def test_dofile_is_followed(tmp_path):
    bad = _write(tmp_path / "bad.lua", "ba.nope()")
    found, _ = lua_check.check(f'dofile("{bad}")', _api_file(tmp_path))
    assert len(found) == 1 and str(bad) in found[0]                       # TA-5


def test_declaration_in_a_loaded_file_counts(tmp_path):
    lib = _write(tmp_path / "lib.lua", "function mn.helper() end")
    found, _ = lua_check.check(f'dofile("{lib}") mn.helper()', _api_file(tmp_path))
    assert found == []                                                    # CR-2 across files


def test_commented_out_load_is_not_followed(tmp_path):
    bad = _write(tmp_path / "bad.lua", "ba.nope()")
    found, unchecked = lua_check.check(f'-- loadfile("{bad}")', _api_file(tmp_path))
    assert found == [] and unchecked == []                                # TA-6 / CR-6


def test_relative_load_is_reported_unchecked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "rel.lua", "ba.nope()")
    found, unchecked = lua_check.check('dofile("rel.lua")', _api_file(tmp_path))
    assert found == [] and unchecked == ["rel.lua"]                       # TA-3 / CR-7


def test_missing_absolute_load_is_reported_unchecked(tmp_path):
    found, unchecked = lua_check.check('loadfile("/no/such/file.lua")', _api_file(tmp_path))
    assert found == [] and unchecked == ["/no/such/file.lua"]


def test_size_cap_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(lua_check, "_FILE_MAX_BYTES", 4)
    at = _write(tmp_path / "at.lua", "ba.x")                 # exactly 4 bytes, all of them read
    over = _write(tmp_path / "over.lua", "ba.x ")            # 5 bytes: not checked
    found, unchecked = lua_check.check(f'dofile("{at}") dofile("{over}")', _api_file(tmp_path))
    assert len(found) == 1 and unchecked == [str(over)]                   # TA-13


def test_special_file_is_not_read(tmp_path):
    found, unchecked = lua_check.check('loadfile("/dev/zero")', _api_file(tmp_path))
    assert found == [] and unchecked == ["/dev/zero"]                     # CR-6: no hang


def test_non_utf8_file_does_not_raise(tmp_path):
    bad = tmp_path / "bin.lua"
    bad.write_bytes(b"\xff\xfe ba.nope()")
    found, _ = lua_check.check(f'dofile("{bad}")', _api_file(tmp_path))
    assert len(found) == 1                                                # TA-2


def test_repeated_load_is_read_once_and_file_count_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(lua_check, "_FILES_MAX", 2)
    files = [_write(tmp_path / f"f{i}.lua", f"ba.nope{i}()") for i in range(3)]
    code = " ".join(f'dofile("{f}")' for f in [files[0]] * 5 + files)
    found, unchecked = lua_check.check(code, _api_file(tmp_path))
    assert len(found) == 2 and unchecked == [str(files[2])]               # CR-6


def test_report_is_bounded(tmp_path):
    code = " ".join(f"ba.nope{i}()" for i in range(30))
    found, _ = lua_check.check(code, _api_file(tmp_path))
    assert len(found) == lua_check._REPORT_MAX + 1 and found[-1] == "... and 20 more"   # CR-9


# ---- the verb: refuse before sending, skip (and say why) otherwise --------------------------

@pytest.fixture
def evalsess(tmp_runtime, monkeypatch):
    """A running game session with eval allowed; the bridge call is replaced by a recorder."""
    s = session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[],
                            mission=None, renderer="opengl", width=1, height=1, switches={},
                            allow_eval=True, extra_args=[])
    monkeypatch.setattr(game.launcher, "require_running", lambda sid: s)
    monkeypatch.setattr(game.launcher, "alive", lambda sess: True)
    sent = []
    monkeypatch.setattr(game, "call", lambda sid, cmd, args=None, timeout=10.0:
                        sent.append((cmd, args)) or {"value": "ok"})
    return s, sent


@pytest.fixture
def with_cache(tmp_path, monkeypatch):
    _api_file(tmp_path)
    monkeypatch.setattr(docs, "cached_reference", lambda binary, mods: tmp_path)
    return tmp_path


def test_verb_refuses_before_sending(evalsess, with_cache):
    s, sent = evalsess
    with pytest.raises(HarnessError) as e:
        game.lua(s.id, "return ba.getControlMode()")
    assert e.value.code == EXIT_REFUSED and "getControlMode" in e.value.message
    assert "scripting.json" in e.value.hint and sent == []                # TA-14 / CR-10


def test_verb_refuses_for_a_loaded_file(evalsess, with_cache):
    s, sent = evalsess
    bad = _write(with_cache / "ctl.lua", 'ci:useButtonControl(-1, "X")')
    with pytest.raises(HarnessError) as e:
        game.lua(s.id, f'local f = loadfile("{bad}"); f()')
    assert str(bad) in e.value.message and sent == []                     # TA-14


def test_verb_passes_clean_code_and_reports_it(evalsess, with_cache):
    s, sent = evalsess
    reply = game.lua(s.id, "return mn.getMissionTime()")
    assert reply["lua_check"] == "passed"
    assert sent == [("lua", {"code": "return mn.getMissionTime()"})]


def test_verb_reports_files_it_could_not_check(evalsess, with_cache):
    s, sent = evalsess
    reply = game.lua(s.id, 'local f = loadfile("/no/such.lua")')
    assert reply["lua_check"] == "passed (not checked: /no/such.lua)" and len(sent) == 1   # CR-7


def test_verb_without_cache_sends_and_says_skipped(evalsess, monkeypatch):
    s, sent = evalsess
    monkeypatch.setattr(docs, "cached_reference", lambda binary, mods: None)
    reply = game.lua(s.id, "return ba.getControlMode()")
    assert reply["lua_check"] == "skipped: no api_docs cache for this build" and len(sent) == 1


def test_verb_lookup_failure_sends_and_says_why(evalsess, monkeypatch):
    s, sent = evalsess
    monkeypatch.delenv("FS2PATH", raising=False)
    reply = game.lua(s.id, "return 1")
    assert reply["lua_check"].startswith("skipped: check error: FS2PATH") and len(sent) == 1


@pytest.mark.parametrize("exc", [OSError("binary being rebuilt"), ValueError("bad json")])
def test_verb_check_failure_sends_and_says_why(evalsess, with_cache, monkeypatch, exc):
    s, sent = evalsess
    monkeypatch.setattr(lua_check, "check", lambda *a: (_ for _ in ()).throw(exc))
    reply = game.lua(s.id, "return 1")
    assert reply["lua_check"] == f"skipped: check error: {exc}" and len(sent) == 1   # CR-3


# ---- docs.cached_reference ----------------------------------------------------------------

def _cache_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(docs.paths, "fs2path", lambda: tmp_path)
    monkeypatch.setattr(docs, "_generate", lambda *a: pytest.fail("must not generate"))
    exe = _write(tmp_path / "engine", "x")
    return str(exe)


def _fill(dest, sizes_off=0):
    dest.mkdir(parents=True)
    sizes = {}
    for name in docs.FILES:
        (dest / name).write_text(name)
        sizes[name] = len(name) + sizes_off
    (dest / docs.MANIFEST).write_text(json.dumps({"files": sizes}))


def test_cached_reference_returns_a_valid_cache(tmp_path, monkeypatch):
    exe = _cache_env(tmp_path, monkeypatch)
    dest = docs.cache_dir(exe, [])
    _fill(dest)
    assert docs.cached_reference(exe, []) == dest                         # TA-1


def test_cached_reference_rejects_a_damaged_cache_and_never_generates(tmp_path, monkeypatch):
    exe = _cache_env(tmp_path, monkeypatch)
    assert docs.cached_reference(exe, []) is None
    _fill(docs.cache_dir(exe, []), sizes_off=1)
    assert docs.cached_reference(exe, []) is None
    assert not os.path.exists(tmp_path / "cache" / "fso-harness" / "docs" / "lock")


def test_messages_for_the_code_itself_have_no_file_prefix(tmp_path):
    found, _ = lua_check.check("ba.nope()", _api_file(tmp_path))
    assert found == ['ba.nope: this build\'s \'ba\' library has no \'nope\' '
                     '(if you add it yourself, write ba["nope"])']          # MU check_7


def test_report_at_exactly_the_cap_has_no_count_line(tmp_path):
    code = " ".join(f"ba.nope{i}()" for i in range(lua_check._REPORT_MAX))
    found, _ = lua_check.check(code, _api_file(tmp_path))
    assert len(found) == lua_check._REPORT_MAX and "more" not in found[-1]  # MU check_47
