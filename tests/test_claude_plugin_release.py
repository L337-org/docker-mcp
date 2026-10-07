"""Unit tests for scripts/claude_plugin_release.py: the decisions and checks a release relies on.

The parts that talk to PyPI, GitHub and a running server are exercised by the pre-merge rehearsal
job instead (premerge.yaml), against the real services; what is here is the logic that decides
whether a publish may go ahead, and the checks whose failure must stop it.
"""

import base64
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


def _state(head=None, head_version=None, head_tree=None, published=None):
    if published is None:
        published = [head_version] if head_version else []
    return release.BranchState(head=head, head_version=head_version, head_tree=head_tree, published=published)


# --------------------------------------------------------------------------------------------
# decide(): every way a publish can go, and every way it must refuse
# --------------------------------------------------------------------------------------------


def test_first_release_creates_the_branch():
    decision = release.decide("2.2.7", "t1", _state())
    assert decision.action == "create"
    assert "first commit" in decision.reason


def test_a_newer_release_creates_a_new_commit():
    decision = release.decide("2.3.0", "t2", _state("c1", "2.2.7", "t1"))
    assert decision.action == "create"


def test_versions_compare_numerically_not_as_strings():
    assert release.decide("2.10.0", "t", _state("c", "2.9.0", "x")).action == "create"
    with pytest.raises(release.ReleaseError, match="which is newer"):
        release.decide("2.9.0", "t", _state("c", "2.10.0", "x"))


def test_an_older_release_never_published_is_refused():
    with pytest.raises(release.ReleaseError, match="already holds 2.3.0, which is newer"):
        release.decide("2.2.7", "t", _state("c", "2.3.0", "x"))


def test_rerunning_the_current_release_does_nothing():
    decision = release.decide("2.2.7", "t", _state("c", "2.2.7", "t"))
    assert decision.action == "noop"


def test_rerunning_the_current_release_with_a_different_lock_does_nothing_and_says_so():
    decision = release.decide("2.2.7", "new-tree", _state("c", "2.2.7", "old-tree"))
    assert decision.action == "noop"
    assert "differs from the published one" in decision.reason


def test_rerunning_a_superseded_release_is_refused():
    with pytest.raises(release.ReleaseError, match="published before.*moved to 2.2.7"):
        release.decide("2.2.6", "t", _state("c", "2.2.7", "x", published=["2.2.7", "2.2.6"]))


def test_a_reverted_release_is_not_published_again():
    """After a revert takes the branch from 2.2.7 back to 2.2.6, a re-run of 2.2.7 is newer than the
    head but must still be refused, or it would undo the rollback."""
    with pytest.raises(release.ReleaseError, match="by a newer release or a revert"):
        release.decide("2.2.7", "t", _state("r", "2.2.6", "x", published=["2.2.6", "2.2.7"]))


def test_a_new_release_after_a_revert_is_published():
    decision = release.decide("2.2.8", "t", _state("r", "2.2.6", "x", published=["2.2.6", "2.2.7"]))
    assert decision.action == "create"


def test_a_head_without_a_plugin_version_is_replaced_and_named():
    decision = release.decide("2.2.7", "t", _state("c", None, "x", published=[]))
    assert decision.action == "create"
    assert "no readable plugin.json" in decision.reason


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


# --------------------------------------------------------------------------------------------
# GitHub.state(): what the branch holds, read from its own history
# --------------------------------------------------------------------------------------------


class _HistoryGitHub(release.GitHub):
    """A GitHub whose API calls are served from a fixed history, shaped on the REST responses the
    class reads: a ref's ``object.sha``, the commits list's ``sha`` and ``commit.tree.sha``, and a
    file's base64 ``content``."""

    def __init__(self, history, versions_by_tree):
        super().__init__("o/r", None)
        self._history, self._versions = history, versions_by_tree
        self.reads: list[str] = []

    def call(self, method, path, body=None):
        self.reads.append(path)
        if path.startswith("git/ref/"):
            return (404, None) if not self._history else (200, {"object": {"sha": self._history[0][0]}})
        if path.startswith("commits?"):
            page = int(path.rsplit("page=", 1)[1])
            chunk = self._history[(page - 1) * 100 : page * 100]
            return 200, [{"sha": c, "commit": {"tree": {"sha": t}}} for c, t in chunk]
        commit = path.rsplit("ref=", 1)[1]
        version = self._versions[dict(self._history)[commit]]
        if version is None:
            return 404, None
        content = base64.b64encode(json.dumps({"version": version}).encode()).decode()
        return 200, {"content": content}


def test_state_of_an_absent_branch():
    assert release.GitHub.state(_HistoryGitHub([], {}), "b") == _state()


def test_state_lists_every_version_the_branch_has_held_newest_first():
    # 2.2.7 published, then reverted: the revert commit restores 2.2.6's tree.
    history = [("revert", "t6"), ("c7", "t7"), ("c6", "t6"), ("c5", "t5")]
    gh = _HistoryGitHub(history, {"t5": "2.2.5", "t6": "2.2.6", "t7": "2.2.7"})
    state = gh.state("b")
    assert state == release.BranchState("revert", "2.2.6", "t6", ["2.2.6", "2.2.7", "2.2.5"])
    assert sum(r.startswith("contents/") for r in gh.reads) == 3, "each tree's version is read once"


def test_state_reads_every_page_of_history():
    history = [(f"c{i}", f"t{i}") for i in range(250)]
    gh = _HistoryGitHub(history, {f"t{i}": f"1.0.{i}" for i in range(250)})
    assert len(gh.state("b").published) == 250


def test_state_refuses_rather_than_reading_part_of_a_long_history(monkeypatch):
    monkeypatch.setattr(release, "MAX_HISTORY_COMMITS", 150)
    history = [(f"c{i}", f"t{i}") for i in range(250)]
    with pytest.raises(release.ReleaseError, match="more than 150 commits"):
        _HistoryGitHub(history, {f"t{i}": "1.0.0" for i in range(250)}).state("b")


def test_state_skips_commits_without_a_plugin_version():
    gh = _HistoryGitHub([("c2", "t2"), ("c1", "t1")], {"t2": None, "t1": "2.2.6"})
    assert gh.state("b") == release.BranchState("c2", None, "t2", ["2.2.6"])


# --------------------------------------------------------------------------------------------
# publish(): what is written, and what happens when a write goes wrong
# --------------------------------------------------------------------------------------------


class _FakeGitHub:
    """Stands in for the GitHub class: serves a fixed state and records every write in order.

    Shaped on the class's own methods (state, commit, call) and on the REST responses the script
    reads - a ref's ``object.sha`` and a created object's ``sha`` - per the Git refs and Git
    database API reference; nothing here is a GitHub behaviour the tests assert on.
    """

    def __init__(self, state, tree, *, verified=True, refuse_move=False, move_lands_on=None):
        self._state, self._tree, self._verified = state, tree, verified
        self._refuse_move, self._move_lands_on = refuse_move, move_lands_on
        self._parents: list[str] = []
        self.writes: list[tuple[str, str, dict]] = []

    def state(self, branch):
        return self._state

    def commit(self, sha):
        return {"tree": self._tree, "parents": self._parents, "verified": self._verified, "reason": "valid"}

    def ref(self, ref):
        raise AssertionError("publish must confirm refs from write responses, not reads")

    def call(self, method, path, body=None):
        body = body or {}
        self.writes.append((method, path, body))
        if path in ("git/blobs", "git/trees"):
            return 201, {"sha": "x"}
        if path == "git/commits":
            self._parents = body["parents"]
            return 201, {"sha": "new-commit"}
        if self._refuse_move:
            raise release.ReleaseError("PATCH git/refs failed with HTTP 422: Update is not a fast forward")
        return 200, {"object": {"sha": self._move_lands_on or body["sha"]}}


def _built(tmp_path) -> tuple[Path, str]:
    build_dir = tmp_path / "build"
    shutil.copytree(_ROOT / "claude-plugin", build_dir / "claude-plugin")
    tree = release.git_tree_sha(release.read_folder(build_dir / "claude-plugin"))
    release.write_build_record(build_dir, _VERSION, "pypi", tree)
    return build_dir, tree


def _publish(monkeypatch, fake, build_dir, *extra):
    monkeypatch.setattr(release, "GitHub", lambda repo, token: fake)
    return release.main(["publish", "--version", _VERSION, "--build", str(build_dir), *extra])


def _ref_writes(fake):
    return [(m, p, b) for m, p, b in fake.writes if p.startswith("git/refs")]


def test_publish_adds_a_commit_on_the_head_and_fast_forwards(tmp_path, monkeypatch):
    build_dir, tree = _built(tmp_path)
    fake = _FakeGitHub(_state("old", "0.0.1", "t0"), tree)
    assert _publish(monkeypatch, fake, build_dir) == 0
    commit = next(b for _, p, b in fake.writes if p == "git/commits")
    assert commit["parents"] == ["old"]
    branch = f"git/refs/heads/{release.DEFAULT_BRANCH}"
    assert _ref_writes(fake) == [("PATCH", branch, {"sha": "new-commit", "force": False})]


def test_the_first_publish_creates_the_branch_from_a_parentless_commit(tmp_path, monkeypatch):
    build_dir, tree = _built(tmp_path)
    fake = _FakeGitHub(_state(), tree)
    assert _publish(monkeypatch, fake, build_dir) == 0
    assert next(b for _, p, b in fake.writes if p == "git/commits")["parents"] == []
    ref = f"refs/heads/{release.DEFAULT_BRANCH}"
    assert _ref_writes(fake) == [("POST", "git/refs", {"ref": ref, "sha": "new-commit"})]


def test_an_unsigned_commit_is_never_published(tmp_path, monkeypatch, capsys):
    build_dir, tree = _built(tmp_path)
    fake = _FakeGitHub(_state("old", "0.0.1", "t0"), tree, verified=False)
    assert _publish(monkeypatch, fake, build_dir) == 1
    assert not _ref_writes(fake)
    assert "not signed by GitHub" in capsys.readouterr().err


def test_a_branch_that_moved_since_it_was_read_is_left_alone(tmp_path, monkeypatch, capsys):
    build_dir, tree = _built(tmp_path)
    fake = _FakeGitHub(_state("old", "0.0.1", "t0"), tree, refuse_move=True)
    assert _publish(monkeypatch, fake, build_dir) == 1
    assert len(_ref_writes(fake)) == 1, "one fast-forward attempt, never a forced retry"
    err = capsys.readouterr().err
    assert "changed after it was read" in err
    assert "Update is not a fast forward" in err


def test_a_move_reported_elsewhere_fails_the_run(tmp_path, monkeypatch, capsys):
    build_dir, tree = _built(tmp_path)
    fake = _FakeGitHub(_state("old", "0.0.1", "t0"), tree, move_lands_on="somewhere-else")
    assert _publish(monkeypatch, fake, build_dir) == 1
    assert "reported it at somewhere-else" in capsys.readouterr().err


def test_rerunning_a_published_release_writes_nothing(tmp_path, monkeypatch):
    build_dir, tree = _built(tmp_path)
    fake = _FakeGitHub(_state("head", _VERSION, tree), tree)
    assert _publish(monkeypatch, fake, build_dir) == 0
    assert fake.writes == []


def test_a_dry_run_reports_a_refusal_without_failing_or_writing(tmp_path, monkeypatch, capsys):
    build_dir, tree = _built(tmp_path)
    fake = _FakeGitHub(_state("newer", "99.0.0", "t9"), tree)
    assert _publish(monkeypatch, fake, build_dir, "--dry-run") == 0
    assert fake.writes == []
    assert "would be refused" in capsys.readouterr().out


def test_a_local_build_is_never_published(tmp_path, monkeypatch, capsys):
    build_dir, tree = _built(tmp_path)
    release.write_build_record(build_dir, _VERSION, "local", tree)
    fake = _FakeGitHub(_state(), tree)
    assert _publish(monkeypatch, fake, build_dir) == 1
    assert fake.writes == []
    assert "local wheel" in capsys.readouterr().err
