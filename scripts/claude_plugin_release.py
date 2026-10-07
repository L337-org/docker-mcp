#!/usr/bin/env python3
"""Build, check and publish the Claude plugin's release branch.

Anthropic's plugin directory follows the `claude-plugin-release` branch.  Each release adds one
commit to it, generated here, holding only `claude-plugin/` with a `uv.lock` that pins the released
server and every dependency by hash.  The branch only ever fast-forwards and its history cannot be
rewritten, so it is also the record of what was published.  A rollback is a signed `git revert` on
the branch, made by a person; `architecture/distribution.md` has the reasoning, the repository
rules this depends on, and the rollback procedure.

The release workflow and the pre-merge workflow both run this script, so what a pull request
rehearses is the code a release runs:

    build    copy claude-plugin/, lock it against the release and check the result
             --source pypi   the release as PyPI serves it (post-release)
             --source local  wheels built from the checkout (pre-merge; nothing is on PyPI yet)
    smoke    install the built plugin both ways it can be installed, start the server and check
             what it registers, with the plugin's settings applied
    publish  add the commit to the branch - or, with --dry-run, only decide

Every check fails the run rather than warning, and `publish` changes nothing until all of them have
passed in the jobs before it.  Standard library only, so CI runs it with a bare Python.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIRNAME = "claude-plugin"
PACKAGE = "docker-mcp-server"
DEFAULT_BRANCH = "claude-plugin-release"
PYPI_INDEX = "https://pypi.org/simple"
API = "https://api.github.com"
DEFAULT_REPO = "L337-org/docker-mcp"

# The directory's own limits (claude.com/docs/plugins/pre-submission-checklist): a non-image file
# over 256 KiB or a plugin over 512 files is held for review rather than read.
MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 512
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
# Caps on what this script reads from the network or a child process, so nothing external can make
# it buffer without bound.
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_MCP_LINE_BYTES = 16 * 1024 * 1024
HTTP_TIMEOUT = 60
# How far back publish reads the branch's history, at one release per commit plus any reverts.
MAX_HISTORY_COMMITS = 1000


class ReleaseError(Exception):
    """A check failed or an operation was refused; the message says which and why."""


# --------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------


def log(message: str) -> None:
    """Print a progress line, flushed so CI shows it in order with child-process output.

    Args:
        message: the line to print
    """
    print(message, flush=True)


def version_key(version: str) -> tuple[int, ...]:
    """Order release versions numerically; only plain dotted integers are releases here.

    Args:
        version: a release version such as ``2.2.7``

    Returns:
        tuple: the version's integer parts

    Raises:
        ReleaseError: if the version is not plain dotted integers
    """
    if not re.fullmatch(r"\d+(\.\d+)*", version):
        raise ReleaseError(f"version {version!r} is not a plain dotted release version")
    return tuple(int(part) for part in version.split("."))


def run(argv: list[str], *, timeout: int, env: dict[str, str] | None = None, cwd: Path | None = None) -> str:
    """Run a command without a shell, raising with its own output when it fails.

    Args:
        argv: the command and its arguments; the program is resolved on PATH
        timeout: seconds before the command is killed
        env: the child environment (defaults to this process's)
        cwd: working directory

    Returns:
        str: the command's combined stdout and stderr

    Raises:
        ReleaseError: if the program is missing, times out or exits non-zero
    """
    program = shutil.which(argv[0], path=(env or os.environ).get("PATH"))
    if program is None:
        raise ReleaseError(f"{argv[0]!r} is not on PATH; it is needed to run {' '.join(argv)!r}")
    try:
        proc = subprocess.run(  # noqa: S603  (resolved program, argv list, no shell)
            [program, *argv[1:]],
            capture_output=True,
            timeout=timeout,
            env=env,
            cwd=cwd,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReleaseError(f"{' '.join(argv)!r} did not finish within {timeout}s") from exc
    output = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise ReleaseError(f"{' '.join(argv)!r} exited {proc.returncode}:\n{output.strip()}")
    return output


def http(
    method: str, url: str, *, token: str | None = None, body: dict | None = None, raw: bool = False
) -> tuple[int, object]:
    """Make one HTTPS request with a timeout and a size cap.

    Args:
        method: HTTP method
        url: absolute https URL
        token: bearer token for the GitHub API, if any
        body: JSON request body, if any
        raw: return the body as bytes rather than parsed JSON

    Returns:
        tuple: (status, parsed JSON or bytes); 404 is returned rather than raised, since callers
        use it to mean "does not exist"

    Raises:
        ReleaseError: on any other HTTP error, with the server's message, or an oversized body
    """
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)  # noqa: S310  (https URLs built here)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "docker-mcp-claude-plugin-release")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # noqa: S310
            content = response.read(MAX_DOWNLOAD_BYTES + 1)
            status = response.status
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 404, None
        detail = exc.read(64 * 1024).decode("utf-8", errors="replace")
        raise ReleaseError(f"{method} {url} failed with HTTP {exc.code}: {detail.strip()}") from exc
    except urllib.error.URLError as exc:
        raise ReleaseError(f"{method} {url} failed: {exc.reason}") from exc
    if len(content) > MAX_DOWNLOAD_BYTES:
        raise ReleaseError(f"{method} {url} returned more than {MAX_DOWNLOAD_BYTES} bytes; refusing to read it")
    if raw:
        return status, content
    return status, (json.loads(content) if content else None)


def expect_dict(value: object, what: str) -> dict:
    """Narrow a parsed JSON value to an object, failing with what was expected.

    Args:
        value: the parsed value
        what: what it should have been, for the error

    Returns:
        dict: the value

    Raises:
        ReleaseError: if it is not a JSON object
    """
    if not isinstance(value, dict):
        raise ReleaseError(f"expected {what} to be a JSON object, got {type(value).__name__}")
    return value


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file.

    Args:
        path: the file

    Returns:
        str: its hex digest
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------------------------
# Git object hashing, so a tree read back from GitHub can be compared with the folder we tested
# --------------------------------------------------------------------------------------------


def git_blob_sha(data: bytes) -> str:
    """The git object id of a blob with these bytes.

    Args:
        data: the file contents

    Returns:
        str: the 40-hex blob id
    """
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def git_tree_sha(files: dict[str, bytes]) -> str:
    """The git tree id of a folder of regular (mode 100644) files, keyed by relative path.

    Git orders tree entries by name with a subtree compared as if its name ended in "/", so this
    produces the same id as `git write-tree` over the same files.

    Args:
        files: relative POSIX path -> contents

    Returns:
        str: the 40-hex tree id
    """
    children: dict[str, dict[str, bytes]] = {}
    entries: list[tuple[str, bytes]] = []
    for path, data in files.items():
        head, _, rest = path.partition("/")
        if rest:
            children.setdefault(head, {})[rest] = data
        else:
            entries.append((head, b"100644 " + head.encode() + b"\0" + bytes.fromhex(git_blob_sha(data))))
    for name, sub in children.items():
        entries.append((name + "/", b"40000 " + name.encode() + b"\0" + bytes.fromhex(git_tree_sha(sub))))
    body = b"".join(entry for _, entry in sorted(entries, key=lambda e: e[0].encode()))
    return hashlib.sha1(b"tree %d\0" % len(body) + body, usedforsecurity=False).hexdigest()


def read_folder(root: Path) -> dict[str, bytes]:
    """Every file under `root`, keyed by POSIX path relative to `root`'s parent.

    Args:
        root: the plugin folder

    Returns:
        dict: relative path (starting with the folder's own name) -> contents

    Raises:
        ReleaseError: if the folder contains a symlink, which the directory refuses to load
    """
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ReleaseError(f"{path} is a symlink; the plugin directory blocks symlinks it loads")
        if path.is_file():
            files[path.relative_to(root.parent).as_posix()] = path.read_bytes()
    return files


# --------------------------------------------------------------------------------------------
# Static checks on a plugin folder
# --------------------------------------------------------------------------------------------


def check_plugin_folder(plugin: Path, version: str) -> None:
    """Check a built plugin folder against the directory's limits and our own invariants.

    Args:
        plugin: the plugin folder
        version: the release it must describe

    Raises:
        ReleaseError: naming every check that failed
    """
    problems: list[str] = []
    files = read_folder(plugin)
    if len(files) > MAX_FILES:
        problems.append(f"{len(files)} files; the directory reads at most {MAX_FILES}")
    for rel, data in files.items():
        if Path(rel).suffix.lower() not in IMAGE_SUFFIXES and len(data) > MAX_FILE_BYTES:
            problems.append(f"{rel} is {len(data)} bytes; the directory reads files up to {MAX_FILE_BYTES}")
        if Path(rel).name in {".DS_Store", "Thumbs.db", "desktop.ini", "CLAUDE.md"}:
            problems.append(f"{rel} must not be in the plugin folder")

    manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    if manifest.get("version") != version:
        problems.append(f"plugin.json version {manifest.get('version')!r} != {version!r}")
    server = manifest.get("mcpServers", {}).get(PACKAGE, {})
    if server.get("command") != "uvx" or server.get("args") != [f"{PACKAGE}=={version}"]:
        problems.append(
            f"plugin.json must launch `uvx {PACKAGE}=={version}` (the only form the locked launch "
            f"supports); it has {server.get('command')!r} {server.get('args')!r}"
        )
    pyproject = tomllib.loads((plugin / "pyproject.toml").read_text(encoding="utf-8"))
    if pyproject["project"].get("dependencies") != [f"{PACKAGE}=={version}"]:
        problems.append(f"pyproject.toml must depend on exactly {PACKAGE}=={version}")

    readme = (plugin / "README.md").read_text(encoding="utf-8")
    prose = re.sub(r"```.*?```", "", readme, flags=re.DOTALL)
    if len(prose.split()) < 40:
        problems.append("README.md has fewer than 40 words outside code blocks")
    if "icon.png" in readme:
        problems.append("README.md names icon.png, which holds the version for review")
    if problems:
        raise ReleaseError("plugin folder checks failed:\n  " + "\n  ".join(problems))


def check_lock(plugin: Path, version: str, pypi_hashes: set[str] | None) -> None:
    """Check `uv.lock` pins this release and every dependency exactly, from PyPI, with hashes.

    That is what the directory's "Launcher locked" check requires, so a lock that fails here would
    lose the badge eligibility the lock exists for.

    Args:
        plugin: the plugin folder holding uv.lock
        version: the release the lock must pin
        pypi_hashes: the sha256 digests PyPI serves for this release, or None when the release was
            built locally (pre-merge) and is not on PyPI, in which case the server's own entry is
            exempt from the registry check - and the caller is told so

    Raises:
        ReleaseError: naming every entry that failed
    """
    lock_path = plugin / "uv.lock"
    size = lock_path.stat().st_size
    problems = (
        [] if size <= MAX_FILE_BYTES else [f"uv.lock is {size} bytes; the directory reads up to {MAX_FILE_BYTES}"]
    )
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    found = False
    for package in lock.get("package", []):
        name, source = package["name"], package.get("source", {})
        if source.get("virtual") or source.get("editable"):
            continue  # the plugin's own launch project, which is not installed
        artefacts = [a for a in [package.get("sdist"), *package.get("wheels", [])] if a]
        is_server = name == PACKAGE
        if is_server:
            found = True
            if package.get("version") != version:
                problems.append(f"uv.lock pins {PACKAGE} {package.get('version')!r}, not {version!r}")
            if pypi_hashes is None:
                continue
            locked = {a.get("hash", "").removeprefix("sha256:") for a in artefacts}
            if locked != pypi_hashes:
                problems.append(f"uv.lock's {PACKAGE} hashes do not match what PyPI serves for {version}")
        if source.get("registry") != PYPI_INDEX:
            problems.append(f"{name} comes from {source!r}, not {PYPI_INDEX}")
        if not artefacts or not all(str(a.get("hash", "")).startswith("sha256:") for a in artefacts):
            problems.append(f"{name} has an artefact without a sha256 hash")
    if not found:
        problems.append(f"uv.lock has no {PACKAGE} entry")
    if problems:
        raise ReleaseError("uv.lock checks failed:\n  " + "\n  ".join(problems))


# --------------------------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------------------------


def uv_env(cache: Path) -> dict[str, str]:
    """Environment for `uv` children: the parent's, minus anything that could redirect an index.

    Args:
        cache: the uv cache directory to use, so a run starts from nothing it did not download

    Returns:
        dict: the child environment
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(("UV_", "PIP_"))}
    env["UV_CACHE_DIR"] = str(cache)
    env["UV_NO_CONFIG"] = "1"
    return env


def pypi_release_files(version: str, workdir: Path, expect_dist: Path | None) -> set[str]:
    """Download this release's files from PyPI and check them.

    Args:
        version: the release
        workdir: where to put the downloads
        expect_dist: the files this run's pypi job built and uploaded, or None when it uploaded
            nothing (a re-run), since a rebuild with a newer build backend can differ

    Returns:
        set: the sha256 digests PyPI serves for the release

    Raises:
        ReleaseError: if PyPI lacks a wheel or sdist, a download does not match PyPI's digest, or a
            file differs from what the release job built
    """
    status, info = http("GET", f"https://pypi.org/pypi/{PACKAGE}/{version}/json")
    if status == 404 or not isinstance(info, dict):
        raise ReleaseError(f"PyPI has no {PACKAGE} {version}; the PyPI publish must succeed first")
    urls = info.get("urls", [])
    kinds = {u.get("packagetype") for u in urls}
    if not {"bdist_wheel", "sdist"} <= kinds:
        raise ReleaseError(f"PyPI's {PACKAGE} {version} lacks a wheel or an sdist (has {sorted(kinds)})")
    digests: set[str] = set()
    for entry in urls:
        name, expected = entry["filename"], entry["digests"]["sha256"]
        _, content = http("GET", entry["url"], raw=True)
        if not isinstance(content, bytes):
            raise ReleaseError(f"downloading {name} from PyPI returned nothing")
        target = workdir / name
        target.write_bytes(content)
        if sha256_file(target) != expected:
            raise ReleaseError(f"{name} downloaded from PyPI does not match PyPI's own sha256 {expected}")
        if expect_dist is not None:
            built = expect_dist / name
            if not built.is_file():
                raise ReleaseError(f"PyPI serves {name}, which the release job did not build (looked in {expect_dist})")
            if sha256_file(built) != expected:
                raise ReleaseError(
                    f"PyPI's {name} differs from the file this run's pypi job built and uploaded ({built}); "
                    "PyPI is not serving what was uploaded, so investigate before re-running"
                )
        digests.add(expected)
        log(f"  {name}: sha256 {expected} matches PyPI" + (" and the release build" if expect_dist else ""))
    return digests


def build(args: argparse.Namespace) -> None:
    """Assemble the plugin folder for a release, lock it, and check everything about it.

    Args:
        args: parsed command line

    Raises:
        ReleaseError: if any step or check fails
    """
    version, out = args.version, Path(args.out).resolve()
    if out.exists():
        raise ReleaseError(f"{out} already exists; build into a fresh directory")
    plugin = out / PLUGIN_DIRNAME
    source_root = Path(args.source_root).resolve()
    shutil.copytree(source_root / PLUGIN_DIRNAME, plugin, ignore=shutil.ignore_patterns("uv.lock", ".DS_Store"))
    check_plugin_folder(plugin, version)

    root = tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))
    plugin_pyproject = tomllib.loads((plugin / "pyproject.toml").read_text(encoding="utf-8"))
    root_constraints = root.get("tool", {}).get("uv", {}).get("constraint-dependencies", [])
    if plugin_pyproject.get("tool", {}).get("uv", {}).get("constraint-dependencies", []) != root_constraints:
        raise ReleaseError(
            "claude-plugin/pyproject.toml's constraint-dependencies differ from the root pyproject.toml's"
        )

    cache = out / ".uv-cache"
    # Run from the plugin folder so a local wheel source is recorded as the relative "../dist".
    lock_argv = ["uv", "lock", "--default-index", PYPI_INDEX]
    if args.source == "pypi":
        log(f"== checking {PACKAGE} {version} on PyPI")
        downloads = out / "pypi"
        downloads.mkdir()
        pypi_hashes = pypi_release_files(version, downloads, Path(args.expect_dist) if args.expect_dist else None)
    else:
        # The wheels are copied into the build so the lock's local source travels with it to the
        # smoke jobs on other runners, at the same relative path.
        dist = out / "dist"
        shutil.copytree(Path(args.dist).resolve(), dist)
        wheels = sorted(dist.glob(f"{PACKAGE.replace('-', '_')}-{version}-*.whl"))
        if not wheels:
            raise ReleaseError(f"no {PACKAGE} {version} wheel in {args.dist}; build one with `uv build`")
        log(f"== using the locally built {wheels[0].name}; the server's lock entry is not checked against PyPI")
        lock_argv += ["--find-links", "../dist"]
        pypi_hashes = None

    log("== locking")
    log(run(lock_argv, timeout=600, env=uv_env(cache), cwd=plugin).strip())
    check_lock(plugin, version, pypi_hashes)
    log(f"  uv.lock: {PACKAGE} {version} and every dependency pinned with sha256 hashes")
    if pypi_hashes is None:
        # A lock against a local wheel records a local source; it is a rehearsal artefact only.
        log("  NOTE: this lock names a local wheel and is for the pre-merge rehearsal, never for publishing")

    log("== claude plugin validate --strict")
    log(run(["claude", "plugin", "validate", "--strict", str(plugin)], timeout=300).strip())
    shutil.rmtree(cache, ignore_errors=True)
    tree = git_tree_sha(read_folder(plugin))
    write_build_record(out, version, args.source, tree)
    log(f"built {plugin} (tree {tree})")


def read_build_record(build_dir: Path) -> dict:
    """Read what a build directory holds.

    Args:
        build_dir: a directory written by build

    Returns:
        dict: version, source (pypi or local) and tree

    Raises:
        ReleaseError: if it has no build record
    """
    path = build_dir / "build.json"
    if not path.is_file():
        raise ReleaseError(f"{build_dir} has no build.json; run build into it first")
    return json.loads(path.read_text(encoding="utf-8"))


def write_build_record(out: Path, version: str, source: str, tree: str) -> None:
    """Record what a build directory holds, for the jobs after it.

    Args:
        out: the build directory
        version: the release
        source: pypi or local
        tree: the plugin folder's git tree id
    """
    (out / "build.json").write_text(json.dumps({"version": version, "source": source, "tree": tree}, indent=2))


# --------------------------------------------------------------------------------------------
# smoke: install the plugin and talk MCP to it
# --------------------------------------------------------------------------------------------


@dataclass
class McpResult:
    """What one MCP session established about the server."""

    tools: list[dict]
    ping_ok: bool | None


def plugin_env(plugin: Path, overrides: dict[str, object]) -> dict[str, str]:
    """The env the plugin's server entry produces, with `${user_config.KEY}` filled in.

    Claude Code substitutes a user's settings, or the option defaults, into the entry's `env`;
    booleans arrive as "true"/"false".  This reproduces that so the smoke test drives the server
    through the same mapping a user's settings take.

    Args:
        plugin: the plugin folder
        overrides: option key -> value standing in for a user's setting

    Returns:
        dict: env var -> value

    Raises:
        ReleaseError: if the entry references an option the manifest does not declare
    """
    manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    options = manifest.get("userConfig", {})
    values = {key: overrides.get(key, opt.get("default", "")) for key, opt in options.items()}

    def fill(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise ReleaseError(f"plugin.json env references undeclared option {key!r}")
        value = values[key]
        return ("true" if value else "false") if isinstance(value, bool) else str(value)

    env = manifest["mcpServers"][PACKAGE].get("env", {})
    return {name: re.sub(r"\$\{user_config\.(\w+)\}", fill, template) for name, template in env.items()}


def mcp_session(argv: list[str], env: dict[str, str], *, ping: bool, timeout: int) -> McpResult:
    """Start the server over stdio, initialise, list every tool, optionally ping the daemon.

    Args:
        argv: the server command
        env: the server's environment
        ping: call system_ping and record whether it succeeded
        timeout: wall-clock seconds for the whole session, first-run downloads included

    Returns:
        McpResult: the tools and the ping outcome

    Raises:
        ReleaseError: if the server fails to start, answers with an error, or runs out of time
    """
    program = shutil.which(argv[0], path=env.get("PATH"))
    if program is None:
        raise ReleaseError(f"{argv[0]!r} is not on PATH")
    stderr = tempfile.TemporaryFile()
    proc = subprocess.Popen(  # noqa: S603  (resolved program, argv list, no shell)
        [program, *argv[1:]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, env=env
    )
    lines: queue.Queue[bytes | None] = queue.Queue()

    stdout, stdin = proc.stdout, proc.stdin
    if stdout is None or stdin is None:
        raise ReleaseError(f"{' '.join(argv)!r}: no stdio pipes")

    def reader() -> None:
        while True:
            line = stdout.readline(MAX_MCP_LINE_BYTES + 1)
            lines.put(line or None)
            if not line:
                return

    threading.Thread(target=reader, daemon=True).start()
    deadline = time.monotonic() + timeout
    next_id = 0

    def stderr_tail() -> str:
        stderr.seek(0)
        return stderr.read()[-4000:].decode("utf-8", errors="replace").strip()

    def send(message: dict) -> None:
        stdin.write(json.dumps(message).encode() + b"\n")
        stdin.flush()

    def request(method: str, params: dict) -> dict:
        nonlocal next_id
        next_id += 1
        send({"jsonrpc": "2.0", "id": next_id, "method": method, "params": params})
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReleaseError(f"{' '.join(argv)!r}: no answer to {method} within {timeout}s\n{stderr_tail()}")
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise ReleaseError(f"{' '.join(argv)!r} exited before answering {method}:\n{stderr_tail()}")
            if len(line) > MAX_MCP_LINE_BYTES:
                raise ReleaseError(f"{method}: a response line exceeded {MAX_MCP_LINE_BYTES} bytes")
            message = json.loads(line)
            if message.get("id") != next_id:
                continue  # a notification or log message
            if "error" in message:
                raise ReleaseError(f"{method} returned an error: {message['error']}")
            return message["result"]

    try:
        request(
            "initialize",
            {
                "protocolVersion": "2026-07-28",
                "capabilities": {},
                "clientInfo": {"name": "claude-plugin-release-smoke", "version": "1"},
            },
        )
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools: list[dict] = []
        cursor: str | None = None
        while True:
            page = request("tools/list", {"cursor": cursor} if cursor else {})
            tools += page.get("tools", [])
            cursor = page.get("nextCursor")
            if not cursor:
                break
        ping_ok = None
        if ping:
            result = request("tools/call", {"name": "system_ping", "arguments": {}})
            ping_ok = not result.get("isError", False)
            if not ping_ok:
                log(f"  system_ping failed: {json.dumps(result)[:2000]}")
        return McpResult(tools=tools, ping_ok=ping_ok)
    finally:
        proc.kill()
        proc.wait(timeout=30)
        stderr.close()


def child_env(base: dict[str, str]) -> dict[str, str]:
    """An allow-listed environment for the server: what uv, Python and Docker need, and nothing else.

    Args:
        base: the uv environment (cache location etc.) to start from

    Returns:
        dict: the child environment
    """
    keep = {
        "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
        "TEMP", "TMP", "TMPDIR", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "XDG_CACHE_HOME",
        "XDG_DATA_HOME", "DOCKER_HOST", "DOCKER_CONFIG", "UV_CACHE_DIR", "UV_NO_CONFIG",
        "UV_PYTHON_INSTALL_DIR", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "LANG", "LC_ALL",
    }  # fmt: skip
    return {k: v for k, v in base.items() if k in keep}


def smoke(args: argparse.Namespace) -> None:
    """Install the built plugin both ways and check the server it starts.

    The two installs are the lock (`uv sync --frozen`, every hash enforced), which is what the
    directory's locked launch installs, and the manifest's own `uvx` command, which is what a
    Claude Code that does not use the lock runs.  Both must report the release version and register
    the same tools; then the plugin's settings must change what is registered as they claim to.

    Args:
        args: parsed command line

    Raises:
        ReleaseError: on any failed install or check
    """
    build_dir = Path(args.build).resolve()
    record = read_build_record(build_dir)
    plugin = build_dir / PLUGIN_DIRNAME
    manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    if version != record["version"]:
        raise ReleaseError(f"build.json says {record['version']} but the plugin declares {version}")
    with tempfile.TemporaryDirectory(prefix="plugin-smoke-") as tmp:
        work = Path(tmp)
        base = uv_env(work / "cache")
        base["UV_TOOL_DIR"] = str(work / "tools")
        base["UV_TOOL_BIN_DIR"] = str(work / "bin")
        local = record["source"] == "local"
        find_links = ["--find-links", str(build_dir / "dist")] if local else []

        log("== install 1: the lock, every hash enforced (uv sync --frozen)")
        venv = work / "venv"
        sync_env = dict(base, UV_PROJECT_ENVIRONMENT=str(venv))
        log(run(["uv", "sync", "--frozen", "--project", str(plugin), *find_links], timeout=900, env=sync_env).strip())
        bindir = venv / ("Scripts" if os.name == "nt" else "bin")
        locked_server = [str(bindir / (PACKAGE + (".exe" if os.name == "nt" else "")))]

        log("== install 2: the manifest's own command")
        server = manifest["mcpServers"][PACKAGE]
        manifest_server = [server["command"], *find_links, *server["args"]]
        if local:
            # Installing from a folder of wheels, uv cannot read the package's Python requirement
            # before choosing an interpreter, and may pick an older managed Python.  Against PyPI it
            # reads the requirement from the index, so the plain manifest command works; locally the
            # plugin's own requires-python is passed instead - the only way this differs from it.
            requires = tomllib.loads((plugin / "pyproject.toml").read_text(encoding="utf-8"))["project"][
                "requires-python"
            ]
            manifest_server[1:1] = ["--python", requires]
            log(f"  (local wheels: adding --python {requires!r} to the manifest command)")

        launches = {"lock": locked_server, "manifest": manifest_server}
        for label, argv in launches.items():
            reported = run([*argv, "--version"], timeout=900, env=child_env(base)).strip()
            if version not in reported.split():
                raise ReleaseError(f"{label} install reports {reported!r}, not {version}")
            log(f"  {label}: --version -> {reported}")

        counts: dict[str, int] = {}
        for label, argv in launches.items():
            env = dict(child_env(base), **plugin_env(plugin, {}))
            result = mcp_session(argv, env, ping=args.docker and label == "lock", timeout=900)
            counts[label] = len(result.tools)
            log(f"  {label}: {len(result.tools)} tools registered with the default settings")
            if result.ping_ok is False:
                raise ReleaseError(f"{label}: system_ping against the runner's Docker daemon failed")
            if result.ping_ok:
                log(f"  {label}: system_ping reached the runner's Docker daemon")
        if counts["lock"] != counts["manifest"] or counts["lock"] == 0:
            raise ReleaseError(f"the two installs register different tool counts: {counts}")
        full = counts["lock"]

        log("== the plugin's settings, through its env mapping")
        env = dict(child_env(base), **plugin_env(plugin, {"read_only": True}))
        tools = mcp_session(locked_server, env, ping=False, timeout=300).tools
        writable = [t["name"] for t in tools if not t.get("annotations", {}).get("readOnlyHint")]
        if not tools or len(tools) >= full or writable:
            raise ReleaseError(f"read-only setting: {len(tools)} of {full} tools, non-read-only: {writable[:10]}")
        log(f"  read-only: {len(tools)} of {full} tools, all read-only")

        env = dict(child_env(base), **plugin_env(plugin, {"disable_domains": "swarm"}))
        tools = mcp_session(locked_server, env, ping=False, timeout=300).tools
        swarm = [t["name"] for t in tools if t["name"].startswith("swarm_")]
        if len(tools) >= full or swarm:
            raise ReleaseError(f"disabling swarm: {len(tools)} of {full} tools, swarm tools left: {swarm}")
        log(f"  disabled domains = swarm: {len(tools)} of {full} tools, no swarm_ tools")
    log(f"smoke passed for {PACKAGE} {version}")


# --------------------------------------------------------------------------------------------
# GitHub: decide, publish
# --------------------------------------------------------------------------------------------


@dataclass
class BranchState:
    """What the release branch holds now, and every version it has ever held."""

    head: str | None
    head_version: str | None
    head_tree: str | None
    published: list[str]


@dataclass
class Decision:
    """What publish will do, and why."""

    action: str  # "create" or "noop"
    reason: str


def decide(version: str, local_tree: str, state: BranchState) -> Decision:
    """Decide what publishing this version should do, refusing anything that would go backwards.

    A pure function of the branch's state so every case is unit tested.  A version is published at
    most once: after a revert has taken the branch back to an earlier release, a re-run of the
    reverted release is refused, so the revert sticks until a newer release replaces it.

    Args:
        version: the release being published
        local_tree: the tree id of the folder built and tested for this release
        state: the branch as it is now

    Returns:
        Decision: the action and its reason

    Raises:
        ReleaseError: when the request is refused
    """
    if state.head is None:
        return Decision("create", f"publishing {version} as the branch's first commit")
    if state.head_version == version:
        if state.head_tree == local_tree:
            return Decision("noop", f"{version} is already published and the branch holds this build")
        return Decision(
            "noop",
            f"{version} is already published; this run's build (tree {local_tree}) differs from the published "
            f"one ({state.head_tree}), most likely a re-lock picking up newer dependencies, and is not published",
        )
    if version in state.published:
        raise ReleaseError(
            f"refusing to publish {version}: it was published before and the branch has since moved to "
            f"{state.head_version}, by a newer release or a revert. A version is published once; see the "
            "rollback procedure in architecture/distribution.md."
        )
    if state.head_version is not None and version_key(version) < version_key(state.head_version):
        raise ReleaseError(
            f"refusing to publish {version}: the branch already holds {state.head_version}, which is newer"
        )
    over = state.head_version or f"{state.head}, which has no readable plugin.json"
    return Decision("create", f"publishing {version} over {over}")


class GitHub:
    """The few GitHub REST calls publishing needs, against one repository."""

    def __init__(self, repo: str, token: str | None) -> None:
        """Bind to a repository.

        Args:
            repo: owner/name
            token: a token with contents: write (the workflow's own), or any token for a dry run
        """
        self.repo, self.token = repo, token

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        """One API call relative to the repository.

        Args:
            method: HTTP method
            path: path under /repos/<repo>/
            body: JSON body, if any

        Returns:
            tuple: (status, parsed JSON or None for a 404)
        """
        return http(method, f"{API}/repos/{self.repo}/{path}", token=self.token, body=body)

    def ref(self, ref: str) -> str | None:
        """The commit a ref points at, or None if it does not exist.

        Args:
            ref: e.g. ``heads/claude-plugin-release``

        Returns:
            str or None: the commit id
        """
        status, data = self.call("GET", f"git/ref/{ref}")
        return None if status == 404 else expect_dict(data, f"ref {ref}")["object"]["sha"]

    def commit(self, sha: str) -> dict:
        """A commit's git data (tree, parents) plus its signature verification.

        Args:
            sha: commit id

        Returns:
            dict: ``{"tree": ..., "parents": [...], "verified": bool, "reason": str}``

        Raises:
            ReleaseError: if the commit does not exist
        """
        status, data = self.call("GET", f"git/commits/{sha}")
        if status == 404:
            raise ReleaseError(f"commit {sha} does not exist in {self.repo}")
        data = expect_dict(data, f"commit {sha}")
        verification = data.get("verification", {})
        return {
            "tree": data["tree"]["sha"],
            "parents": [p["sha"] for p in data["parents"]],
            "verified": bool(verification.get("verified")),
            "reason": verification.get("reason", ""),
        }

    def plugin_version(self, commit: str) -> str | None:
        """The version the plugin declares at a commit.

        Args:
            commit: commit id

        Returns:
            str or None: the version, or None if the commit has no plugin.json
        """
        status, data = self.call("GET", f"contents/{PLUGIN_DIRNAME}/.claude-plugin/plugin.json?ref={commit}")
        if status == 404:
            return None
        return json.loads(base64.b64decode(expect_dict(data, "plugin.json")["content"]))["version"]

    def history(self, head: str) -> list[tuple[str, str]]:
        """Every commit reachable from `head`, newest first, with its tree.

        Args:
            head: the commit to start from

        Returns:
            list: (commit id, tree id) pairs

        Raises:
            ReleaseError: if there are more than MAX_HISTORY_COMMITS, rather than deciding on part of it
        """
        commits: list[tuple[str, str]] = []
        page = 1
        while True:
            _, data = self.call("GET", f"commits?sha={head}&per_page=100&page={page}")
            if not isinstance(data, list):
                raise ReleaseError(f"listing the history of {head} returned {type(data).__name__}, not a list")
            commits += [(c["sha"], c["commit"]["tree"]["sha"]) for c in data]
            if len(commits) > MAX_HISTORY_COMMITS:
                raise ReleaseError(
                    f"the release branch has more than {MAX_HISTORY_COMMITS} commits; raise MAX_HISTORY_COMMITS "
                    "so the check for an already-published version reads all of them"
                )
            if len(data) < 100:
                return commits
            page += 1

    def state(self, branch: str) -> BranchState:
        """Read the branch head and the version every commit on it declares.

        Args:
            branch: release branch name

        Returns:
            BranchState: what is there now
        """
        head = self.ref(f"heads/{branch}")
        if head is None:
            return BranchState(head=None, head_version=None, head_tree=None, published=[])
        history = self.history(head)
        # A revert restores an earlier tree, so many commits share a few trees; read each tree once.
        by_tree: dict[str, str | None] = {}
        for commit, tree in history:
            if tree not in by_tree:
                by_tree[tree] = self.plugin_version(commit)
        head_tree = history[0][1]
        published = [v for v in dict.fromkeys(by_tree[tree] for _, tree in history) if v is not None]
        return BranchState(head=head, head_version=by_tree[head_tree], head_tree=head_tree, published=published)


def publish(args: argparse.Namespace) -> None:
    """Add the release's plugin commit to the branch, as a fast-forward.

    Nothing is written until the commit is verified, its parent is the head that was read, and its
    tree is proven byte-identical to the folder the smoke jobs tested.  The branch then moves by
    fast-forward only, which GitHub refuses if the branch has moved since it was read, so two runs
    can never overwrite each other and a stale read of the branch fails rather than publishing.

    Args:
        args: parsed command line

    Raises:
        ReleaseError: if any check fails or the request is refused
    """
    repo = os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO)
    gh = GitHub(repo, os.environ.get("GH_TOKEN"))
    version, branch = args.version, args.branch
    build_dir = Path(args.build).resolve()
    record = read_build_record(build_dir)
    if record["version"] != version:
        raise ReleaseError(f"the build is of {record['version']}, not {version}")
    if record["source"] == "local" and not args.dry_run:
        raise ReleaseError("this build was locked against a local wheel; only a PyPI build can be published")
    check_plugin_folder(build_dir / PLUGIN_DIRNAME, version)
    local_files = read_folder(build_dir / PLUGIN_DIRNAME)
    local_tree = git_tree_sha(local_files)
    if local_tree != record["tree"]:
        raise ReleaseError(f"the build's files hash to {local_tree}, not the {record['tree']} that was tested")

    state = gh.state(branch)
    log(f"branch {branch}: {state.head or 'absent'} ({state.head_version or '-'}); published before: {state.published}")
    try:
        decision = decide(version, local_tree, state)
    except ReleaseError as exc:
        if not args.dry_run:
            raise
        # A rehearsal of a pull request that predates a newer release would be refused, rightly for
        # a real publish; for a rehearsal it is information, not a failure.
        log(f"dry run: publishing {version} now would be refused: {exc}")
        return
    log(f"decision: {decision.action} - {decision.reason}")
    if args.dry_run or decision.action == "noop":
        return

    entries = []
    for rel, data in local_files.items():
        _, blob = gh.call("POST", "git/blobs", {"content": base64.b64encode(data).decode(), "encoding": "base64"})
        sha = expect_dict(blob, f"blob for {rel}")["sha"]
        entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": sha})
    _, tree = gh.call("POST", "git/trees", {"tree": entries})
    message = (
        f"Claude plugin for {PACKAGE} {version}\n\n"
        f"Generated by the release workflow from {args.release_commit or 'an unrecorded commit'} "
        f"(release v{version}); holds only {PLUGIN_DIRNAME}/, locked against the release on PyPI.\n"
    )
    parents = [state.head] if state.head else []
    # No author, committer or signature fields: GitHub then signs the commit itself.
    body = {"message": message, "tree": expect_dict(tree, "the created tree")["sha"], "parents": parents}
    _, created = gh.call("POST", "git/commits", body)
    target = expect_dict(created, "the created commit")["sha"]

    info = gh.commit(target)
    if not info["verified"]:
        raise ReleaseError(f"commit {target} is not signed by GitHub ({info['reason']!r}); nothing was moved")
    if info["parents"] != parents or info["tree"] != local_tree:
        raise ReleaseError(
            f"commit {target} has parents {info['parents']} and tree {info['tree']}, not {parents} and the tested "
            f"folder's {local_tree}; nothing was moved"
        )
    log(f"commit {target}: verified ({info['reason']}), parent {state.head or '(none)'}, tree {info['tree']}")

    try:
        if state.head is None:
            _, moved = gh.call("POST", "git/refs", {"ref": f"refs/heads/{branch}", "sha": target})
        else:
            _, moved = gh.call("PATCH", f"git/refs/heads/{branch}", {"sha": target, "force": False})
    except ReleaseError as exc:
        raise ReleaseError(
            f"{branch} was not moved to {target}: it changed after it was read at {state.head or '(absent)'}, by "
            f"another release or a revert, or the repository's rules refused the update; re-run to decide again. "
            f"({exc})"
        ) from exc
    # The write's own response says where the branch now points; a separate read could be stale.
    now = expect_dict(moved, f"the updated {branch}")["object"]["sha"]
    if now != target:
        raise ReleaseError(f"moving {branch} to {target} reported it at {now}; check the branch")
    log(f"{branch} moved {state.head or '(new)'} -> {target}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"Claude plugin: `{branch}` -> `{target}` ({decision.reason})\n")


# --------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point.

    Args:
        argv: arguments (defaults to sys.argv)

    Returns:
        int: process exit status
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build", help="assemble, lock and check the plugin folder")
    p.add_argument("--version", required=True)
    p.add_argument("--source", choices=["pypi", "local"], required=True)
    p.add_argument("--out", required=True, help="a directory that does not exist yet")
    p.add_argument("--dist", help="local wheels (required with --source local)")
    p.add_argument("--expect-dist", help="the files this run's pypi job built and uploaded, to compare with PyPI's")
    p.add_argument(
        "--source-root",
        default=str(REPO_ROOT),
        help="the checkout whose claude-plugin/ and pyproject.toml to build (default: this script's own)",
    )
    p.set_defaults(func=build)

    p = sub.add_parser("smoke", help="install the built plugin and check the server")
    p.add_argument("--build", required=True, help="a directory written by build")
    p.add_argument("--docker", action="store_true", help="also call system_ping against the local daemon")
    p.set_defaults(func=smoke)

    p = sub.add_parser("publish", help="add the tested build to the release branch")
    p.add_argument("--version", required=True)
    p.add_argument("--branch", default=DEFAULT_BRANCH)
    p.add_argument("--build", required=True, help="a directory written by build")
    p.add_argument("--dry-run", action="store_true", help="decide and report, change nothing")
    p.add_argument("--release-commit", help="the release tag's commit, recorded in the plugin commit")
    p.set_defaults(func=publish)

    args = parser.parse_args(argv)
    if getattr(args, "source", None) == "local" and not args.dist:
        parser.error("--source local needs --dist")
    try:
        args.func(args)
    except ReleaseError as exc:
        print(f"::error::{exc}" if os.environ.get("GITHUB_ACTIONS") else f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
