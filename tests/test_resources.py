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
    assert payload["sdk_base_url"] == DOCKER_DOCS_BASE_URL
    # Every SDK section is mapped to its base-URL'd HTML page.
    for section in SDK_SECTIONS:
        assert payload["sections"][section] == f"{DOCKER_DOCS_BASE_URL}/{section}.html"
    # Every external section is mapped to its absolute URL.
    for section, url in EXTERNAL_SECTIONS.items():
        assert payload["sections"][section] == url
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
