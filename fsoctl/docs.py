"""api_docs: the engine's own reference for the exact build in use, cached per build and mod set.

-output_sexps / -output_script_lua / -output_script_json make the engine write sexps.html,
scripting.lua and scripting.json into its working directory within seconds of starting, and keep
running (probe results section 0). The run uses a shadow working folder so the files never land in
FS2PATH. Once the files exist and their sizes hold still, the engine is asked to quit cleanly, each
file is checked for its known ending, and the set is copied to
~/.cache/fso-harness/docs/<binary hash>-<mods hash>/ through a temp folder renamed into place,
with a manifest of the file sizes. A cache counts only if every size matches its manifest.

Why per build: the sexp list and Lua API change between builds; documentation for another build
gives an agent wrong function names. Mods matter too: mods can add sexps (Lua-defined operators).
Why the checks (QA SF-8): the old cache trusted any three files that existed, so a file copied
mid-write stayed cached forever.
"""
import argparse
import contextlib
import fcntl
import hashlib
import itertools
import json
import os
import secrets
import shutil
import time
from pathlib import Path

from . import launcher, paths, shadowdir
from .cli import register
from .errors import EXIT_ENGINE, EXIT_TIMEOUT, EXIT_USAGE, HarnessError
from .launcher import LaunchRequest

FILES = ("sexps.html", "scripting.lua", "scripting.json")
MANIFEST = "manifest.json"
_WAIT_SEC = 120.0
_LOCK_WAIT_SEC = 600.0   # longest wait for another caller generating the same key
_FINGERPRINT_MAX = 50_000   # files per api_docs call; bounds the walk of the mods
_SETTLE_SEC = 1.0   # sizes count as stable when two reads this far apart agree
# How each file ends, from the code that writes it (fs2open-3424) and the real files saved in
# docs/research: sexps.html ends with fputs("</body>\n</html>\n") (parse/sexp.cpp output_sexps);
# scripting.lua opens with "-- Lua Stub File" and its last line is the json_encodeexception alias
# (scripting/doc_luastub.cpp:591, :577, then a blank line at :638). scripting.json is one JSON
# object holding "version" and "elements" (scripting/doc_json.cpp output_json_doc). If a future
# build changes these, generation fails loudly naming the file, rather than caching a guess.
_LUA_HEAD = "-- Lua Stub File"
_LUA_LAST = "--- @alias json_encodeexception"
_HTML_END = "</html>"


def _file_hash(path: Path) -> str:
    """Return the first 12 hex digits of the file's sha256 (read in 1 MiB chunks)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def _mod_fingerprint(fs2: Path, mods: list[str]) -> list[tuple[str, int, int]]:
    """Return (path, size, mtime_ns) for every file that can change the generated reference.

    Covered: each mod's data/tables and data/scripts (Lua-defined sexps and scripts live there)
    and the .vp archives at the top of each mod folder. Names, sizes and mtimes only, never file
    contents: hashing MediaVPs would read gigabytes on every call. Limits: an edit that keeps
    both size and mtime goes unnoticed; files elsewhere in a mod (or .vp files in subfolders)
    are not looked at; FS2PATH's own loose files only count through FS2PATH's path in the key.
    """
    out: list[tuple[str, int, int]] = []
    seen = 0
    for mod in mods:
        root = fs2 / mod
        top = root.iterdir() if root.is_dir() else iter(())
        walk = itertools.chain((root / "data" / "tables").rglob("*"),
                               (root / "data" / "scripts").rglob("*"),
                               (p for p in top if p.suffix.lower() == ".vp"))
        for p in walk:   # lazy walk, so the cap stops it rather than counting after the fact
            seen += 1
            if seen > _FINGERPRINT_MAX:
                raise HarnessError(EXIT_USAGE, f"more than {_FINGERPRINT_MAX} table/script "
                                   "entries in the mods", "pass fewer mods to api_docs")
            if p.is_file():
                st = p.stat()
                out.append((str(p.relative_to(fs2)), st.st_size, st.st_mtime_ns))
    return sorted(out)


def cache_dir(binary: str, mods: list[str]) -> Path:
    """Return the cache folder for this engine binary, FS2PATH, mod list and mod contents.

    WHY all four (cross-family X-5): mods add sexps and Lua API through their tables and scripts,
    so a changed mod under the same name, or another game folder with same-named mods, must not
    be served the old reference.
    """
    fs2 = paths.fs2path()
    key = json.dumps([str(fs2), mods, _mod_fingerprint(fs2, mods)])
    mods_hash = hashlib.sha256(key.encode()).hexdigest()[:8]
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "fso-harness" / "docs" / f"{_file_hash(Path(binary))}-{mods_hash}"


def _json_reason(text: str) -> str | None:
    """Return why scripting.json text is incomplete, or None."""
    try:
        doc = json.loads(text)
    except ValueError:
        return "scripting.json is not valid JSON (cut off?)"
    if not isinstance(doc, dict) or "version" not in doc or "elements" not in doc:
        return "scripting.json lacks its version/elements keys"
    return None


def _lua_reason(text: str) -> str | None:
    """Return why scripting.lua text is incomplete, or None."""
    last = text.rstrip().rsplit("\n", 1)[-1]
    if text.startswith(_LUA_HEAD) and last.startswith(_LUA_LAST):
        return None
    return f"scripting.lua does not start with {_LUA_HEAD!r} and end with {_LUA_LAST!r}"


def incomplete_reason(path: Path) -> str | None:
    """Return why a generated reference file is not complete, or None when it is."""
    text = path.read_bytes().decode("utf-8", errors="replace")
    if not text.strip():
        return f"{path.name} is empty"
    if path.name == "scripting.json":
        return _json_reason(text)
    if path.name == "scripting.lua":
        return _lua_reason(text)
    if text.rstrip().endswith(_HTML_END):
        return None
    return f"{path.name} does not end with {_HTML_END}"


def _wait_written(shadow: Path, sid: str) -> None:
    """Wait (bounded) until all three files exist and their sizes hold still for _SETTLE_SEC."""
    deadline = time.monotonic() + _WAIT_SEC
    last: dict[str, int] | None = None
    while time.monotonic() < deadline:
        found = shadowdir.collect(shadow, list(FILES))
        sizes = {n: p.stat().st_size for n, p in found.items()}
        if len(sizes) == len(FILES) and sizes == last:
            return
        last = sizes if len(sizes) == len(FILES) else None
        time.sleep(_SETTLE_SEC if last else 0.5)
    raise HarnessError(EXIT_ENGINE, "the engine did not finish writing its reference files",
                       f"see `fsoctl log {sid}`", {"id": sid})


def _install(shadow: Path, dest: Path) -> None:
    """Copy the checked files and a size manifest into a temp folder, then rename it to dest.

    WHY the rename: a reader (or a crash) never sees a half-filled cache folder. An old dest is
    moved aside first because a folder cannot be renamed onto a non-empty one.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.tmp-{secrets.token_hex(4)}"
    tmp.mkdir()
    try:
        sizes = {}
        for name, src in shadowdir.collect(shadow, list(FILES)).items():
            shutil.copy2(src, tmp / name)
            sizes[name] = (tmp / name).stat().st_size
        (tmp / MANIFEST).write_text(json.dumps({"files": sizes}, indent=1))
        old = dest.parent / f".{dest.name}.old-{secrets.token_hex(4)}"
        if dest.exists():
            os.rename(dest, old)
        os.rename(tmp, dest)
        shutil.rmtree(old, ignore_errors=True)   # best effort: a leftover is harmless and hidden
    finally:
        # After a successful rename tmp no longer exists; after a failure it must not pile up
        # beside the cache (cross-family Y-10).
        shutil.rmtree(tmp, ignore_errors=True)


def _generate(exe: str, mods: list[str], dest: Path) -> None:
    """Run the engine with the doc flags in a shadow folder, quit it, check and cache the files."""
    shadow = shadowdir.make(paths.fs2path(),
                            paths.runtime_root() / "shadows" / secrets.token_hex(4))
    req = LaunchRequest(binary=exe, mods=mods, skip_movies=True, cwd=str(shadow),
                        extra_args=["-output_sexps", "-output_script_lua", "-output_script_json"])
    sid = launcher.launch(req)["id"]
    try:
        _wait_written(shadow, sid)
        launcher.quit(sid)   # a clean exit: every file the engine had open is closed
    finally:
        launcher.stop(sid)   # idempotent; also covers a failed wait or quit
    for name, path in shadowdir.collect(shadow, list(FILES)).items():
        reason = incomplete_reason(path)
        if reason:
            raise HarnessError(EXIT_ENGINE, f"reference file incomplete: {reason}",
                               f"run api_docs --refresh; if it repeats see `fsoctl log {sid}` "
                               f"(a new build may end {name} differently)", {"id": sid})
    _install(shadow, dest)


def _key_lock_path(dest: Path) -> Path:
    """Return the lock file that serialises generating and publishing one cache key."""
    return dest.parent / f".{dest.name}.lock"


@contextlib.contextmanager
def _key_lock(dest: Path):
    """Hold the cache key's lock while generating and publishing (bounded wait).

    WHY: two api_docs calls for one key both saw no cache, both generated, and the second
    rename failed against the first's folder, leaving its temp folder behind (cross-family Y-10).
    The wait covers one whole generation (engine start, writing, quit) by another caller.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(_key_lock_path(dest), "a") as f:   # "a": never truncates the lock file
        deadline = time.monotonic() + _LOCK_WAIT_SEC
        while True:   # bounded by the deadline below
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise HarnessError(EXIT_TIMEOUT, f"another api_docs held {dest.name} for "
                                       f"{_LOCK_WAIT_SEC:.0f}s", "retry later") from None
                time.sleep(0.2)
        yield   # released when the file closes


def _cache_valid(dest: Path) -> bool:
    """True when dest holds every file at exactly the size its manifest recorded."""
    try:
        sizes = json.loads((dest / MANIFEST).read_text())["files"]
        return all((dest / n).stat().st_size == sizes[n] for n in FILES)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def cached_reference(binary: str, mods: list[str]) -> Path | None:
    """Return this build's reference folder if a complete cache already exists, else None.

    Never generates (that launches the engine); the `lua` verb's pre-send check uses this and
    reports itself skipped when it returns None.
    """
    dest = cache_dir(binary, mods)
    return dest if _cache_valid(dest) else None


def api_docs(binary: str | None = None, mods: list[str] | None = None,
             refresh: bool = False) -> dict:
    """Return {"dir", "files", "cached"} for this build's reference, generating it if needed."""
    exe = binary or os.environ.get("FSO_BIN")
    if not exe:
        raise HarnessError(EXIT_USAGE, "no engine binary", "pass --binary or export FSO_BIN")
    mods = mods or []
    dest = cache_dir(exe, mods)
    cached = not refresh and _cache_valid(dest)
    if not cached:
        with _key_lock(dest):
            # Recheck under the lock: another fsoctl may have just generated this key while we
            # waited for it, and running the engine again would only race its publication.
            cached = not refresh and _cache_valid(dest)
            if not cached:
                _generate(exe, mods, dest)
    return {"dir": str(dest), "files": {n: str(dest / n) for n in FILES}, "cached": cached}


def _cfg(p: argparse.ArgumentParser) -> None:
    """Add the `api_docs` verb's arguments."""
    p.add_argument("--binary")
    p.add_argument("--mods", default="")
    p.add_argument("--refresh", action="store_true", help="regenerate even if cached")


@register("api_docs", "sexp and Lua API reference generated by this exact build", _cfg)
def _cli(args: argparse.Namespace) -> dict:
    """CLI wrapper for api_docs()."""
    return api_docs(args.binary, [m for m in args.mods.split(",") if m], args.refresh)
