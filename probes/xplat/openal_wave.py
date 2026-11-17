"""Probe 6 (cross-platform Plan A, Task 9): per-session audio through OpenAL Soft's wave writer.

The harness captures each session's sound by giving it its own OpenAL Soft config: ALSOFT_CONF
names a file with "drivers = wave" and "[wave] file = <session>/audio.wav", so the session writes
its audio to a file instead of a sound card (none on CI, and never the user's speakers). This
probe loads the OpenAL library an FSO build ships, opens the default device under such a config,
and plays a one-second tone, recording:

    openal_is_soft     the library is OpenAL Soft (its AL_RENDERER / AL_VERSION strings)
    device             the device name OpenAL reports (1.23.1 says "OpenAL Soft" for the wave
                       writer; newer versions say "Wave File Writer")
    wave_file_written  audio.wav exists after the device is closed
    wave_bytes         its size (how long the device ran; the writer records silence too)
    wave_peak          the loudest sample in it, 0..1
    wave_has_tone      wave_peak is above TONE_FLOOR: the tone reached the file
    al_version, al_renderer, notes

Safety: the tone is played only once audio.wav exists, i.e. the wave writer is what opened, so a
library that ignores the config can never sound on a real output. Exit 0 whatever is found.

    python openal_wave.py --out DIR --openal PATH_TO_LIBRARY
"""
import argparse
import array
import ctypes
import json
import math
import os
import platform
import struct
import sys
import time
from pathlib import Path

RATE, SECONDS, TONE_HZ = 44100, 1, 440
PLAY_WAIT_S = 3
TONE_FLOOR = 0.05            # peak above this (0..1) means the tone reached the file
AL_FORMAT_MONO16, AL_BUFFER, AL_SOURCE_STATE, AL_PLAYING = 0x1101, 0x1009, 0x1010, 0x1012
AL_VENDOR, AL_VERSION, AL_RENDERER, ALC_DEVICE_SPECIFIER = 0xB001, 0xB002, 0xB003, 0x1005


def os_name() -> str:
    """Return "linux", "windows" or "macos" for the result file name."""
    return {"Linux": "linux", "Windows": "windows", "Darwin": "macos"}.get(platform.system(),
                                                                          platform.system())


def config_text(wav: Path) -> str:
    """The per-session OpenAL Soft config: the wave backend only, writing to wav. Forward
    slashes, which Windows accepts, so no backslash in the value can be read as an escape."""
    return f"drivers = wave\n\n[wave]\nfile = {wav.as_posix()}\n"


def tone() -> bytes:
    """SECONDS of a TONE_HZ sine at RATE, 16-bit mono, at half scale."""
    return b"".join(struct.pack("<h", int(16000 * math.sin(2 * math.pi * TONE_HZ * i / RATE)))
                    for i in range(RATE * SECONDS))


def wave_chunks(data: bytes) -> dict:
    """Return the chunks of a RIFF/WAVE file as {chunk id: body}; {} when it is not one."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return {}
    chunks, pos = {}, 12
    while pos + 8 <= len(data):                # bounded: pos grows by at least 8 every pass
        size = struct.unpack_from("<I", data, pos + 4)[0]
        chunks.setdefault(data[pos:pos + 4], data[pos + 8:pos + 8 + size])
        pos += 8 + size + (size & 1)           # chunks are padded to an even length
    return chunks


def wave_peak(data: bytes):
    """Return the loudest sample of a WAV's data chunk on a 0..1 scale, or None when the file
    is not 16-bit PCM or 32-bit float (WAVE_FORMAT_EXTENSIBLE included). The wave writer records
    silence too, so a file's size says how long the device ran, not that the tone reached it
    (QA CR-3); the peak does."""
    chunks = wave_chunks(data)
    fmt, body = chunks.get(b"fmt "), chunks.get(b"data")
    if fmt is None or body is None or len(fmt) < 16:
        return None
    tag, bits = struct.unpack_from("<H", fmt, 0)[0], struct.unpack_from("<H", fmt, 14)[0]
    if tag == 0xFFFE and len(fmt) >= 26:       # extensible: the real format leads SubFormat
        tag = struct.unpack_from("<H", fmt, 24)[0]
    if (tag, bits) == (3, 32):
        samples, scale = array.array("f"), 1.0
    elif (tag, bits) == (1, 16):
        samples, scale = array.array("h"), 32768.0
    else:
        return None
    samples.frombytes(body[:len(body) - len(body) % samples.itemsize])
    return max((abs(s) for s in samples), default=0.0) / scale


def bind(lib) -> None:
    """Give the OpenAL functions this probe calls their C signatures (pointer-sized handles)."""
    vp, ui, i = ctypes.c_void_p, ctypes.c_uint, ctypes.c_int
    for name, res, args in (
            ("alcOpenDevice", vp, [ctypes.c_char_p]), ("alcCloseDevice", ctypes.c_bool, [vp]),
            ("alcCreateContext", vp, [vp, vp]), ("alcDestroyContext", None, [vp]),
            ("alcMakeContextCurrent", ctypes.c_bool, [vp]),
            ("alcGetString", ctypes.c_char_p, [vp, i]), ("alGetString", ctypes.c_char_p, [i]),
            ("alGenBuffers", None, [i, ctypes.POINTER(ui)]),
            ("alBufferData", None, [ui, i, vp, i, i]),
            ("alGenSources", None, [i, ctypes.POINTER(ui)]),
            ("alSourcei", None, [ui, i, i]), ("alSourcePlay", None, [ui]),
            ("alGetSourcei", None, [ui, i, ctypes.POINTER(i)]),
            ("alDeleteSources", None, [i, ctypes.POINTER(ui)]),
            ("alDeleteBuffers", None, [i, ctypes.POINTER(ui)]), ("alGetError", i, [])):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = res, args


def play_tone(lib, r: dict) -> None:
    """Upload the tone, play it to the end (at most PLAY_WAIT_S), and free the AL objects."""
    data = tone()
    buf, src, state = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_int()
    lib.alGenBuffers(1, ctypes.byref(buf))
    lib.alBufferData(buf, AL_FORMAT_MONO16, data, len(data), RATE)
    lib.alGenSources(1, ctypes.byref(src))
    lib.alSourcei(src, AL_BUFFER, buf.value)
    lib.alSourcePlay(src)
    deadline = time.monotonic() + PLAY_WAIT_S
    while time.monotonic() < deadline:                    # bounded by the deadline
        lib.alGetSourcei(src, AL_SOURCE_STATE, ctypes.byref(state))
        if state.value != AL_PLAYING:
            break
        time.sleep(0.05)
    r["al_error"] = lib.alGetError()
    lib.alDeleteSources(1, ctypes.byref(src))
    lib.alDeleteBuffers(1, ctypes.byref(buf))


def run(lib, r: dict, wav: Path) -> None:
    """Open the default device, check the wave writer is what opened, play the tone, close it.
    The check is the file itself: the writer creates it when the context comes up, before any
    sound (seen with 1.23.1, whose device name is "OpenAL Soft", not WAVE_DEVICE)."""
    device = lib.alcOpenDevice(None)
    if not device:
        r["notes"].append("alcOpenDevice(NULL) failed")
        return
    r["device"] = (lib.alcGetString(device, ALC_DEVICE_SPECIFIER) or b"").decode(errors="replace")
    context = lib.alcCreateContext(device, None)
    if context and lib.alcMakeContextCurrent(context):
        r["al_version"] = (lib.alGetString(AL_VERSION) or b"").decode(errors="replace")
        r["al_renderer"] = (lib.alGetString(AL_RENDERER) or b"").decode(errors="replace")
        r["openal_is_soft"] = "OpenAL Soft" in r["al_renderer"] or "ALSOFT" in r["al_version"]
        if wav.exists():
            play_tone(lib, r)
        else:
            r["notes"].append(f"opened {r['device']!r} and no {wav.name} appeared: not the wave "
                              "writer, tone not played")
        lib.alcMakeContextCurrent(None)
        lib.alcDestroyContext(context)
    else:
        r["notes"].append("could not create or make current an AL context")
    lib.alcCloseDevice(device)


def judge_wave(wav: Path, r: dict) -> None:
    """Record whether the file exists, its size, its peak and whether it holds the tone (a peak
    above TONE_FLOOR; the tone is written at half scale, about 0.29 after OpenAL's mixing)."""
    r["wave_file_written"] = wav.exists()
    if not wav.exists():
        return
    data = wav.read_bytes()
    r["wave_bytes"] = len(data)
    r["wave_peak"] = wave_peak(data)
    r["wave_has_tone"] = r["wave_peak"] is not None and r["wave_peak"] > TONE_FLOOR


def main() -> int:
    """Run the probe once and write probe-openal-wave-<os>.json; always exit 0."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--openal", type=Path, required=True)
    args = ap.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    wav, conf = out / "audio.wav", out / "alsoft.conf"
    wav.unlink(missing_ok=True)
    conf.write_text(config_text(wav))
    # Set before the library loads; ALSOFT_DRIVERS would override the config file, so it goes.
    os.environ["ALSOFT_CONF"] = str(conf)
    os.environ.pop("ALSOFT_DRIVERS", None)
    # The library's file name only: a full path would carry the machine's folders (QA SA-7).
    r = {"os": os_name(), "library": args.openal.name, "openal_is_soft": False, "device": None,
         "al_version": None, "al_renderer": None, "al_error": None,
         "wave_file_written": False, "wave_bytes": 0, "wave_peak": None, "wave_has_tone": False,
         "notes": []}
    try:
        lib = ctypes.CDLL(str(args.openal.resolve()))
        bind(lib)
        run(lib, r, wav)
    except (OSError, AttributeError, ctypes.ArgumentError) as e:   # no library, no symbol,
        r["notes"].append(f"{type(e).__name__}: {e}")              # or a call it rejects
    judge_wave(wav, r)
    (out / f"probe-openal-wave-{os_name()}.json").write_text(json.dumps(r, indent=1))
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
