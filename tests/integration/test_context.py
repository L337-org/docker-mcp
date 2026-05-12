# integration tests for `docker context` — require a real Docker daemon and the `docker` binary.
# run with: uv run pytest -m integration

import pytest

from tools.context import context_inspect, context_ls


def test_context_ls_returns_at_least_one_context():
    contexts = context_ls()
    assert isinstance(contexts, list)
    assert contexts, "expected at least one Docker context to be configured"
    # Every context entry has a Name key in the `--format '{{json .}}'` output.
    assert all("Name" in ctx for ctx in contexts)


def test_context_inspect_current_context_returns_endpoint():
    contexts = context_ls()
    current = next((c for c in contexts if c.get("Current")), contexts[0])
    name = current["Name"]
    detail = context_inspect(name)
    assert detail["Name"] == name
    # `Endpoints` is the docker CLI's name for the daemon/cluster URLs configured per context.
    assert "Endpoints" in detail or "Metadata" in detail


def test_context_inspect_unknown_raises():
    with pytest.raises(RuntimeError):
        context_inspect("definitely-not-a-real-context-name-xyz123")
