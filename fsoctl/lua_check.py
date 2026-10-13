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
Both run on the code with comments and string literals blanked, so text inside them never
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
    libs = library_members(json.loads(scripting_json.read_text()))
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
    found = [prefix + p for text, b, prefix in texts for p in problems_in(text, b, libs, decl)]
    if len(found) > _REPORT_MAX:
        found = [*found[:_REPORT_MAX], f"... and {len(found) - _REPORT_MAX} more"]
    return found, unchecked
