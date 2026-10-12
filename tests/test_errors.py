from fsoctl.errors import EXIT_TIMEOUT, HarnessError


def test_harness_error_to_dict_carries_code_message_hint():
    err = HarnessError(EXIT_TIMEOUT, "no reply", "check `fsoctl status`")
    assert err.to_dict() == {
        "ok": False, "code": EXIT_TIMEOUT, "message": "no reply", "hint": "check `fsoctl status`",
    }
    assert str(err) == "no reply"


def test_details_appear_only_when_given():
    assert "details" not in HarnessError(EXIT_TIMEOUT, "m").to_dict()
    assert HarnessError(EXIT_TIMEOUT, "m", "", {"id": "s1"}).to_dict()["details"] == {"id": "s1"}
