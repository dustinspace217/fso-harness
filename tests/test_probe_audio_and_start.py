"""The platform-free parts of probes/xplat/openal_wave.py (plan A Task 9) and fso_start.py
(Task 8): the per-session OpenAL config, the test tone and the WAV level check, the gate that
keeps the tone off real speakers, and the engine run with a stand-in engine.

Loaded under their package-style names so mutmut can match the mutants to these tests."""
import importlib.util
import itertools
import json
import struct
import sys
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

_DIR = Path(__file__).resolve().parents[1] / "probes" / "xplat"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"probes.xplat.{name}", _DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


openal_wave = _load("openal_wave")
fso_start = _load("fso_start")


def wav_bytes(samples: bytes, tag=1, bits=16, extensible=False) -> bytes:
    """A minimal mono WAV around samples (PCM or float; optionally WAVE_FORMAT_EXTENSIBLE)."""
    rate, block = 48000, bits // 8
    fmt = struct.pack("<HHIIHH", 0xFFFE if extensible else tag, 1, rate, rate * block, block,
                      bits)
    if extensible:      # cbSize, valid bits, channel mask, SubFormat GUID (format code first)
        fmt += struct.pack("<HHI", 22, bits, 4) + struct.pack("<H", tag) + bytes(14)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(samples)) + samples
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_config_selects_only_the_wave_writer_and_its_file():
    wav = PureWindowsPath(r"C:\runner\out\audio.wav")
    text = openal_wave.config_text(wav)
    assert text.splitlines() == ["drivers = wave", "", "[wave]", "file = C:/runner/out/audio.wav"]


def test_tone_is_one_second_of_16_bit_mono_and_not_silent():
    data = openal_wave.tone()
    assert len(data) == openal_wave.RATE * openal_wave.SECONDS * 2
    samples = struct.unpack(f"<{len(data) // 2}h", data)
    assert max(samples) > 15000 and min(samples) < -15000


def test_wave_peak_reads_pcm_float_and_extensible_files():
    tone = openal_wave.tone()                          # peak 16000 / 32768
    assert openal_wave.wave_peak(wav_bytes(tone)) == pytest.approx(16000 / 32768, abs=1e-3)
    floats = struct.pack("<3f", 0.0, -0.29, 0.1)
    assert openal_wave.wave_peak(wav_bytes(floats, 3, 32)) == pytest.approx(0.29, abs=1e-6)
    assert openal_wave.wave_peak(wav_bytes(floats, 3, 32, True)) == pytest.approx(0.29, abs=1e-6)
    assert openal_wave.wave_peak(wav_bytes(bytes(2000))) == 0.0          # silence
    assert openal_wave.wave_peak(b"not a wave file") is None
    assert openal_wave.wave_peak(wav_bytes(bytes(30), 1, 24)) is None    # 24-bit: not read


def test_judge_wave_says_silence_is_not_the_tone(tmp_path):
    wav, r = tmp_path / "audio.wav", {}
    openal_wave.judge_wave(wav, r)
    assert r == {"wave_file_written": False}
    wav.write_bytes(wav_bytes(bytes(96000)))           # a second of 16-bit silence at 48 kHz
    openal_wave.judge_wave(wav, r)
    assert (r["wave_file_written"], r["wave_peak"], r["wave_has_tone"]) == (True, 0.0, False)
    wav.write_bytes(wav_bytes(openal_wave.tone()))
    openal_wave.judge_wave(wav, r)
    assert r["wave_has_tone"] is True


def fake_openal():
    """Just enough of the OpenAL C API for run() (calls are recorded, nothing plays)."""
    return SimpleNamespace(
        alcOpenDevice=lambda _name: 1, alcGetString=lambda _d, _what: b"OpenAL Soft",
        alcCreateContext=lambda _d, _a: 2, alcMakeContextCurrent=lambda _c: True,
        alGetString=lambda what: {openal_wave.AL_VERSION: b"1.1 ALSOFT 1.23.1",
                                  openal_wave.AL_RENDERER: b"OpenAL Soft"}.get(what, b""),
        alcDestroyContext=lambda _c: None, alcCloseDevice=lambda _d: True)


@pytest.mark.parametrize("wave_writer_opened", [False, True])
def test_the_tone_plays_only_when_the_wave_writer_made_its_file(tmp_path, monkeypatch,
                                                               wave_writer_opened):
    wav, played = tmp_path / "audio.wav", []
    if wave_writer_opened:
        wav.write_bytes(b"RIFF")                       # the writer creates it at context time
    monkeypatch.setattr(openal_wave, "play_tone", lambda _lib, _r: played.append(1))
    r = {"notes": []}
    openal_wave.run(fake_openal(), r, wav)
    assert bool(played) is wave_writer_opened
    assert r["openal_is_soft"] is True
    assert any("tone not played" in n for n in r["notes"]) is not wave_writer_opened


def test_a_wave_file_from_an_earlier_run_is_removed_first(tmp_path, monkeypatch):
    # Without the removal the gate above would pass for a real output device (QA TA-3).
    (tmp_path / "audio.wav").write_bytes(wav_bytes(openal_wave.tone()))
    monkeypatch.setattr(openal_wave.ctypes, "CDLL", lambda _p: (_ for _ in ()).throw(
        OSError("no such library")))
    monkeypatch.setattr(sys, "argv", ["openal_wave.py", "--out", str(tmp_path),
                                      "--openal", str(tmp_path / "libopenal.so")])
    openal_wave.main()
    r = json.loads(next(tmp_path.glob("probe-openal-wave-*.json")).read_text())
    assert (r["wave_file_written"], r["wave_has_tone"]) == (False, False)
    assert r["library"] == "libopenal.so"              # a name, never the machine's path


def test_read_log_is_empty_until_the_engine_writes_one(tmp_path):
    assert fso_start.read_log(tmp_path) == []
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "fs2_open.log").write_text("a\n... OpenGL init is complete!\n")
    assert fso_start.read_log(tmp_path) == ["a", "... OpenGL init is complete!"]


ENGINE = """#!{python}
import os, pathlib, sys, time
log = pathlib.Path(os.environ["FSO_PREFERENCES_PATH"]) / "data" / "fs2_open.log"
if {write_log}:
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("... OpenGL init is complete!\\n")
time.sleep({sleep})
sys.exit(3)
"""


def run_fake_engine(tmp_path, monkeypatch, write_log=True, sleep=0.0, stale_log=False):
    """Run fso_start.main() against a small Python 'engine'; return the result JSON."""
    pytest.importorskip("psutil")
    engine = tmp_path / "engine"
    engine.write_text(ENGINE.format(python=sys.executable, write_log=write_log, sleep=sleep))
    engine.chmod(0o755)
    out = tmp_path / "out"
    if stale_log:
        (out / "prefs" / "data").mkdir(parents=True)
        (out / "prefs" / "data" / "fs2_open.log").write_text("... OpenGL init is complete!\n")
    monkeypatch.setattr(fso_start, "START_WAIT_S", 2)
    monkeypatch.setattr(fso_start, "POLL_S", 0.1)
    monkeypatch.setattr(sys, "argv", ["fso_start.py", "--out", str(out), "--engine", str(engine)])
    fso_start.main()
    return json.loads(next(out.glob("probe-fso-start-*.json")).read_text())


@pytest.mark.linux
def test_an_engine_that_exits_reports_its_exit_code_and_gl_init(tmp_path, monkeypatch):
    r = run_fake_engine(tmp_path, monkeypatch)
    assert (r["exit_code"], r["reached_gl_init"], r["udp_ports"]) == (3, True, [])
    assert r["argv"][0] == "engine"


@pytest.mark.linux
def test_an_engine_still_running_at_the_deadline_has_no_exit_code(tmp_path, monkeypatch):
    r = run_fake_engine(tmp_path, monkeypatch, sleep=30)
    assert r["exit_code"] is None and r["seconds"] >= 2


def test_a_log_from_an_earlier_run_does_not_count(tmp_path, monkeypatch):
    r = run_fake_engine(tmp_path, monkeypatch, write_log=False, stale_log=True)
    assert r["reached_gl_init"] is False
    assert "the engine wrote no log under FSO_PREFERENCES_PATH" in r["notes"]


def test_the_tone_is_440_hz_and_never_louder_than_16000():
    samples = struct.unpack(f"<{openal_wave.RATE}h", openal_wave.tone())
    crossings = sum(1 for a, b in itertools.pairwise(samples) if a < 0 <= b)
    assert crossings in (439, 440, 441)                 # one rising crossing per cycle
    assert max(samples) <= 16000 and min(samples) >= -16000


def test_wave_peak_scale_is_exact():
    assert openal_wave.wave_peak(wav_bytes(struct.pack("<h", -16384))) == 0.5


def test_wave_peak_handles_empty_odd_and_unknown_shapes():
    assert openal_wave.wave_peak(wav_bytes(b"")) == 0.0               # empty data, last chunk
    assert openal_wave.wave_peak(wav_bytes(b"\x00\x40\x07")) == 0.5   # odd length: last byte cut
    riff_x = b"RIFX" + bytes(4) + b"WAVE" + wav_bytes(b"\x00\x40")[12:]
    assert openal_wave.wave_peak(riff_x) is None                      # not RIFF
    fmt_only = wav_bytes(b"\x00\x40")[:12 + 8 + 16]
    assert openal_wave.wave_peak(fmt_only) is None                    # no data chunk
    data_only = b"RIFF" + bytes(4) + b"WAVE" + b"data" + struct.pack("<I", 2) + b"\x00\x40"
    assert openal_wave.wave_peak(data_only) is None                   # no fmt chunk


def test_wave_peak_skips_an_odd_sized_chunk_and_its_pad_byte():
    good = wav_bytes(b"\x00\x40")
    junk = b"junk" + struct.pack("<I", 3) + b"abc" + b"\x00"      # 3 bytes + 1 pad byte
    padded = good[:12] + junk + good[12:]
    assert openal_wave.wave_peak(padded) == 0.5


def test_wave_peak_reads_the_format_from_a_plain_header_with_extra_bytes():
    # A plain PCM fmt chunk longer than 26 bytes: the SubFormat field must not be consulted.
    fmt = struct.pack("<HHIIHH", 1, 1, 48000, 96000, 2, 16) + struct.pack("<H", 10) + bytes(10)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", 2) + b"\x00\x40"
    assert openal_wave.wave_peak(b"RIFF" + struct.pack("<I", len(body)) + body) == 0.5


def test_wave_peak_reads_an_extensible_header_of_exactly_26_bytes():
    fmt = struct.pack("<HHIIHH", 0xFFFE, 1, 48000, 96000, 2, 16) + struct.pack("<HHIH", 8, 16, 4, 1)
    assert len(fmt) == 26
    body = b"WAVE" + b"fmt " + struct.pack("<I", 26) + fmt
    body += b"data" + struct.pack("<I", 2) + b"\x00\x40"
    assert openal_wave.wave_peak(b"RIFF" + struct.pack("<I", len(body)) + body) == 0.5


def test_judge_wave_records_the_size_and_needs_more_than_the_floor(tmp_path, monkeypatch):
    wav = tmp_path / "audio.wav"
    data = wav_bytes(openal_wave.tone())
    wav.write_bytes(data)
    r = {}
    openal_wave.judge_wave(wav, r)
    assert r["wave_bytes"] == len(data)
    monkeypatch.setattr(openal_wave, "wave_peak", lambda _d: openal_wave.TONE_FLOOR)
    openal_wave.judge_wave(wav, r)
    assert r["wave_has_tone"] is False                  # exactly the floor is not a tone


def test_read_log_survives_bytes_that_are_not_utf8(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "fs2_open.log").write_bytes(b"\xff\xfe\n... OpenGL init is complete!\n")
    assert fso_start.read_log(tmp_path)[-1] == "... OpenGL init is complete!"
