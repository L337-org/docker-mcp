import json
from unittest.mock import MagicMock, patch

import pytest

from tools.resources import (
    DOCKER_DOCS_BASE_URL,
    EXTERNAL_SECTIONS,
    SDK_SECTIONS,
    get_docs_section,
    list_docs_sections,
)


def test_list_docs_sections_returns_json_with_sdk_and_external_sections():
    payload = json.loads(list_docs_sections())
    # Backward-compatible fields: `base_url` (SDK base) and `sections` (list of section names).
    assert payload["base_url"] == DOCKER_DOCS_BASE_URL
    assert payload["sdk_base_url"] == DOCKER_DOCS_BASE_URL
    assert isinstance(payload["sections"], list)
    for section in SDK_SECTIONS:
        assert section in payload["sections"]
    for section in EXTERNAL_SECTIONS:
        assert section in payload["sections"]
    # New field: `section_urls` maps each section name to its absolute URL.
    for section in SDK_SECTIONS:
        assert payload["section_urls"][section] == f"{DOCKER_DOCS_BASE_URL}/{section}.html"
    for section, url in EXTERNAL_SECTIONS.items():
        assert payload["section_urls"][section] == url
    assert "usage" in payload


def test_get_docs_section_fetches_sdk_section_at_base_url():
    response = MagicMock()
    response.read.return_value = b"<html>containers</html>"
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    with patch("tools.resources.urllib.request.urlopen", return_value=response) as mock_urlopen:
        result = get_docs_section("containers")
    assert result == "<html>containers</html>"
    mock_urlopen.assert_called_once_with(f"{DOCKER_DOCS_BASE_URL}/containers.html")


def test_get_docs_section_fetches_external_section_at_absolute_url():
    response = MagicMock()
    response.read.return_value = b"<html>compose</html>"
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    with patch("tools.resources.urllib.request.urlopen", return_value=response) as mock_urlopen:
        result = get_docs_section("compose")
    assert result == "<html>compose</html>"
    mock_urlopen.assert_called_once_with(EXTERNAL_SECTIONS["compose"])


def test_get_docs_section_rejects_unknown_section():
    with pytest.raises(ValueError, match="Unknown documentation section"):
        get_docs_section("not-a-section")
