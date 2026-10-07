"""The Claude plugin in `claude-plugin/` must stay a faithful second front-end to the `.mcpb`.

Nothing generates `plugin.json` from `manifest.json`, and nothing restamps it at release: the release
script copies this folder as committed and adds only `uv.lock`.  So every fact the two share is
asserted here rather than left to a bump checklist.  The release script's own checks are tested in
test_claude_plugin_release.py.
"""

import json
import os
import re
import tomllib
from pathlib import Path
from urllib.parse import urlparse

from docker_mcp.tools.resources import DOCKER_DOCS_BASE_URL, EXTERNAL_SECTIONS

_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_DIR = _ROOT / "claude-plugin"
_PLUGIN_JSON = _PLUGIN_DIR / ".claude-plugin" / "plugin.json"
_MANIFEST = _ROOT / "manifest.json"
_SERVER = "docker-mcp-server"


def _plugin() -> dict:
    return json.loads(_PLUGIN_JSON.read_text(encoding="utf-8"))


def _manifest() -> dict:
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def _pyproject_version() -> str:
    return tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def test_plugin_version_and_uvx_pin_match_pyproject():
    """The plugin's version and the release its `uvx` launch pins both move with pyproject.toml.

    A stale pin is the failure that matters: the directory would keep serving the previous server
    under a new plugin version.  `preflight` checks the same pair against the release tag.
    """
    version = _pyproject_version()
    plugin = _plugin()
    assert plugin["version"] == version, (
        f"claude-plugin plugin.json version {plugin['version']!r} != pyproject.toml {version!r} - bump them together"
    )
    server = plugin["mcpServers"][_SERVER]
    assert server["command"] == "uvx"
    assert server["args"] == [f"{_SERVER}=={version}"], (
        f"plugin uvx args {server['args']!r} must pin exactly {_SERVER}=={version}: the directory blocks an "
        "unpinned launcher, and a stale pin ships the previous release"
    )


def test_plugin_launch_project_pins_the_same_release_and_constraints():
    """The lock is generated from claude-plugin/pyproject.toml, so it must pin what plugin.json runs.

    Its constraint-dependencies mirror the root's, so the plugin's lock honours the same security
    floors that the project's own lock does.
    """
    version = _pyproject_version()
    plugin_project = tomllib.loads((_PLUGIN_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    root = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert plugin_project["project"]["version"] == version
    assert plugin_project["project"]["dependencies"] == [f"{_SERVER}=={version}"]
    assert plugin_project["project"]["requires-python"] == root["project"]["requires-python"]
    assert plugin_project["tool"]["uv"]["package"] is False
    assert plugin_project["tool"]["uv"]["constraint-dependencies"] == root["tool"]["uv"]["constraint-dependencies"]


def test_no_plugin_lockfile_is_committed_on_main():
    """The lock can only be generated once the release is on PyPI, so it lives only on the release
    branch; one committed here would be stale by the next release and would not be what ships."""
    assert not (_PLUGIN_DIR / "uv.lock").exists()


# The `user_config` option keys this test knows how to carry into `userConfig`.  Every one has the
# same name and meaning in both schemas.  A key outside this set (`sensitive`, `min`/`max`,
# `multiple`) fails the test below until someone decides how it maps, rather than being dropped.
_MIRRORED_OPTION_KEYS = {"type", "title", "description", "required", "default"}


def test_plugin_settings_mirror_the_mcpb_install_dialog():
    """Same options, in the same order, with every option key carried over.

    A string option with no default in the `.mcpb` gets an explicit `""` in the plugin: without a
    default, the plugin's `${user_config.KEY}` reference has no value to substitute, whereas `""` is
    read by the server as unset, which is what the `.mcpb` gives for a blank field.
    """
    manifest = _manifest()["user_config"]
    plugin = _plugin()["userConfig"]
    assert list(plugin) == list(manifest)
    for key, option in manifest.items():
        unmapped = set(option) - _MIRRORED_OPTION_KEYS
        assert not unmapped, (
            f"manifest.json option {key!r} has {sorted(unmapped)}, which this test does not know how to "
            "mirror into plugin.json's userConfig - decide the mapping and extend _MIRRORED_OPTION_KEYS"
        )
        expected = {**option, "default": option.get("default", "")}
        assert plugin[key] == expected, f"plugin userConfig {key!r} has drifted from manifest.json"


def test_plugin_passes_settings_to_the_server_as_the_mcpb_does():
    """The env mapping is the contract with the server; both front-ends must use the same one."""
    manifest_env = _manifest()["server"]["mcp_config"]["env"]
    assert _plugin()["mcpServers"][_SERVER]["env"] == manifest_env
    # Every option is wired through and nothing references an option that does not exist, which
    # `claude plugin validate` reports as an error but nothing here would otherwise notice.
    referenced = {m for v in manifest_env.values() for m in re.findall(r"\$\{user_config\.(\w+)\}", v)}
    assert referenced == set(_plugin()["userConfig"])


def test_plugin_listing_metadata_matches_the_bundle():
    """The two listings describe the same server, so the shared identity fields must agree."""
    manifest = _manifest()
    plugin = _plugin()
    assert plugin["name"] == manifest["name"] == _SERVER
    assert plugin["displayName"] == manifest["display_name"]
    assert plugin["description"] == manifest["description"]
    assert plugin["author"] == manifest["author"]
    assert plugin["license"] == manifest["license"]
    assert plugin["homepage"] == manifest["homepage"]
    assert plugin["repository"] == manifest["repository"]["url"]
    assert plugin["keywords"] == manifest["keywords"]
    assert plugin["documentationUrl"] == manifest["documentation"]
    assert plugin["supportUrl"] == manifest["support"]
    assert plugin["privacyPolicyUrl"] == manifest["privacy_policies"][0]


def test_plugin_icon_is_a_real_copy_of_the_project_icon():
    """The directory blocks a symlink it loads, so the icon is a copy - and must stay identical."""
    assert _plugin()["icon"] == "./icon.png"
    icon = _PLUGIN_DIR / "icon.png"
    assert not icon.is_symlink()
    assert icon.read_bytes() == (_ROOT / "assets" / "icon.png").read_bytes(), (
        "claude-plugin/icon.png differs from assets/icon.png - copy it again"
    )


def test_plugin_folder_holds_nothing_the_directory_rejects():
    """Symlinks anywhere in the plugin folder block submission; a root CLAUDE.md is never loaded."""
    for dirpath, dirnames, filenames in os.walk(_PLUGIN_DIR):
        for name in dirnames + filenames:
            assert not (Path(dirpath) / name).is_symlink(), f"symlink in plugin folder: {Path(dirpath) / name}"
    assert not (_PLUGIN_DIR / "CLAUDE.md").exists()


def test_plugin_readme_meets_the_directory_minimum_and_names_no_image():
    """The directory requires 40 words outside code blocks, and holds a version for review when a
    document names a bundled image or font."""
    text = (_PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
    prose = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    assert len(prose.split()) >= 40
    assert "icon.png" not in text


def test_plugin_is_excluded_from_the_other_channels_artifacts():
    """Both ignore files are denylists, so a new top-level directory ships by default."""
    assert "claude-plugin/" in (_ROOT / ".mcpbignore").read_text(encoding="utf-8").splitlines()
    assert "claude-plugin" in (_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()


def test_plugin_readme_lists_every_setting():
    """Directory reviewers and users read only the plugin folder, so its README names each setting."""
    text = (_PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
    for key, option in _plugin()["userConfig"].items():
        assert f"**{option['title']}**" in text, f"claude-plugin/README.md does not describe setting {key!r}"


def _documentation_hosts_named_in(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"documentation hosts \(([^)]*)\)", text)
    assert match, f"{path.name} has no 'documentation hosts (...)' list"
    return {h.strip() for h in re.split(r",|\band\b", " ".join(match.group(1).split())) if h.strip()}


def test_documentation_hosts_in_the_disclosures_match_the_server():
    """The plugin README and the privacy policy both list the hosts `docs_lookup` fetches from.

    The security scan reads the plugin README as the disclosure of what the plugin contacts, so a
    host added to the server's documentation sources must be named there and in the policy.
    """
    expected = {urlparse(url).hostname for url in [DOCKER_DOCS_BASE_URL, *EXTERNAL_SECTIONS.values()]}
    for doc in (_PLUGIN_DIR / "README.md", _ROOT / "PRIVACY.md"):
        assert _documentation_hosts_named_in(doc) == expected, f"{doc.name} documentation hosts have drifted"


def test_every_readme_states_the_project_is_not_affiliated_with_docker():
    """The plugin's name contains a brand, so each listing's description says whose project it is.

    The plugin README is what directory reviewers read; README.md and DOCKERHUB.md are the PyPI,
    GitHub and Docker Hub descriptions.
    """
    for doc in (_PLUGIN_DIR / "README.md", _ROOT / "README.md", _ROOT / "DOCKERHUB.md"):
        text = " ".join(doc.read_text(encoding="utf-8").split())
        assert "not affiliated with, endorsed by or sponsored by Docker, Inc." in text, doc.name
