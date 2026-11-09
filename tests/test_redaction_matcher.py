import secrets

from docker_mcp.redaction import forms
from docker_mcp.redaction.matcher import Hit, Matcher


def _index(m: Matcher, value: str, value_id: int) -> None:
    m.add(forms.forms_of(value), value_id)


def test_a_stored_value_is_found_raw_and_encoded():
    m = Matcher()
    value = "correct-horse-battery-staple"
    _index(m, value, 1)
    text = f"a {value} b {forms.render(value, 13)} c"
    hits = m.find(text)
    assert [(text[h.start : h.end], h.form_id) for h in hits] == [(value, forms.RAW), (forms.render(value, 13), 13)]


def test_overlapping_values_are_all_reported_for_the_caller_to_resolve():
    m = Matcher()
    _index(m, "secretvalue", 1)
    _index(m, "secretvalue-and-more", 2)
    assert m.find("x secretvalue-and-more y") == [Hit(2, 13, 1, forms.RAW), Hit(2, 22, 2, forms.RAW)]


def test_values_are_still_found_across_merges_and_the_levels_stay_few():
    m = Matcher()
    values = [secrets.token_urlsafe(18) for _ in range(60)]
    for value_id, value in enumerate(values):
        _index(m, value, value_id)
    text = " | ".join(values)
    assert [hit.value_id for hit in m.find(text) if hit.form_id == forms.RAW] == list(range(60))
    assert m.levels() <= 5


def test_an_empty_matcher_finds_nothing_and_clear_forgets():
    m = Matcher()
    assert m.find("anything") == []
    _index(m, "a-stored-value", 1)
    m.clear()
    assert m.find("a-stored-value") == []


def test_adding_nothing_new_rebuilds_nothing():
    m = Matcher()
    _index(m, "a-stored-value", 1)
    levels_before = m._levels
    _index(m, "a-stored-value", 2)
    assert m._levels is levels_before
