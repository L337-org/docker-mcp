import logging
import pickle
import threading
import traceback

import pytest

from docker_mcp.redaction import store as store_module
from docker_mcp.redaction.store import (
    BudgetReachedError,
    IdentifiersExhaustedError,
    Secret,
    Tier,
    ValueStore,
)

_VALUE = "hunter2-but-longer"


def test_a_value_is_stored_once_per_host():
    store = ValueStore()
    first = store.add(_VALUE, 0, Tier.HARVESTED)
    assert first is not None
    assert store.add(_VALUE, 0, Tier.HARVESTED) is first
    other = store.add(_VALUE, 1, Tier.HARVESTED)
    assert other is not None and other.value_id != first.value_id
    assert store.get(first.value_id) is first
    assert store.get_for(_VALUE, 1) is other


def test_a_discovered_value_later_harvested_becomes_harvested():
    store = ValueStore()
    discovered = store.add(_VALUE, 0, Tier.DISCOVERED)
    assert discovered is not None
    promoted = store.add(_VALUE, 0, Tier.HARVESTED)
    assert promoted is not None
    assert (promoted.value_id, promoted.tier) == (discovered.value_id, Tier.HARVESTED)
    assert store.report(0)["discovered_values"] == 0
    assert store.report(0)["harvested_values"] == 1


def test_an_empty_value_is_not_stored():
    assert ValueStore().add("", 0, Tier.HARVESTED) is None


def test_the_matcher_is_given_each_new_value_and_its_forms():
    seen = []
    store = ValueStore(on_add=lambda entry, value_forms: seen.append((entry.value_id, value_forms)))
    entry = store.add(_VALUE, 0, Tier.HARVESTED)
    store.add(_VALUE, 0, Tier.HARVESTED)
    assert entry is not None
    assert len(seen) == 1 and seen[0][0] == entry.value_id and _VALUE in seen[0][1]


def test_past_the_discovered_count_limit_a_value_is_not_stored_and_this_is_logged_once(monkeypatch, caplog):
    monkeypatch.setattr(store_module, "MAX_DISCOVERED_VALUES", 2)
    store = ValueStore()
    assert store.add("discovered-one", 0, Tier.DISCOVERED) is not None
    assert store.add("discovered-two", 0, Tier.DISCOVERED) is not None
    with caplog.at_level(logging.WARNING, logger="docker_mcp.redaction"):
        assert store.add("discovered-three", 0, Tier.DISCOVERED, host_label="prod") is None
        assert store.add("discovered-four", 0, Tier.DISCOVERED, host_label="prod") is None
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "'prod'" in warnings[0]
    assert "discovered-three" not in caplog.text
    # Harvested values are unaffected by the discovered limits.
    assert store.add("harvested-value", 0, Tier.HARVESTED) is not None


def test_past_the_discovered_byte_limit_a_value_is_not_stored(monkeypatch):
    store = ValueStore()
    first = store.add("x" * 20, 0, Tier.DISCOVERED)
    assert first is not None
    # Room for one more value the same size, not for a larger one.
    monkeypatch.setattr(store_module, "MAX_DISCOVERED_BYTES", first.cost * 2 + 64)
    assert store.add("y" * 60, 0, Tier.DISCOVERED) is None
    assert store.add("z" * 20, 0, Tier.DISCOVERED) is not None


def test_a_discovered_value_longer_than_the_match_limit_is_not_stored(monkeypatch):
    monkeypatch.setattr(store_module, "MAX_DISCOVERED_MATCH_LENGTH", 10)
    store = ValueStore()
    assert store.add("x" * 11, 0, Tier.DISCOVERED) is None
    assert store.add("x" * 11, 0, Tier.HARVESTED) is not None


def test_the_overall_budget_refuses_a_harvested_value_and_stays_reached(monkeypatch, caplog):
    monkeypatch.setattr(store_module, "OVERALL_BUDGET_BYTES", 300)
    store = ValueStore()
    assert store.add("first-harvested", 0, Tier.HARVESTED) is not None
    with caplog.at_level(logging.ERROR, logger="docker_mcp.redaction"), pytest.raises(BudgetReachedError):
        store.add("z" * 200, 0, Tier.HARVESTED)
    assert store.budget_reached and store.report(0)["budget_reached"]
    assert "z" * 20 not in caplog.text
    with pytest.raises(BudgetReachedError):
        store.add("anything-else", 0, Tier.HARVESTED)
    # A value already stored is still found, so its markers keep working until the restart.
    assert store.get_for("first-harvested", 0) is not None


def test_a_discovered_value_never_pushes_the_store_over_its_overall_budget(monkeypatch):
    monkeypatch.setattr(store_module, "OVERALL_BUDGET_BYTES", 300)
    store = ValueStore()
    assert store.add("q" * 150, 0, Tier.DISCOVERED) is None
    assert not store.budget_reached


def test_identifiers_are_never_reused(monkeypatch):
    monkeypatch.setattr(store_module, "_MAX_VALUE_ID", 1)
    store = ValueStore()
    store.add("value-zero", 0, Tier.HARVESTED)
    store.add("value-one", 0, Tier.HARVESTED)
    with pytest.raises(IdentifiersExhaustedError):
        store.add("value-two", 0, Tier.HARVESTED)


def test_the_report_counts_each_kind_per_host_and_holds_no_value():
    store = ValueStore()
    store.add("harvested-on-zero", 0, Tier.HARVESTED)
    store.add("discovered-on-one", 1, Tier.DISCOVERED)
    zero, one = store.report(0), store.report(1)
    assert (zero["harvested_values"], zero["discovered_values"]) == (1, 0)
    assert (one["harvested_values"], one["discovered_values"]) == (0, 1)
    assert zero["harvested_bytes"] > len("harvested-on-zero")
    assert "harvested-on-zero" not in repr(zero)


def test_a_secret_never_prints_its_value():
    secret = Secret(_VALUE)
    # The old-style `%` form is one of the ways checked, hence UP031.
    old_style = "%s" % secret  # noqa: UP031
    for shown in (repr(secret), str(secret), f"{secret}", f"{secret!r}", f"{secret:>40}", old_style):
        assert _VALUE not in shown
    assert secret.reveal() == _VALUE
    with pytest.raises(TypeError, match="cannot be serialised"):
        pickle.dumps(secret)


def test_an_entry_never_prints_its_value_even_in_an_exception():
    entry = ValueStore().add(_VALUE, 0, Tier.HARVESTED)
    assert entry is not None
    assert _VALUE not in repr(entry) and _VALUE not in str(entry)
    try:
        raise RuntimeError(f"failed on {entry} holding {entry.secret}")
    except RuntimeError:
        assert _VALUE not in traceback.format_exc()


def test_concurrent_adds_lose_nothing_and_never_share_an_identifier():
    store = ValueStore()

    def work(worker: int) -> None:
        for i in range(500):
            store.add(f"shared-value-{i}", 0, Tier.HARVESTED)
            store.add(f"own-value-{worker}-{i}", 0, Tier.DISCOVERED)

    threads = [threading.Thread(target=work, args=(worker,)) for worker in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    entries = store.entries()
    assert len(entries) == 500 + 8 * 500
    assert len({entry.value_id for entry in entries}) == len(entries)
    report = store.report(0)
    assert (report["harvested_values"], report["discovered_values"]) == (500, 4_000)
