"""Unit tests for scripts/claude_plugin_release.py: the decisions and checks a release relies on.

The parts that talk to PyPI, GitHub and a running server are exercised by the pre-merge rehearsal
job instead (premerge.yaml), against the real services; what is here is the logic that decides
whether a publish may go ahead, and the checks whose failure must stop it.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("claude_plugin_release", _ROOT / "scripts" / "claude_plugin_release.py")
assert _spec and _spec.loader
release = importlib.util.module_from_spec(_spec)
sys.modules["claude_plugin_release"] = release
_spec.loader.exec_module(release)

_VERSION = json.loads((_ROOT / "claude-plugin" / ".claude-plugin" / "plugin.json").read_text())["version"]


def _state(head=None, head_version=None, head_tree=None, tag=None):
    return release.BranchState(head=head, head_version=head_version, head_tree=head_tree, tag=tag)


# --------------------------------------------------------------------------------------------
# decide(): every way a publish can go, and every way it must refuse
# --------------------------------------------------------------------------------------------


def test_first_release_creates_the_branch():
    decision = release.decide("2.2.7", "t1", _state(), rollback=False)
    assert decision.action == "create"
    assert "first release" in decision.reason


def test_a_newer_release_creates_a_new_commit():
    decision = release.decide("2.3.0", "t2", _state("c1", "2.2.7", "t1"), rollback=False)
    assert decision.action == "create"


def test_versions_compare_numerically_not_as_strings():
    assert release.decide("2.10.0", "t", _state("c", "2.9.0", "x"), rollback=False).action == "create"
    with pytest.raises(release.ReleaseError, match="not older"):
        release.decide("2.9.0", "t", _state("c", "2.10.0", "x"), rollback=False)


def test_an_older_release_without_a_tag_is_refused():
    with pytest.raises(release.ReleaseError, match="already holds 2.3.0, which is not older"):
        release.decide("2.2.7", "t", _state("c", "2.3.0", "x"), rollback=False)


def test_the_same_version_with_a_different_build_is_refused():
    with pytest.raises(release.ReleaseError, match="already holds 2.2.7"):
        release.decide("2.2.7", "new-tree", _state("c", "2.2.7", "old-tree"), rollback=False)


def test_a_run_that_moved_the_branch_but_failed_to_tag_resumes_by_tagging():
    decision = release.decide("2.2.7", "same", _state("c", "2.2.7", "same"), rollback=False)
    assert decision.action == "tag-head"


def test_rerunning_the_current_release_does_nothing():
    decision = release.decide("2.2.7", "t", _state("c", "2.2.7", "t", tag="c"), rollback=False)
    assert decision.action == "noop"


def test_a_tagged_but_unpublished_newer_release_moves_the_branch_to_the_tag():
    decision = release.decide("2.3.0", "t", _state("c", "2.2.7", "x", tag="c2"), rollback=False)
    assert decision.action == "move-to-tag"


def test_rerunning_a_superseded_release_is_refused_and_points_at_rollback():
    with pytest.raises(release.ReleaseError, match="superseded release.*claude_plugin_rollback"):
        release.decide("2.2.6", "t", _state("c", "2.2.7", "x", tag="old"), rollback=False)


def test_a_rollback_moves_to_the_earlier_tag():
    decision = release.decide("2.2.6", "t", _state("c", "2.2.7", "x", tag="old"), rollback=True)
    assert decision.action == "move-to-tag"
    assert "from 2.2.7 to 2.2.6" in decision.reason


def test_a_rollback_to_a_release_with_no_plugin_commit_is_refused():
    with pytest.raises(release.ReleaseError, match="no published plugin commit is tagged"):
        release.decide("2.2.5", "t", _state("c", "2.2.7", "x"), rollback=True)


def test_a_rollback_to_the_release_already_held_does_nothing():
    assert release.decide("2.2.6", "t", _state("c", "2.2.6", "x", tag="c"), rollback=True).action == "noop"


def test_a_version_that_is_not_a_plain_release_is_refused():
    with pytest.raises(release.ReleaseError, match="not a plain dotted release version"):
        release.version_key("2.2.7rc1")


# --------------------------------------------------------------------------------------------
# git_tree_sha(): the id we compare GitHub's commit against must be git's own
# --------------------------------------------------------------------------------------------


def test_tree_id_matches_git_for_the_plugin_folder(tmp_path):
    """Mutation-proof by construction: any byte change in any file changes git's id and ours."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed, so the tree id cannot be compared with git's")
    shutil.copytree(_ROOT / "claude-plugin", tmp_path / "claude-plugin")
    (tmp_path / "claude-plugin" / "uv.lock").write_text("lock\n")
    # Git sorts a subtree as if its name ended in "/", which only shows when a directory's name is a
    # prefix of a file's: "x.txt" sorts before "x/" but after "x".
    (tmp_path / "claude-plugin" / "x").mkdir()
    (tmp_path / "claude-plugin" / "x" / "inner").write_text("inner\n")
    (tmp_path / "claude-plugin" / "x.txt").write_text("outer\n")
    git = [shutil.which("git") or "git", "-C", str(tmp_path), "-c", "core.fileMode=false"]
    subprocess.run([*git, "init", "-q"], check=True)  # noqa: S603 - fixed argv, resolved binary, no shell
    subprocess.run([*git, "add", "-A"], check=True)  # noqa: S603 - as above
    written = subprocess.run([*git, "write-tree"], check=True, capture_output=True, text=True)  # noqa: S603
    expected = written.stdout.strip()
    assert release.git_tree_sha(release.read_folder(tmp_path / "claude-plugin")) == expected


def test_a_symlink_in_the_plugin_folder_is_refused(tmp_path):
    folder = tmp_path / "claude-plugin"
    folder.mkdir()
    (folder / "real.txt").write_text("x")
    (folder / "link.txt").symlink_to(folder / "real.txt")
    with pytest.raises(release.ReleaseError, match="symlink"):
        release.read_folder(folder)


# --------------------------------------------------------------------------------------------
# check_plugin_folder() and check_lock()
# --------------------------------------------------------------------------------------------


def _plugin_copy(tmp_path) -> Path:
    target = tmp_path / "claude-plugin"
    shutil.copytree(_ROOT / "claude-plugin", target)
    return target


def test_the_repository_plugin_folder_passes_its_release_checks(tmp_path):
    release.check_plugin_folder(_plugin_copy(tmp_path), _VERSION)


def test_folder_checks_name_each_problem(tmp_path):
    plugin = _plugin_copy(tmp_path)
    (plugin / "README.md").write_text("too short, and names icon.png\n")
    manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
    manifest["mcpServers"]["docker-mcp-server"]["args"] = ["docker-mcp-server"]
    (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest))
    with pytest.raises(release.ReleaseError) as caught:
        release.check_plugin_folder(plugin, _VERSION)
    message = str(caught.value)
    assert "fewer than 40 words" in message
    assert "names icon.png" in message
    assert f"must launch `uvx docker-mcp-server=={_VERSION}`" in message


def test_folder_checks_refuse_a_version_the_plugin_does_not_declare(tmp_path):
    with pytest.raises(release.ReleaseError, match=r"plugin.json version .* != '9.9.9'"):
        release.check_plugin_folder(_plugin_copy(tmp_path), "9.9.9")


def _write_lock(plugin: Path, packages: list[str]) -> None:
    plugin.mkdir(exist_ok=True)
    (plugin / "uv.lock").write_text("version = 1\n\n" + "\n".join(packages))


_SERVER = """[[package]]
name = "docker-mcp-server"
version = "2.2.7"
source = {{ registry = "{registry}" }}
sdist = {{ url = "https://x/s.tar.gz", hash = "sha256:aaa" }}
wheels = [{{ url = "https://x/w.whl", hash = "sha256:bbb" }}]
"""
_DEP = """[[package]]
name = "docker"
version = "7.1.0"
source = {{ registry = "{registry}" }}
wheels = [{{ url = "https://x/d.whl", hash = "{hash}" }}]
"""


def test_a_lock_pinned_from_pypi_with_hashes_passes(tmp_path):
    _write_lock(
        tmp_path,
        [_SERVER.format(registry=release.PYPI_INDEX), _DEP.format(registry=release.PYPI_INDEX, hash="sha256:ccc")],
    )
    release.check_lock(tmp_path, "2.2.7", {"aaa", "bbb"})


def test_lock_checks_name_each_problem(tmp_path):
    _write_lock(
        tmp_path,
        [
            _SERVER.format(registry=release.PYPI_INDEX),
            _DEP.format(registry="https://evil.example/simple", hash="md5:1"),
        ],
    )
    with pytest.raises(release.ReleaseError) as caught:
        release.check_lock(tmp_path, "2.2.8", {"other"})
    message = str(caught.value)
    assert "pins docker-mcp-server '2.2.7', not '2.2.8'" in message
    assert "hashes do not match what PyPI serves" in message
    assert "docker comes from" in message and "evil.example" in message
    assert "docker has an artefact without a sha256 hash" in message


def test_a_local_rehearsal_lock_exempts_only_the_servers_own_source(tmp_path):
    _write_lock(
        tmp_path, [_SERVER.format(registry="../dist"), _DEP.format(registry=release.PYPI_INDEX, hash="sha256:c")]
    )
    release.check_lock(tmp_path, "2.2.7", None)
    _write_lock(tmp_path, [_SERVER.format(registry="../dist"), _DEP.format(registry="../dist", hash="sha256:c")])
    with pytest.raises(release.ReleaseError, match="docker comes from"):
        release.check_lock(tmp_path, "2.2.7", None)


def test_a_lock_without_the_server_is_refused(tmp_path):
    _write_lock(tmp_path, [_DEP.format(registry=release.PYPI_INDEX, hash="sha256:c")])
    with pytest.raises(release.ReleaseError, match="no docker-mcp-server entry"):
        release.check_lock(tmp_path, "2.2.7", None)


def test_an_oversized_lock_is_refused(tmp_path):
    _write_lock(tmp_path, [_SERVER.format(registry=release.PYPI_INDEX), "# " + "x" * release.MAX_FILE_BYTES])
    with pytest.raises(release.ReleaseError, match="the directory reads up to"):
        release.check_lock(tmp_path, "2.2.7", {"aaa", "bbb"})


# --------------------------------------------------------------------------------------------
# plugin_env(): the smoke test drives the server through the plugin's own settings mapping
# --------------------------------------------------------------------------------------------


def test_unset_settings_reach_the_server_as_the_mcpb_sends_them():
    env = release.plugin_env(_ROOT / "claude-plugin", {})
    assert env == {
        "DOCKER_MCP_SERVER_HOSTS": "",
        "DOCKER_MCP_SERVER_READONLY": "false",
        "DOCKER_MCP_SERVER_NO_DESTRUCTIVE": "false",
        "DOCKER_MCP_SERVER_DISABLE": "",
    }


def test_set_settings_reach_the_server_as_strings():
    env = release.plugin_env(_ROOT / "claude-plugin", {"read_only": True, "disable_domains": "swarm"})
    assert env["DOCKER_MCP_SERVER_READONLY"] == "true"
    assert env["DOCKER_MCP_SERVER_DISABLE"] == "swarm"


def test_an_env_reference_to_an_undeclared_option_is_refused(tmp_path):
    plugin = _plugin_copy(tmp_path)
    manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
    manifest["mcpServers"]["docker-mcp-server"]["env"]["X"] = "${user_config.nope}"
    (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest))
    with pytest.raises(release.ReleaseError, match="undeclared option 'nope'"):
        release.plugin_env(plugin, {})
