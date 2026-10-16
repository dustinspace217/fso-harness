"""openal_names: print the OpenAL playback and capture device names one libopenal lists, as JSON.

Usage (run by sound.check_device_names in a child process, never imported):
    python3 -I openal_names.py <path or soname of libopenal>
Prints {"playback": [names], "capture": [names]}. Opens no device, so no stream is created.

WHY a child process: loading the engine's OpenAL into fsoctl would tie fsoctl to that library
for the rest of its life, and the child gets the engine's sound environment (PULSE_SERVER,
ALSOFT_DRIVERS) without changing fsoctl's own.
"""
import ctypes
import json
import sys

_ALL_DEVICES = 0x1013        # ALC_ALL_DEVICES_SPECIFIER (playback, extended names; FSO uses it)
_CAPTURE_DEVICES = 0x310     # ALC_CAPTURE_DEVICE_SPECIFIER
_MAX_NAMES = 256             # a device list is a few dozen at most; bounds the walk below


def names(al: ctypes.CDLL, which: int) -> list[str]:
    """Return the NUL-separated, double-NUL-terminated name list alcGetString(NULL, which) gives."""
    ptr = al.alcGetString(None, which)
    out: list[str] = []
    while ptr and len(out) < _MAX_NAMES:
        raw = ctypes.string_at(ptr)
        if not raw:
            break
        out.append(raw.decode("utf-8", "replace"))
        ptr += len(raw) + 1
    return out


def main(lib: str) -> int:
    """Load `lib`, print both lists as JSON; return 0."""
    al = ctypes.CDLL(lib)
    al.alcGetString.restype = ctypes.c_void_p
    al.alcGetString.argtypes = [ctypes.c_void_p, ctypes.c_int]
    print(json.dumps({"playback": names(al, _ALL_DEVICES), "capture": names(al, _CAPTURE_DEVICES)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
