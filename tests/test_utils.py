from tools._utils import drop_none


def test_drop_none_filters_none_values():
    assert drop_none(a=1, b=None, c="x", d=None) == {"a": 1, "c": "x"}


def test_drop_none_keeps_falsy_non_none_values():
    assert drop_none(zero=0, empty="", emptylist=[], emptydict={}, false=False) == {
        "zero": 0,
        "empty": "",
        "emptylist": [],
        "emptydict": {},
        "false": False,
    }


def test_drop_none_with_no_kwargs_returns_empty_dict():
    assert drop_none() == {}


def test_drop_none_with_all_none_returns_empty_dict():
    assert drop_none(a=None, b=None) == {}
