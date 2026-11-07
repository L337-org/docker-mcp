import gzip
import inspect
import io
import secrets
import tarfile
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import docker_mcp._hosts as hosts
from docker_mcp._hosts import parse_registry
from docker_mcp.redaction import disk, settings, wire
from docker_mcp.redaction.redactor import Redactor
from docker_mcp.redaction.settings import NameRules
from docker_mcp.redaction.wire import Scope

# Any marker-shaped text is refused in a file, whichever process issued it: a marker means nothing there.
_MARKER = "[REDACTED:" + "Q" * 22 + "]"
_SECRET = "pw-" + secrets.token_urlsafe(18)


class _NoHarvest:
    def ensure_fresh(self):
        pass

    def harvest_if_unseen(self, ids):
        return False


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(
        hosts, "_registry", parse_registry("dev=ssh://dev, prod=ssh://prod(redact), stage=ssh://stage(ro,redact)")
    )
    monkeypatch.setattr(settings, "_global", False)
    monkeypatch.setattr(settings, "_names", NameRules())
    monkeypatch.setattr(wire, "REDACTOR", Redactor(host_labels=hosts.labels))
    monkeypatch.setattr(wire, "_harvest_for", lambda label: _NoHarvest())


def _tool(name: str, *, writes: bool = True):
    calls: list[dict[str, Any]] = []

    def target(
        files: list | None = None,
        project_dir: str | None = None,
        compose_files: list | None = None,
        cwd: str | None = None,
        context: str | None = None,
        file: str | None = None,
        build_contexts: dict | None = None,
        secret: list | None = None,
        path: str | None = None,
        dockerfile: str | None = None,
        source: str | None = None,
        data: bytes | None = None,
        from_file: str | None = None,
        host: str | None = None,
    ) -> str:
        calls.append({"host": host})
        return "ok"

    return wire.wrap(target, name, Scope.HOST_ARGUMENT, ToolError, writes=writes), calls


def _refused(name: str, **arguments) -> str:
    wrapped, calls = _tool(name)
    with pytest.raises(ToolError) as caught:
        wrapped(host="prod", **arguments)
    assert calls == [], "the call ran"
    return str(caught.value)


def _runs(name: str, host: str = "prod", writes: bool = True, **arguments) -> None:
    wrapped, calls = _tool(name, writes=writes)
    wrapped(host=host, **arguments)
    assert len(calls) == 1


def _project(tmp_path: Path, **files: str) -> Path:
    for relative, text in files.items():
        target = tmp_path / relative.replace("__", "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return tmp_path


# ---------- files a call reads ----------


def test_a_compose_file_holding_a_marker_is_refused_naming_the_file(tmp_path):
    project = _project(tmp_path, **{"compose.yaml": f"services:\n  db:\n    environment:\n      PW: {_MARKER}\n"})
    message = _refused("compose_up", project_dir=str(project))
    assert str(project / "compose.yaml") in message
    assert "holds a [REDACTED:...] marker" in message
    assert "never swapped back in a file" in message


def test_a_marker_anywhere_in_the_compose_project_is_refused(tmp_path):
    # A secret file, an env file or a build context under the project directory can all reach a container.
    project = _project(tmp_path, **{"compose.yaml": "services: {}\n", "app__config__settings.ini": f"key={_MARKER}"})
    assert "settings.ini" in _refused("compose_up", project_dir=str(project))


def test_a_clean_compose_project_runs(tmp_path):
    _runs("compose_up", project_dir=str(_project(tmp_path, **{"compose.yaml": "services: {}\n", ".env": "A=1\n"})))


def test_the_compose_project_directory_is_the_first_compose_files(tmp_path):
    project = _project(tmp_path, **{"sub__compose.yaml": "services: {}\n", "sub__.env": f"PW={_MARKER}\n"})
    assert ".env" in _refused("compose_up", files=[str(project / "sub" / "compose.yaml")])


def test_a_file_named_outside_the_working_tree_is_searched(tmp_path):
    work = _project(tmp_path / "work", **{"docker-bake.hcl": "target app {}\n"})
    outside = _project(tmp_path / "elsewhere", **{"extra.hcl": f'variable X {{ default = "{_MARKER}" }}\n'})
    assert "extra.hcl" in _refused("buildx_bake", cwd=str(work), files=[str(outside / "extra.hcl")])


def test_a_stack_compose_file_holding_a_marker_is_refused(tmp_path):
    project = _project(tmp_path, **{"stack.yml": f"services:\n  a:\n    image: x\n    command: {_MARKER}\n"})
    assert "stack.yml" in _refused("stack_deploy", compose_files=[str(project / "stack.yml")])


def test_a_marker_anywhere_in_a_build_context_is_refused(tmp_path):
    context = _project(tmp_path, **{"Dockerfile": "FROM alpine\nCOPY . /app\n", "src__app.conf": _MARKER})
    assert "app.conf" in _refused("image_build", path=str(context))
    assert "app.conf" in _refused("buildx_build", context=str(context))


def test_a_file_the_build_context_excludes_is_not_searched(tmp_path):
    context = _project(
        tmp_path, **{"Dockerfile": "FROM alpine\n", ".dockerignore": "notes/\n", "notes__todo.txt": _MARKER}
    )
    _runs("image_build", path=str(context))


def test_an_excluded_dockerfile_is_still_searched(tmp_path):
    context = _project(
        tmp_path, **{"build.Dockerfile": f"FROM alpine\nENV X={_MARKER}\n", ".dockerignore": "*.Dockerfile\n"}
    )
    assert "build.Dockerfile" in _refused("image_build", path=str(context), dockerfile="build.Dockerfile")


def test_an_excluded_dockerfile_named_by_absolute_path_is_still_searched(tmp_path):
    context = _project(
        tmp_path, **{"build.Dockerfile": f"FROM alpine\nENV X={_MARKER}\n", ".dockerignore": "*.Dockerfile\n"}
    )
    message = _refused("image_build", path=str(context), dockerfile=str(context / "build.Dockerfile"))
    assert "build.Dockerfile" in message


def test_an_unknown_home_directory_is_left_for_the_tool_to_report():
    # pathlib's expanduser raises a bare RuntimeError for an unknown `~user`; the call must reach the tool.
    _runs("buildx_build", context="~no-such-user-dm42/ctx", file="~no-such-user-dm42/Dockerfile")


def test_buildx_build_searches_its_named_contexts_and_secret_files(tmp_path):
    context = _project(tmp_path / "ctx", Dockerfile="FROM alpine\n")
    extra = _project(tmp_path / "extra", **{"data.txt": _MARKER})
    secret = _project(tmp_path / "s", **{"token": _MARKER}) / "token"
    assert "data.txt" in _refused("buildx_build", context=str(context), build_contexts={"extra": str(extra)})
    assert "token" in _refused("buildx_build", context=str(context), secret=[f"id=t,src={secret}"])


def test_a_remote_build_context_is_not_searched():
    _runs("buildx_build", context="https://github.com/example/repo.git")


def test_compose_cp_searches_a_local_source(tmp_path):
    source = _project(tmp_path, **{"copy__me.txt": _MARKER}) / "copy"
    assert "me.txt" in _refused("compose_cp", project_dir=str(tmp_path), source=str(source))


def test_a_marker_split_across_two_reads_is_found(tmp_path, monkeypatch):
    monkeypatch.setattr(disk, "_CHUNK", 16)
    project = _project(tmp_path, **{"compose.yaml": "x" * 29 + _MARKER})
    assert "compose.yaml" in _refused("compose_up", project_dir=str(project))


def test_a_symlink_is_not_followed(tmp_path):
    outside = _project(tmp_path / "outside", **{"big.txt": _MARKER})
    project = _project(tmp_path / "project", **{"compose.yaml": "services: {}\n"})
    (project / "link").symlink_to(outside, target_is_directory=True)
    _runs("compose_up", project_dir=str(project))


def test_past_the_search_limits_the_call_is_refused_rather_than_run_unsearched(tmp_path, monkeypatch):
    monkeypatch.setattr(disk, "MAX_SEARCH_FILES", 2)
    project = _project(tmp_path, **{"compose.yaml": "services: {}\n", "a": "1", "b": "2"})
    message = _refused("compose_up", project_dir=str(project))
    assert "past the limit of the search" in message


def test_the_byte_limit_counts_what_is_read_not_the_size_a_file_reports(tmp_path, monkeypatch):
    # A procfs file reports a size of 0 and a log being written keeps growing; both read past their size.
    project = _project(tmp_path, **{"compose.yaml": "small"})
    monkeypatch.setattr(disk, "MAX_SEARCH_BYTES", 1000)
    monkeypatch.setattr(disk, "_CHUNK", 100)
    real_open = Path.open

    def open_growing(self, *args, **kwargs):
        if self.name == "compose.yaml":
            return io.BytesIO(b"x" * 10_000)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_growing)
    assert "past the limit of the search" in _refused("compose_up", project_dir=str(project))


def test_the_search_limits_are_the_ssh_fallbacks_staging_limits():
    from docker_mcp.tools import _ssh_proxy

    assert (disk.MAX_SEARCH_FILES, disk.MAX_SEARCH_BYTES) == (_ssh_proxy._MAX_STAGE_FILES, _ssh_proxy._MAX_STAGE_BYTES)


def test_files_are_searched_only_for_a_write_to_a_redacted_host_that_accepts_writes(tmp_path):
    project = str(_project(tmp_path, **{"compose.yaml": _MARKER}))
    _runs("compose_up", host="dev", project_dir=project)
    _runs("compose_ps", writes=False, project_dir=project)
    _runs("compose_up", host="stage", project_dir=project)  # the host guard refuses it there


def test_no_swapped_value_or_file_contents_appear_in_the_refusal(tmp_path):
    project = _project(tmp_path, **{"compose.yaml": f"PW={_SECRET} {_MARKER}"})
    assert _SECRET not in _refused("compose_up", project_dir=str(project))


# ---------- archives ----------


def _tar(**members: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, text in members.items():
            payload = text.encode()
            info = tarfile.TarInfo(name.replace("__", "/"))
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def test_an_archive_in_band_holding_a_marker_is_refused_naming_the_member():
    message = _refused("container_archive_put", data=_tar(**{"readme.txt": "hi", "etc__app.conf": f"pw={_MARKER}"}))
    assert "member 'etc/app.conf' of the archive in `data`" in message


def test_an_archive_from_a_file_holding_a_marker_is_refused(tmp_path):
    archive = tmp_path / "upload.tar"
    archive.write_bytes(_tar(**{"app.conf": _MARKER}))
    message = _refused("container_archive_put", from_file=str(archive))
    assert "member 'app.conf'" in message and str(archive) in message


def test_a_compressed_archive_is_refused():
    assert "gzip-compressed" in _refused("container_archive_put", data=gzip.compress(_tar(**{"a": "b"})))


def test_a_clean_archive_is_uploaded():
    _runs("container_archive_put", data=_tar(**{"a": "b"}))


def test_an_archive_is_not_searched_on_an_unfiltered_host():
    _runs("container_archive_put", host="dev", data=_tar(**{"a": _MARKER}))


def test_archive_bytes_are_never_swapped_back():
    # A marker issued for prod inside the archive is refused, not swapped, even though it would resolve.
    redactor = wire.REDACTOR
    redactor.remember(_SECRET, "prod")
    marker = redactor.redact(f"x {_SECRET}", "prod").split(" ", 1)[1]
    assert "holds a [REDACTED:...] marker" in _refused("container_archive_put", data=_tar(**{"a": marker}))


# ---------- the table ----------

# Arguments that name a file or directory on the server host.
_PATH_ARGUMENTS = {
    "build_contexts",
    "compose_files",
    "config",
    "context",
    "cwd",
    "descriptor_files",
    "dest_path",
    "dockercfg_path",
    "dockerfile",
    "file",
    "files",
    "from_file",
    "project_dir",
    "secret",
    "source",
}


def test_every_write_tool_that_takes_a_path_is_searched_or_says_why_not():
    from docker_mcp.server import TOOL_CATEGORIES, ToolCategory, mcp

    unaccounted = []
    for name, registered in mcp._tool_manager._tools.items():
        if TOOL_CATEGORIES.get(name, ToolCategory.MUTATING) is ToolCategory.READ_ONLY or name in wire.NO_DAEMON_TOOLS:
            continue
        if not _PATH_ARGUMENTS & set(inspect.signature(registered.fn).parameters):
            continue
        if name not in disk.FILE_INPUTS and not name.startswith("compose_") and name not in disk.NOT_SEARCHED:
            unaccounted.append(name)
    assert not unaccounted, f"Add these to FILE_INPUTS or NOT_SEARCHED in docker_mcp/redaction/disk.py: {unaccounted}"
