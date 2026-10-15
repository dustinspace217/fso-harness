"""Pre-send check for agent Lua: refuse the API mistakes that kill the engine.

WHY this exists: in FSO an API mistake in Lua (an index the API lacks, an argument of the wrong
type) throws os::dialogs::LuaErrorException, a C++ exception that `pcall` cannot catch; under
-noninteractive it ends the whole engine (probe-results section 19, observed three times in the
2026-10-12 demo). Two of those three were visible in the code text before it was ever sent:
`ba.getControlMode` (no such function) and `ci:useButtonControl(...)` (a colon call on a function
whose binding takes no self). This module catches those two shapes. It cannot see handle TYPES,
so `.Name` read on a weapon handle still gets through; the MCP instructions keep that rule.

What it checks, against this build's own scripting.json (the api_docs cache):
  1. `<lib>.<name>` for the libraries in _CHECKED_LIBS (short or full name, e.g. ba / Base) where
     <name> is neither a member of that library nor declared by the code itself (`lib.name = ...`
     or `function lib.name`): FSO library tables accept new keys (scripting/ade.cpp, index STEP 1
     reads user keys first, STEP 6 stores new ones), so that is valid Lua. Skipped for a library
     name the code rebinds (`local ba`, `ba =`, a parameter, a loop variable).
  2. Colon calls (with parentheses or the string/table call forms) on the control_info functions
     whose C++ bindings parse no self argument.
  3. Handle members that exist nowhere: `x.Field` with a capitalised name (FSO properties are
     PascalCase) and `x:method(...)` calls, where the name is a member of no class or library in
     this build's API, is not defined by the code (assignment, table-constructor key, `function
     x:name`), and is not a Lua string method. A misspelt field aborted the engine
     (`Stats.MissionPrimaryFriendlyHits`, 2026-10-15) and an invented method raised a Lua error
     (`ship:isWarpingOut`). Limit: a real name on the wrong handle type still passes (`Linked`
     is on weaponbanktype, not weaponbank, and reading it on a bank aborted the engine); that
     would need the handle's type, which the text does not give.
All run on the code with comments and string literals blanked, so text inside them never
matches. Files the code loads with loadfile("/abs/path") / dofile("/abs/path") in code (not in a
comment) are checked too, one level deep; the files that could not be checked are reported back
so a "passed" never overstates what was looked at.
"""
import json
import os
import re
import stat
from pathlib import Path

# Libraries the check applies to. WHY an allowlist and not "every library": a library table can be
# extended by user code, and short names such as opt, ts, gl, ui, parse or async are also common
# local variable names, so checking them would refuse valid code (QA CR-2). These are the
# libraries agents call constantly and whose names are not ordinary words. io is FSO's Controls
# library, installed over Lua's standard io (scripting/api/libs/controls.cpp:23, lua.cpp:236-239),
# so `io.write` is exactly the kind of habit this check exists for (QA CR-8).
_CHECKED_LIBS = frozenset({"ba", "mn", "gr", "hu", "ad", "ca", "cf", "tb", "io"})

# control_info functions whose bindings parse their arguments with no self handle:
# control_info.cpp:335 ("|iiii"), :354 ("i|s"), :387 ("i"), :401 ("s"). A colon call shifts
# every argument.
_NO_SELF = ("accessButtonInfo", "useButtonControl", "getButtonControlName",
            "getButtonControlNumber")
_COLON_NO_SELF = re.compile(r":\s*(" + "|".join(_NO_SELF) + r")\b")
_CALL_OPENERS = "(\"'[{"            # what may follow a function name in a Lua call

# `name.member`, not preceded by a word character, ":" or a single "." (that would be a field of
# something else), but allowed right after the ".." concatenation operator (QA CR-1).
_LIB_USE = re.compile(r"(?:(?<=\.\.)|(?<![\w.:]))([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)")
_DECLARE = re.compile(r"(?<![\w.:])([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*=(?!=)"
                      r"|\bfunction\s+([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)")
_LOAD_CALL = re.compile(r"\b(?:loadfile|dofile)\s*\(")   # stop at "(": the literal is blanked
_QUOTED = re.compile(r"\s*([\"'])([^\"'\n]*)\1")
_FILE_MAX_BYTES = 1 << 20      # larger "scripts" are not checked (and not read beyond this)
_FILES_MAX = 20                # loaded files checked per call
_REPORT_MAX = 10               # problems listed in a refusal; the rest are counted
_LONG_OPEN = re.compile(r"\[(=*)\[")
# Check 3. A capitalised field after an expression (not after "..", the concatenation operator),
# and a colon call followed by a call opener.
# The owner is a lookbehind, not consumed: consuming it skipped every second link of a chain, so
# me.Stats.MissionPrimaryFriendlyHits (the crash this check exists for) passed (red test).
_FIELD_USE = re.compile(r"(?<=[\w)\]])\s*\.(?!\.)\s*([A-Z]\w*)")
# hv holds whatever variables the running hook passes (hv.Ship, hv.Killer, hv.Beam...), so its
# members are not in the API listing.
_DYNAMIC_LIBS = frozenset({"hv", "HookVariables"})
# A string-argument call (s:f"x") is not seen: string literals are blanked before matching.
_METHOD_USE = re.compile(r"[\w)\]]\s*:\s*([A-Za-z_]\w*)\s*[(\"'{\[]")
_CTOR_KEY = re.compile(r"[{,;]\s*([A-Za-z_]\w*)\s*=(?!=)")
_ANY_KEY_SET = re.compile(r"\.\s*([A-Za-z_]\w*)\s*=(?!=)")
# A name the code builds as a table (`t = {`, `t = t or {`, `local t = {`): its keys are the
# code's own (A1.cfg.Kp read as `A1.cfg.Kp or 17`), not API members.
_TABLE_ROOT = re.compile(r"(?<![\w.:])([A-Za-z_]\w*)\s*=\s*(?:[A-Za-z_][\w.]*\s+or\s+)?\{")
_CHAIN_BACK = 200       # characters scanned back from a field to find the chain's root name
_FUNC_DEF = re.compile(r"\bfunction\s+[\w.]+[.:]([A-Za-z_]\w*)")
# Lua 5.1 string methods (s:lower() and friends); FSO adds no others to strings.
_STRING_METHODS = frozenset({"byte", "char", "dump", "find", "format", "gmatch", "gsub", "len",
                             "lower", "match", "rep", "reverse", "sub", "upper"})


def strip_comments_and_strings(code: str) -> str:
    """Return code with every comment and string literal blanked out.

    Handles -- line comments, --[[ ]] / --[==[ ]==] block comments, "..." and '...' with
    backslash escapes, and [[ ]] / [==[ ]==] long strings. One pass, left to right; each step
    moves i forward, so the loop is bounded by len(code). An unterminated block comment or long
    string runs to the end of the text; an unterminated short string stops at the end of its line
    (the engine would reject either). Newlines are kept, so offsets stay valid.
    """
    out, n = list(code), len(code)
    i = 0
    while i < n:
        c = code[i]
        long_open = _LONG_OPEN.match(code, i) if c == "[" else None
        if code.startswith("--", i):
            m = _LONG_OPEN.match(code, i + 2)
            if m:
                close = code.find("]" + m.group(1) + "]", m.end())
                end = n if close < 0 else close + len(m.group(1)) + 2
            else:
                nl = code.find("\n", i)
                end = n if nl < 0 else nl
        elif c in "\"'":
            j = i + 1
            while j < n and code[j] != c and code[j] != "\n":
                j += 2 if code[j] == "\\" else 1
            end = min(j + 1, n)
        elif long_open:
            close = code.find("]" + long_open.group(1) + "]", long_open.end())
            end = n if close < 0 else close + len(long_open.group(1)) + 2
        else:
            i += 1
            continue
        for k in range(i, end):
            if out[k] != "\n":
                out[k] = " "
        i = end
    return "".join(out)


def _rebound(name: str, bare: str) -> bool:
    """True when the code binds `name` itself: local, assignment, function parameter, or loop."""
    w = re.escape(name)
    return bool(re.search(rf"\blocal\s+(?:\w+\s*,\s*)*{w}\b", bare)
                or re.search(rf"(?<![\w.:]){w}\s*=(?!=)", bare)
                or re.search(rf"\bfunction\b[^(]*\([^)]*\b{w}\b[^)]*\)", bare)
                or re.search(rf"\bfor\s+(?:\w+\s*,\s*)*{w}\b", bare))


def library_members(api: dict) -> dict[str, set[str]]:
    """Return {library name: member names} for the checked libraries, by short AND full name."""
    libs = {}
    for el in api.get("elements", []):
        short = el.get("shortName")
        if el.get("type") == "library" and short in _CHECKED_LIBS:
            members = {c["name"] for c in el.get("children", []) if "name" in c}
            libs[short] = members
            libs[el["name"]] = members
    return libs


def api_names(api: dict) -> set[str]:
    """Return every member name of every class and library in the API (and the names themselves)."""
    names = set()
    for el in api.get("elements", []):
        for key in ("name", "shortName"):
            if el.get(key):
                names.add(el[key])
        names.update(c["name"] for c in el.get("children", []) if "name" in c)
    return names


def declared_keys(texts: list[str]) -> set[str]:
    """Return every key the code defines anywhere: t.k = ..., { k = ... }, function t:k / t.k."""
    keys = set()
    for bare in texts:
        for m in _DECLARE.finditer(bare):
            keys.add(m.group(2) or m.group(4))
        keys.update(m.group(1) for m in _CTOR_KEY.finditer(bare))
        keys.update(m.group(1) for m in _FUNC_DEF.finditer(bare))
        keys.update(m.group(1) for m in _ANY_KEY_SET.finditer(bare))
    return keys


def table_roots(texts: list[str]) -> set[str]:
    """Return the names the code builds as tables (see _TABLE_ROOT)."""
    return {m.group(1) for bare in texts for m in _TABLE_ROOT.finditer(bare)}


def _chain_root(bare: str, dot: int) -> str:
    """Return the first name of the dotted chain ending just before offset `dot` ("" if none).

    Walks back one link at a time (blanks, a name, blanks, a single dot), at most _CHAIN_BACK
    characters; blanks count only next to a dot, so `if hv.Weapon` gives "hv", not "if".
    """
    i, stop, root = dot, max(0, dot - _CHAIN_BACK), ""
    while i > stop:
        j = i
        while j > stop and bare[j - 1] in " \t":
            j -= 1
        k = j
        while k > stop and (bare[k - 1].isalnum() or bare[k - 1] == "_"):
            k -= 1
        if k == j:
            break                                    # no name here: a call, an index, or start
        root = bare[k:j]
        while k > stop and bare[k - 1] in " \t":
            k -= 1
        if k > stop and bare[k - 1] == "." and not (k - 1 > stop and bare[k - 2] == "."):
            i = k - 1                                # one more link to the left
        else:
            break
    return root


def member_problems(bare: str, names: set[str], keys: set[str],
                    roots: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """Return one message per field or method name that exists nowhere (check 3; empty = clean).

    Receives the blanked code, the API's names, the keys the code defines and the names it
    builds as tables (fields under those are skipped).
    """
    found = set()
    for m in _FIELD_USE.finditer(bare):
        name = m.group(1)
        if _chain_root(bare, m.start()) in roots | _DYNAMIC_LIBS:
            continue
        if name not in names and name not in keys:
            found.add(f".{name}: no class or library in this build's API has a member '{name}' "
                      f"(check the handle's class in api_docs scripting.lua)")
    for m in _METHOD_USE.finditer(bare):
        name = m.group(1)
        # The no-self control_info functions are API names; check 2 reports their colon misuse.
        if (name not in names and name not in keys and name not in _STRING_METHODS
                and name not in _NO_SELF):
            found.add(f":{name}(): no class or library in this build's API has a method "
                      f"'{name}' (check the handle's class in api_docs scripting.lua)")
    return sorted(found)


def declared(texts: list[str]) -> set[tuple[str, str]]:
    """Return the (table, key) pairs the code assigns or defines, over all texts (already bare)."""
    out = set()
    for bare in texts:
        for m in _DECLARE.finditer(bare):
            out.add((m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4)))
    return out


def problems_in(code: str, bare: str, libs: dict[str, set[str]],
                decl: set[tuple[str, str]]) -> list[str]:
    """Return one message per refusable mistake in this code (empty list = clean).

    Receives the original code, the same code blanked by strip_comments_and_strings (offsets
    match), the library members, and the declarations gathered from every text of this call.
    """
    found, rebound = [], {}
    for m in _LIB_USE.finditer(bare):
        lib, name = m.group(1), m.group(2)
        if lib not in libs or name in libs[lib] or (lib, name) in decl:
            continue
        if lib not in rebound:                       # one search per library, not per use
            rebound[lib] = _rebound(lib, bare)
        if not rebound[lib]:
            found.append(f"{lib}.{name}: this build's '{lib}' library has no '{name}' "
                         f"(if you add it yourself, write {lib}[\"{name}\"])")
    for m in _COLON_NO_SELF.finditer(bare):
        # What follows the name is read from the ORIGINAL code: in the blanked copy a string
        # argument (ci:f"X") is spaces. The scan stops at the first non-blank, so it is bounded.
        k = m.end()
        while k < len(code) and code[k] in " \t\r\n":
            k += 1
        if k < len(code) and code[k] in _CALL_OPENERS:
            found.append(f":{m.group(1)}: this binding takes no self; call it with a dot, "
                         f"e.g. ci.{m.group(1)}(...)")
    return sorted(set(found))


def _loaded_paths(code: str, bare: str) -> list[str]:
    """Return the absolute paths the code passes to loadfile/dofile, in order, de-duplicated.

    The call is found in the blanked text (so a commented-out load is ignored, QA CR-6); the
    path is read from the original text at the same offset, where the string literal still is.
    """
    paths = []
    for m in _LOAD_CALL.finditer(bare):
        q = _QUOTED.match(code, m.end())
        if q and q.group(2).startswith("/") and q.group(2) not in paths:
            paths.append(q.group(2))
    return paths


def _read_script(path: str) -> str | None:
    """Return a regular file's text (bounded read), or None when it should not be checked."""
    try:
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _FILE_MAX_BYTES:
            return None                  # FIFOs and devices would block or never end (QA CR-6)
        with open(path, "rb") as f:
            return f.read(_FILE_MAX_BYTES + 1).decode("utf-8", errors="replace")
    except OSError:
        return None                      # a missing file makes loadfile return nil, not a crash


def check(code: str, scripting_json: Path) -> tuple[list[str], list[str]]:
    """Return (problems, unchecked) for `code` and the absolute-path files it loads (one level).

    problems: at most _REPORT_MAX messages plus a count line. unchecked: loaded paths that were
    not read (relative, missing, special, oversize, or past _FILES_MAX).
    """
    api = json.loads(scripting_json.read_text())
    libs, names = library_members(api), api_names(api)
    bare = strip_comments_and_strings(code)
    texts, unchecked = [(code, bare, "")], []
    for i, path in enumerate(_loaded_paths(code, bare)):
        text = _read_script(path) if i < _FILES_MAX else None
        if text is None:
            unchecked.append(path)
        else:
            texts.append((text, strip_comments_and_strings(text), f"{path}: "))
    for m in _LOAD_CALL.finditer(bare):                  # relative loads are never followed
        q = _QUOTED.match(code, m.end())
        if q and not q.group(2).startswith("/"):
            unchecked.append(q.group(2))
    decl = declared([b for _, b, _ in texts])
    keys = declared_keys([b for _, b, _ in texts])
    roots = table_roots([b for _, b, _ in texts])
    found = [prefix + p for text, b, prefix in texts
             for p in problems_in(text, b, libs, decl) + member_problems(b, names, keys, roots)]
    if len(found) > _REPORT_MAX:
        found = [*found[:_REPORT_MAX], f"... and {len(found) - _REPORT_MAX} more"]
    return found, unchecked
