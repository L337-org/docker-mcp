import secrets

from docker_mcp.redaction import forms, matcher
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


def test_where_two_values_overlap_the_longest_wins():
    m = Matcher()
    _index(m, "secretvalue", 1)
    _index(m, "secretvalue-and-more", 2)
    assert m.find("x secretvalue-and-more y") == [Hit(2, 22, 2, forms.RAW)]


def test_values_are_still_found_across_merges(monkeypatch):
    monkeypatch.setattr(matcher, "_MIN_MERGE_THRESHOLD", 5)
    m = Matcher()
    values = [secrets.token_urlsafe(18) for _ in range(60)]
    for value_id, value in enumerate(values):
        _index(m, value, value_id)
    text = " | ".join(values)
    assert [hit.value_id for hit in m.find(text)] == list(range(60))


def test_an_empty_matcher_finds_nothing_and_clear_forgets():
    m = Matcher()
    assert m.find("anything") == []
    _index(m, "a-stored-value", 1)
    m.clear()
    assert m.find("a-stored-value") == []
