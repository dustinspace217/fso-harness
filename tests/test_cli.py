import json

from fsoctl import cli
from fsoctl.errors import EXIT_NOT_FOUND


def test_version_json(capsys):
    assert cli.main(["--json", "version"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["version"] == "0.1.0"


def test_unknown_session_maps_to_exit_code(capsys, tmp_runtime):
    assert cli.main(["--json", "status", "s00000000"]) == EXIT_NOT_FOUND
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["hint"]
