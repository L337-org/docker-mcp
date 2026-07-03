from unittest.mock import patch

from docker_mcp.tools._labels import (
    DISABLE_ENV,
    LABEL_PREFIX,
    MANAGED_FILTER,
    MANAGED_LABEL,
    managed_filter,
    provenance_labels,
    with_provenance,
)


def test_provenance_labels_on_by_default():
    labels = provenance_labels("container_run")
    assert labels[MANAGED_LABEL] == "true"
    assert labels[f"{LABEL_PREFIX}.tool"] == "container_run"
    assert f"{LABEL_PREFIX}.version" in labels
    assert f"{LABEL_PREFIX}.created" in labels


def test_provenance_labels_disabled_via_env():
    with patch.dict("os.environ", {DISABLE_ENV: "1"}):
        assert provenance_labels("container_run") == {}


def test_with_provenance_caller_keys_win():
    merged = with_provenance({MANAGED_LABEL: "nope", "team": "infra"}, "container_run")
    assert merged is not None
    assert merged[MANAGED_LABEL] == "nope"  # caller value preserved, not overwritten
    assert merged["team"] == "infra"
    assert merged[f"{LABEL_PREFIX}.tool"] == "container_run"


def test_with_provenance_accepts_list_of_names():
    merged = with_provenance(["traefik.enable", "team"], "container_run")
    assert merged is not None
    assert merged["traefik.enable"] == ""
    assert merged["team"] == ""
    assert merged[MANAGED_LABEL] == "true"


def test_with_provenance_returns_none_when_disabled_and_no_caller_labels():
    with patch.dict("os.environ", {DISABLE_ENV: "1"}):
        assert with_provenance(None, "container_run") is None


def test_with_provenance_keeps_caller_labels_when_disabled():
    with patch.dict("os.environ", {DISABLE_ENV: "1"}):
        merged = with_provenance({"team": "infra"}, "container_run")
    assert merged == {"team": "infra"}


def test_managed_filter_adds_label_to_empty_filters():
    assert managed_filter(None) == {"label": MANAGED_FILTER}


def test_managed_filter_preserves_other_filters():
    result = managed_filter({"status": "running"})
    assert result["status"] == "running"
    assert result["label"] == MANAGED_FILTER


def test_managed_filter_combines_with_existing_string_label():
    result = managed_filter({"label": "team=infra"})
    assert result["label"] == ["team=infra", MANAGED_FILTER]


def test_managed_filter_combines_with_existing_list_label():
    result = managed_filter({"label": ["team=infra"]})
    assert result["label"] == ["team=infra", MANAGED_FILTER]


def test_managed_filter_does_not_mutate_input():
    original = {"status": "running"}
    managed_filter(original)
    assert original == {"status": "running"}
