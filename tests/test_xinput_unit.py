import pytest

from fsoctl import xinput
from fsoctl.errors import EXIT_USAGE, HarnessError


@pytest.mark.parametrize("crop", ["10x10", "axb+1+1", "0x5+0+0", "5x5+-1+0"])
def test_bad_crop_rejected(crop):
    with pytest.raises(HarnessError) as e:
        xinput.parse_crop(crop)
    assert e.value.code == EXIT_USAGE


def test_good_crop_parsed():
    assert xinput.parse_crop("400x300+10+20") == (400, 300, 10, 20)


def test_scale_bounds():
    with pytest.raises(HarnessError):
        xinput.check_scale(1.5)
    assert xinput.check_scale(0.5) == 0.5


def test_key_names_reject_shell_metacharacters():
    with pytest.raises(HarnessError):
        xinput.check_keys(["Return; rm -rf ~"])
    assert xinput.check_keys(["alt+r", "KP_4", "Return"]) == ["alt+r", "KP_4", "Return"]
