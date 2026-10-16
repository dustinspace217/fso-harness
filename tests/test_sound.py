"""Unit tests for launch --sound (fsoctl/sound.py, openal_names.py, launcher/runner wiring).

pactl is faked throughout (conftest's no_real_pulse stub, replaced per test): nothing here talks
to the user's sound server. Sink names carry this runtime root's tag, so they are built with
sound.sink_name() under tmp_runtime rather than written out.
"""
import configparser
import ctypes
import json
import signal
import subprocess
from pathlib import Path

import pytest

from fsoctl import channel, cli, launcher, openal_names, runner, scope, session, sound
from fsoctl.errors import EXIT_LAUNCH, EXIT_USAGE, HarnessError
from fsoctl.launcher import LaunchRequest

# The real functions, captured at import, before conftest's autouse fixture replaces them.
_REAL_PACTL = sound._pactl
_REAL_CHECK = sound.check_device_names
_REAL_RUNTIME_DIR = sound._runtime_dir


def _sess(**kw):
    base = dict(kind="game", binary="/x/fs2_open", fs2path="/g", mods=["MVPS"], mission="m.fs2",
                renderer="opengl", width=1280, height=720, switches={}, allow_eval=False,
                extra_args=[])
    base.update(kw)
    return session.new_session(**base)


class FakePactl:
    """Answers the pactl calls sound.py makes; records every call."""

    def __init__(self, sinks=(), inputs=(), modules_text="", sources=(), outputs=()):
        self.sinks, self.inputs, self.modules_text = list(sinks), list(inputs), modules_text
        self.sources, self.outputs, self.calls = list(sources), list(outputs), []

    def __call__(self, *args):
        self.calls.append(args)
        replies = {("-f", "json", "list", "short", "sinks"): self.sinks,
                   ("-f", "json", "list", "sink-inputs"): self.inputs,
                   ("-f", "json", "list", "short", "sources"): self.sources,
                   ("-f", "json", "list", "short", "source-outputs"): self.outputs}
        if args in replies:
            return json.dumps(replies[args])
        if args == ("list", "short", "modules"):
            return self.modules_text
        if args[0] == "unload-module":
            return ""
        if args[0] == "load-module":
            return "536870999\n"
        raise AssertionError(f"unexpected pactl call {args}")


def _module_line(index, name):
    return (f"{index}\tmodule-null-sink\tsink_name={name} "
            f"sink_properties=device.description={name}\t\n")


# ---- argv and validation --------------------------------------------------------------------

def test_argv_drops_nosound_only_for_a_sound_session(tmp_runtime):
    assert "-nosound" in launcher.build_argv(_sess())
    assert "-nosound" not in launcher.build_argv(_sess(sound=True))


def test_qtfred_stays_silent_even_if_the_record_says_sound(tmp_runtime):
    assert "-nosound" in launcher.build_argv(_sess(kind="qtfred", sound=True))


def test_sound_is_refused_for_qtfred_and_standalone(tmp_runtime, monkeypatch):
    with pytest.raises(HarnessError) as e:
        launcher._validate(LaunchRequest(binary="/bin/true", kind="qtfred", sound=True))
    assert e.value.code == EXIT_USAGE and "--sound" in e.value.message
    monkeypatch.setattr(launcher.match, "load", lambda mid: type("M", (), {"status": "alive"})())
    monkeypatch.setattr(launcher.match, "alive", lambda m: True)
    with pytest.raises(HarnessError) as e:
        launcher._validate(LaunchRequest(binary="/bin/true", match="m0a1b2c3d", role="standalone",
                                         sound=True))
    assert e.value.code == EXIT_USAGE and "--sound" in e.value.message


def test_cli_flag_reaches_the_request(monkeypatch):
    got = {}
    monkeypatch.setattr(launcher, "launch", lambda req: got.setdefault("req", req) and {})
    monkeypatch.setenv("FSO_BIN", "/bin/true")
    cli.main(["launch", "--sound", "--mission", "m.fs2"])
    assert got["req"].sound is True


# ---- names, environment, prefs ----------------------------------------------------------------

def test_sink_names_carry_the_runtime_root_tag(tmp_path, monkeypatch):
    monkeypatch.setenv("FSO_HARNESS_RUNTIME", str(tmp_path / "a"))
    a = sound.sink_name("s1")
    monkeypatch.setenv("FSO_HARNESS_RUNTIME", str(tmp_path / "b"))
    b = sound.sink_name("s1")
    assert a != b and a.startswith("fsoh_") and a.endswith("_s1") and b.endswith("_s1")
    assert sound.capture_name("s1") == "Monitor of " + b


def test_engine_env_uses_the_real_runtime_socket_and_never_reconnects(tmp_runtime, monkeypatch):
    monkeypatch.setattr(sound, "_runtime_dir", lambda: "/run/user/4242")
    monkeypatch.setenv("PULSE_SERVER", "tcp:elsewhere:4713")        # an inherited remote server
    env = sound.engine_env("s1")
    assert env["PULSE_SERVER"] == "unix:/run/user/4242/pulse/native"
    assert env["PULSE_SINK"] == sound.sink_name("s1") and env["ALSOFT_DRIVERS"] == "pulse"
    assert set(env["PULSE_PROP"].split()) == {"node.dont-reconnect=true",
                                              "node.dont-fallback=true"}


def test_runtime_dir_prefers_xdg_and_falls_back_to_the_uid_folder(monkeypatch):
    # Restore only this function (conftest points it at tmp_path); every other stub stays.
    monkeypatch.setattr(sound, "_runtime_dir", _REAL_RUNTIME_DIR)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(sound.os, "getuid", lambda: 4242)
    assert sound._runtime_dir() == "/run/user/4242"
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/7")
    assert sound._runtime_dir() == "/run/user/7"


def test_device_prefs_name_the_sink_and_its_monitor_in_a_fresh_folder(tmp_runtime, tmp_path):
    sound.write_device_prefs(tmp_path, "s1")
    text = (tmp_path / "fs2_open.ini").read_text()
    name = sound.sink_name("s1")
    assert f"[Sound]\nPlaybackDevice={name}\nCaptureDevice=Monitor of {name}\n" in text
    assert f'[Audio]\nPlaybackDevice="{name}"\n' in text              # in-game options: JSON


def test_device_prefs_keep_existing_keys(tmp_runtime, tmp_path):
    (tmp_path / "fs2_open.ini").write_text("[Default]\nLastPlayer=P\n")
    sound.write_device_prefs(tmp_path, "s1")
    assert (tmp_path / "fs2_open.ini").read_text().startswith("[Default]\nLastPlayer=P\n")


# ---- the OpenAL device-name check ---------------------------------------------------------------

def test_openal_names_walks_a_double_nul_list():
    buf = ctypes.create_string_buffer(b"Speakers\0Monitor of fsoh_x\0\0")

    class FakeAL:
        @staticmethod
        def alcGetString(dev, which):
            return ctypes.addressof(buf) if which == openal_names._ALL_DEVICES else None
    assert openal_names.names(FakeAL, openal_names._ALL_DEVICES) == ["Speakers",
                                                                      "Monitor of fsoh_x"]
    assert openal_names.names(FakeAL, openal_names._CAPTURE_DEVICES) == []


def test_openal_names_main_prints_both_lists(monkeypatch, capsys):
    play = ctypes.create_string_buffer(b"Speakers\0\0")
    cap = ctypes.create_string_buffer(b"Mic\0\0")

    class FakeFn:
        restype = argtypes = None

        def __call__(self, dev, which):
            return ctypes.addressof({openal_names._ALL_DEVICES: play,
                                     openal_names._CAPTURE_DEVICES: cap}[which])

    class FakeAL:
        alcGetString = FakeFn()
    loaded = []
    monkeypatch.setattr(openal_names.ctypes, "CDLL", lambda lib: loaded.append(lib) or FakeAL)
    assert openal_names.main("libopenal.so.1") == 0
    assert loaded == ["libopenal.so.1"]
    assert json.loads(capsys.readouterr().out) == {"playback": ["Speakers"], "capture": ["Mic"]}
    assert FakeAL.alcGetString.restype is ctypes.c_void_p        # pointer, not a bytes copy


LDD = ("\tlinux-vdso.so.1 (0x00007ffd)\n"
       "\tlibopenal.so.1 => /opt/fso/bin/libopenal.so.1 (0x00007fdac6000000)\n"
       "\tlibc.so.6 => /lib64/libc.so.6 (0x00007fdac5c00000)\n")


def _names_reply(monkeypatch, playback, capture, code=0, ldd=LDD):
    """Fake ldd and the openal_names child: subprocess.run inside sound returns these."""
    monkeypatch.setattr(sound, "check_device_names", _REAL_CHECK)
    seen = {}

    def run(argv, **kw):
        if argv[0] == "ldd":
            seen["ldd"] = argv
            return subprocess.CompletedProcess(argv, 0, ldd, "")
        seen["argv"], seen["env"] = argv, kw.pop("env")
        seen["kw"] = kw
        return subprocess.CompletedProcess(argv, code, json.dumps(
            {"playback": playback, "capture": capture}), "")
    monkeypatch.setattr(sound.subprocess, "run", run)
    return seen


def test_device_name_check_passes_when_both_names_are_listed(tmp_runtime, monkeypatch):
    seen = _names_reply(monkeypatch, ["Speakers", sound.sink_name("s1")],
                        ["Mic", sound.capture_name("s1")])
    sound.check_device_names("/opt/fso/bin/fs2_open", "s1")
    assert seen["ldd"] == ["ldd", "/opt/fso/bin/fs2_open"]
    assert seen["argv"][-1] == "/opt/fso/bin/libopenal.so.1"      # what the loader picks
    assert seen["argv"][1] == "-I"                                   # isolated, never -i
    assert seen["argv"][2] == openal_names.__file__
    assert seen["env"]["PULSE_SINK"] == sound.sink_name("s1")
    assert seen["env"]["PATH"] == sound.os.environ["PATH"]
    assert seen["env"]["HOME"] != sound.os.environ.get("HOME")          # no user alsoft.conf
    assert seen["env"]["XDG_CONFIG_HOME"] == seen["env"]["HOME"]
    assert seen["kw"] == {"capture_output": True, "text": True, "check": False,
                          "timeout": sound._PACTL_TIMEOUT}


@pytest.mark.parametrize("ldd", ["\tlibc.so.6 => /lib64/libc.so.6 (0x1)\n",       # not linked
                                 "\tlibopenal.so.1 => not found\n"])               # unresolved
def test_device_name_check_refuses_when_the_engines_openal_is_unknown(tmp_runtime, monkeypatch,
                                                                      ldd):
    seen = _names_reply(monkeypatch, [sound.sink_name("s1")], [sound.capture_name("s1")],
                        ldd=ldd)
    with pytest.raises(HarnessError) as e:
        sound.check_device_names("/x/fs2_open", "s1")
    assert "cannot tell which libopenal" in e.value.message and "argv" not in seen


@pytest.mark.parametrize("missing", ["playback", "capture"])
def test_device_name_check_refuses_a_missing_name(tmp_runtime, monkeypatch, missing):
    playback = ["Speakers"] + ([] if missing == "playback" else [sound.sink_name("s1")])
    capture = ["Mic"] + ([] if missing == "capture" else [sound.capture_name("s1")])
    _names_reply(monkeypatch, playback, capture)
    with pytest.raises(HarnessError) as e:
        sound.check_device_names("/x/fs2_open", "s1")
    assert e.value.code == EXIT_LAUNCH and "does not list" in e.value.message


def test_device_name_check_refuses_when_openal_cannot_be_listed(tmp_runtime, monkeypatch):
    _names_reply(monkeypatch, [], [], code=1)
    with pytest.raises(HarnessError) as e:
        sound.check_device_names("/x/fs2_open", "s1")
    assert "could not list" in e.value.message


# ---- pactl itself -------------------------------------------------------------------------------

@pytest.fixture
def real_pactl(monkeypatch):
    """Undo conftest's _pactl stub; each test fakes subprocess.run inside sound instead."""
    monkeypatch.setattr(sound, "_pactl", _REAL_PACTL)


def test_pactl_missing_is_its_own_error(real_pactl, monkeypatch):
    def run(*a, **kw):
        raise FileNotFoundError("pactl")
    monkeypatch.setattr(sound.subprocess, "run", run)
    with pytest.raises(sound.PactlMissing):
        sound._pactl("info")


def test_pactl_timeout_and_failure_are_launch_errors(real_pactl, monkeypatch):
    def slow(*a, **kw):
        raise subprocess.TimeoutExpired("pactl", 10)
    monkeypatch.setattr(sound.subprocess, "run", slow)
    with pytest.raises(HarnessError) as e:
        sound._pactl("info")
    assert e.value.code == EXIT_LAUNCH and "did not answer" in e.value.message
    assert not isinstance(e.value, sound.PactlMissing)
    monkeypatch.setattr(sound.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "no server"))
    with pytest.raises(HarnessError) as e:
        sound._pactl("info")
    assert "no server" in e.value.message


def test_pactl_talks_to_the_same_server_as_the_engine(real_pactl, monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.pop("env")
        seen["kw"] = kw
        return subprocess.CompletedProcess(argv, 0, "ok", "")
    monkeypatch.setenv("PULSE_SERVER", "tcp:elsewhere:4713")
    monkeypatch.setattr(sound.subprocess, "run", run)
    assert sound._pactl("info") == "ok"
    assert seen["argv"] == ["pactl", "info"]
    assert seen["env"]["PULSE_SERVER"] == sound.server_address()
    assert seen["env"]["PATH"] == sound.os.environ["PATH"]           # the rest is inherited
    assert seen["kw"] == {"capture_output": True, "text": True, "check": False,
                          "timeout": sound._PACTL_TIMEOUT}


def test_pactl_json_that_is_not_a_list_is_a_launch_error(monkeypatch):
    monkeypatch.setattr(sound, "_pactl", lambda *a: "Usage: pactl ...")
    with pytest.raises(HarnessError) as e:
        sound._pactl_json("list", "sink-inputs")
    assert e.value.code == EXIT_LAUNCH and "pactl 16" in e.value.hint
    monkeypatch.setattr(sound, "_pactl", lambda *a: "{}")
    with pytest.raises(HarnessError):
        sound._pactl_json("list", "sink-inputs")


# ---- sinks --------------------------------------------------------------------------------------

def test_create_sink_returns_the_module_index_and_names_the_sink(tmp_runtime, monkeypatch):
    fake = FakePactl()
    monkeypatch.setattr(sound, "_pactl", fake)
    assert sound.create_sink("s1") == 536870999
    assert fake.calls[0][:2] == ("load-module", "module-null-sink")
    assert f"sink_name={sound.sink_name('s1')}" in fake.calls[0]


def test_create_sink_refuses_a_reply_that_is_not_an_index(tmp_runtime, monkeypatch):
    monkeypatch.setattr(sound, "_pactl", lambda *a: "Failure: Module initialization failed\n")
    with pytest.raises(HarnessError) as e:
        sound.create_sink("s1")
    assert "load-module returned" in e.value.message


def test_null_sinks_skips_other_modules_short_and_garbage_lines(tmp_runtime, monkeypatch):
    text = ("12\tmodule-native-protocol-unix\t\t\n"          # another module first
            "junk\tmodule-null-sink\tsink_name=fsoh_bad\t\n"  # non-numeric index
            "13\tmodule-null-sink\n"                          # too few fields
            + _module_line(77, "fsoh_x_s1") + "14\tmodule-null-sink\t\t\n")
    monkeypatch.setattr(sound, "_pactl", FakePactl(modules_text=text))
    assert sound._null_sinks() == {77: "fsoh_x_s1", 14: ""}


def test_remove_sink_unloads_only_while_the_index_is_still_this_sessions_sink(tmp_runtime,
                                                                             monkeypatch):
    # PipeWire reuses a freed module index at once (probed 2026-10-15), so a stale index may
    # belong to another session's sink by now.
    fake = FakePactl(modules_text=_module_line(77, sound.sink_name("sother")))
    monkeypatch.setattr(sound, "_pactl", fake)
    assert sound.remove_sink(77, "sgone") is None
    assert not any(c[0] == "unload-module" for c in fake.calls)
    assert sound.remove_sink(77, "sother") is None
    assert ("unload-module", "77") in fake.calls


def test_sink_loads_and_unloads_wait_for_the_sound_lock(tmp_runtime, monkeypatch):
    fake = FakePactl(modules_text=_module_line(77, sound.sink_name("s1")))
    monkeypatch.setattr(sound, "_pactl", fake)
    monkeypatch.setattr(sound, "_LOCK_WAIT", 0.2)
    with sound.sound_lock():                       # another fsoctl holds it
        assert "held by another fsoctl" in sound.remove_sink(77, "s1")
        with pytest.raises(HarnessError):
            sound.create_sink("s2")
    assert fake.calls == []                        # neither touched the server while it was held


def test_remove_sink_reports_an_unusable_lock_file(tmp_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(sound, "_runtime_dir", lambda: str(tmp_path / "gone"))
    assert sound.remove_sink(77, "s1").startswith("sound lock:")


def test_remove_sink_reports_a_failed_unload(tmp_runtime, monkeypatch):
    fake = FakePactl(modules_text=_module_line(77, sound.sink_name("s1")))

    def pactl(*args):
        if args[0] == "unload-module":
            raise HarnessError(EXIT_LAUNCH, "pactl unload-module failed: no such module", "")
        return fake(*args)
    monkeypatch.setattr(sound, "_pactl", pactl)
    assert "no such module" in sound.remove_sink(77, "s1")


def test_orphans_are_this_roots_dead_sinks_minus_those_still_recorded(tmp_runtime, monkeypatch):
    mine = sound.sink_name
    text = (_module_line(1, mine("slive")) + _module_line(2, mine("sdead"))
            + _module_line(3, mine("srec")) + _module_line(4, "fsoh_ffffff_sother")
            + _module_line(5, "user_sink"))
    fake = FakePactl(modules_text=text,
                     sources=[{"index": 9, "name": mine("srec") + ".monitor"},
                              {"index": 8, "name": "mic"}],
                     outputs=[{"index": 1, "source": 9}, {"index": 2, "source": 8}])
    monkeypatch.setattr(sound, "_pactl", fake)
    orphans, kept = sound.orphan_sinks({"slive"})
    assert orphans == [(2, mine("sdead"))] and kept == [mine("srec")]


# ---- routing check ---------------------------------------------------------------------------

def _routing(monkeypatch, inputs):
    sinks = [{"index": 72, "name": "speakers"}, {"index": 90, "name": sound.sink_name("s1")}]
    monkeypatch.setattr(sound, "_pactl", FakePactl(sinks=sinks, inputs=inputs))


def _input(index, sink, pid="4242", props=True):
    return {"index": index, "sink": sink,
            "properties": {"application.process.id": pid} if props else None}


def test_verify_routing_passes_when_every_engine_stream_is_on_its_sink(tmp_runtime, monkeypatch):
    _routing(monkeypatch, [_input(5, 90), _input(6, 90), _input(7, 72, pid="999"),
                           _input(8, 72, props=False)])
    assert sound.verify_routing(4242, "s1") == {"sink": sound.sink_name("s1"), "streams": 2}


@pytest.mark.parametrize("inputs, words", [
    ([_input(5, 90), _input(6, 72)], "speakers"),            # one stream on the speakers
    ([_input(5, 4294967295)], "4294967295"),                 # on no sink at all
    ([_input(7, 72, pid="999")], "no audio stream"),          # the engine has none
])
def test_verify_routing_fails_closed(tmp_runtime, monkeypatch, inputs, words):
    _routing(monkeypatch, inputs)
    with pytest.raises(HarnessError) as e:
        sound.verify_routing(4242, "s1")
    assert e.value.code == EXIT_LAUNCH and words in e.value.message


# ---- launcher wiring -------------------------------------------------------------------------

@pytest.fixture
def launchable(tmp_runtime, tmp_path, monkeypatch):
    """Stub everything launch() needs except the sound calls: a fake systemd scope, a ready
    bridge, no renderer libraries. Returns the list of scope units stopped."""
    fs2 = tmp_path / "FS2"
    fs2.mkdir()
    monkeypatch.setenv("FS2PATH", str(fs2))
    states, stopped = {}, []
    monkeypatch.setattr(scope, "active_units", lambda: [])
    monkeypatch.setattr(scope, "state", lambda unit: states.get(unit, "inactive"))
    monkeypatch.setattr(scope, "is_active", lambda unit: states.get(unit) == "active")
    def start(unit, *a):
        states[unit] = "active"
        # What the runner records once the engine runs; launch passes it to verify_routing.
        (s,) = [s for s in session.list_sessions() if s.scope == unit]
        session.update(s.id, lambda rec: setattr(rec, "engine_pid", 4242))
    monkeypatch.setattr(scope, "start", start)

    def stop(unit, timeout=20):
        stopped.append(unit)
        states[unit] = "inactive"
    monkeypatch.setattr(scope, "stop", stop)
    monkeypatch.setattr(channel.Channel, "wait_ready",
                        lambda self, timeout, alive: {"state": "GS_STATE_MAIN_MENU"})
    monkeypatch.setattr(launcher.renderer, "read_maps", lambda pid: "")
    monkeypatch.setattr(sound, "_pactl", FakePactl())
    return stopped


def test_launch_returns_the_routing_check(launchable, monkeypatch):
    asked = []
    monkeypatch.setattr(sound, "verify_routing",
                        lambda pid, sid: asked.append((pid, sid)) or {"sink": "x", "streams": 1})
    r = launcher.launch(LaunchRequest(binary="/bin/true", sound=True))
    assert r["sound"] == {"sink": "x", "streams": 1}
    assert asked == [(4242, r["id"])]
    assert launcher.launch(LaunchRequest(binary="/bin/true"))["sound"] is None


def test_a_failed_routing_check_stops_the_session_and_names_it(launchable, monkeypatch):
    def misrouted(pid, sid):
        raise HarnessError(EXIT_LAUNCH, "audio stream(s) on ['speakers']", "")
    monkeypatch.setattr(sound, "verify_routing", misrouted)
    unloaded = []
    monkeypatch.setattr(sound, "remove_sink", lambda m, sid: unloaded.append(m))
    with pytest.raises(HarnessError) as e:
        launcher.launch(LaunchRequest(binary="/bin/true", sound=True))
    sid = e.value.details["id"]
    assert launchable == [session.load(sid).scope] and unloaded == [536870999]
    assert session.load(sid).status == "stopped"


def test_prepare_creates_the_sink_and_names_it_in_prefs(launchable, monkeypatch):
    checked = []
    monkeypatch.setattr(sound, "check_device_names", lambda b, sid: checked.append((b, sid)))
    sess = launcher._prepare(LaunchRequest(binary="/bin/true", sound=True))[0]
    assert checked == [(sess.binary, sess.id)]
    assert sess.sound and sess.sound_module == 536870999
    ini = (session.prefs_dir(sess) / "fs2_open.ini").read_text()
    assert f"PlaybackDevice={sound.sink_name(sess.id)}" in ini
    assert f"CaptureDevice={sound.capture_name(sess.id)}" in ini


@pytest.mark.parametrize("failing", ["write_device_prefs", "check_device_names"])
def test_a_prepare_that_fails_after_the_sink_unloads_it(launchable, monkeypatch, failing):
    def fail(*a):
        raise HarnessError(EXIT_LAUNCH, "the engine's OpenAL does not list ['x']", "")
    monkeypatch.setattr(sound, failing, fail)
    unloaded = []
    monkeypatch.setattr(sound, "remove_sink", lambda m, sid: unloaded.append((m, sid)))
    with pytest.raises(HarnessError):
        launcher._prepare(LaunchRequest(binary="/bin/true", sound=True))
    (failed,) = session.list_sessions()
    assert failed.status == "failed" and unloaded == [(536870999, failed.id)]


def test_a_prepare_failure_reports_a_sink_it_could_not_unload(launchable, monkeypatch):
    def fail(*a):
        raise HarnessError(EXIT_LAUNCH, "the engine's OpenAL does not list ['x']", "")
    monkeypatch.setattr(sound, "check_device_names", fail)
    monkeypatch.setattr(sound, "remove_sink", lambda m, sid: "server gone")
    with pytest.raises(HarnessError) as e:
        launcher._prepare(LaunchRequest(binary="/bin/true", sound=True))
    assert "server gone" in e.value.details["sound_sink_error"]


def test_the_default_run_excludes_every_real_resource_marker():
    ini = configparser.ConfigParser()
    ini.read(Path(__file__).resolve().parent.parent / "pytest.ini")
    addopts = ini["pytest"]["addopts"]
    for marker in ("engine", "vulkan", "qtfred", "sound"):
        assert f"not {marker}" in addopts, marker


def test_stop_unloads_the_sink_only_after_the_scope_is_stopped(tmp_runtime, monkeypatch):
    s = _sess(sound=True)
    s.status, s.sound_module = "running", 77
    session.save(s)
    order = []
    monkeypatch.setattr(launcher.scope, "state", lambda unit: "inactive")
    monkeypatch.setattr(launcher.scope, "stop", lambda unit, timeout=20: order.append("scope"))
    monkeypatch.setattr(sound, "remove_sink", lambda m, sid: order.append(("sink", m, sid)))
    out = launcher.stop(s.id)
    assert order == ["scope", ("sink", 77, s.id)]
    assert out["status"] == "stopped" and "sound_sink_error" not in out


def test_stop_without_sound_never_touches_the_sound_server(tmp_runtime, monkeypatch):
    s = _sess()
    s.status = "running"
    session.save(s)
    monkeypatch.setattr(launcher.scope, "state", lambda unit: "inactive")
    monkeypatch.setattr(sound, "remove_sink", lambda m, sid: pytest.fail("no sink to remove"))
    assert launcher.stop(s.id)["status"] == "stopped"


def test_stop_reports_a_sink_it_could_not_remove(tmp_runtime, monkeypatch):
    s = _sess(sound=True)
    s.status, s.sound_module = "running", 77
    session.save(s)
    monkeypatch.setattr(launcher.scope, "state", lambda unit: "inactive")
    monkeypatch.setattr(sound, "remove_sink", lambda m, sid: "server gone")
    assert "server gone" in launcher.stop(s.id)["sound_sink_error"]


@pytest.mark.parametrize("status, live", [("starting", True), ("running", True),
                                          ("exited", False), ("failed", False),
                                          ("stopped", False)])
def test_cleanup_spares_only_starting_and_running_sessions(tmp_runtime, monkeypatch, status,
                                                           live):
    s = _sess(sound=True)
    s.status = status
    session.save(s)
    monkeypatch.setattr(launcher, "_removable", lambda sess, now, older: False)
    seen = {}

    def orphans(ids):
        seen["live"] = set(ids)
        return [], []
    monkeypatch.setattr(sound, "orphan_sinks", orphans)
    launcher.cleanup(0)
    assert (s.id in seen["live"]) is live


def test_cleanup_unloads_orphans_and_reports_what_it_did(tmp_runtime, monkeypatch):
    monkeypatch.setattr(launcher, "_removable", lambda sess, now, older: False)
    monkeypatch.setattr(sound, "orphan_sinks",
                        lambda ids: ([(5, "fsoh_abcdef_sdead"), (6, "fsoh_abcdef_sbad")],
                                     ["fsoh_abcdef_srec"]))
    removed = []

    def remove(m, sid):
        removed.append((m, sid))
        return "busy" if sid == "sbad" else None
    monkeypatch.setattr(sound, "remove_sink", remove)
    out = launcher.cleanup(0)
    assert removed == [(5, "sdead"), (6, "sbad")]
    assert out["removed_sinks"] == ["fsoh_abcdef_sdead"]
    assert out["kept_sinks"] == ["fsoh_abcdef_srec"]
    assert out["failed"] == [{"path": "pulse sink fsoh_abcdef_sbad", "error": "busy"}]


def test_cleanup_is_silent_without_pactl_and_reports_any_other_failure(tmp_runtime,
                                                                       monkeypatch):
    out = launcher.cleanup(0)                     # conftest: pactl is "not installed"
    assert out["failed"] == [] and out["removed_sinks"] == []

    def no_server(*a):
        raise HarnessError(EXIT_LAUNCH, "pactl list failed: Connection refused", "")
    monkeypatch.setattr(sound, "_pactl", no_server)
    assert launcher.cleanup(0)["failed"] == [{"path": "pulse sinks",
                                               "error": "pactl list failed: Connection refused"}]


@pytest.fixture
def runner_signals():
    """Restore the SIGTERM handler and signal mask runner.main changes in this process: a
    leaked block is inherited by every child a later test spawns (it hung mcp_check's
    SIGTERM test for 30 s)."""
    handler = signal.getsignal(signal.SIGTERM)
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    yield
    signal.pthread_sigmask(signal.SIG_SETMASK, mask)
    signal.signal(signal.SIGTERM, handler)


def test_runner_gives_a_sound_session_the_engine_env_and_others_none(tmp_runtime, runner_signals,
                                                                     monkeypatch):
    got = {}
    monkeypatch.setattr(runner, "_start_xvfb", lambda w, h: (":99", None))
    monkeypatch.setattr(runner, "_run_engine", lambda sid, env, started: got.update(env) or 0)
    loud = _sess(sound=True)
    assert runner.main(loud.id) == 0
    assert {k: got.get(k) for k in sound.engine_env(loud.id)} == sound.engine_env(loud.id)
    got.clear()
    quiet = _sess()
    assert runner.main(quiet.id) == 0
    assert "PULSE_SINK" not in got and "PULSE_PROP" not in got
