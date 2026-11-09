#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = ["docker-mcp-server"]
#
# [tool.uv.sources]
# docker-mcp-server = { path = "../", editable = true }
# ///
r"""Regenerate the measured figures in `REDACTION.md`, and check the mode end to end with a real agent.

    uv run scripts/measure-redaction.py sweep --configuration all   # caught, missed, replaced by mistake
    uv run scripts/measure-redaction.py sweep --docker-host unix:///var/run/docker.sock
    uv run scripts/measure-redaction.py cost                        # time added per call, on a large daemon
    uv run scripts/measure-redaction.py agents                      # everyday requests to a real agent

By default each run starts a throwaway Docker-in-Docker daemon (`--dind-image`, privileged, on a
loopback port), measures against it and removes it, so nothing is left on the daemon you use.
`--docker-host` measures an existing daemon instead: the sweep then creates its containers, image
and Compose project there and removes them when it finishes, and its false-replacement count covers
whatever else that daemon holds, which is the point of running it against a real one.

REPORT ONLY, BY DESIGN, like `scripts/measure-comparison-figures.py`: it never edits `REDACTION.md`,
and it is never a CI gate, since timings vary by machine and the sweep needs a privileged daemon.
Correcting the document stays a reviewed edit informed by this output.

THE METHOD
==========
sweep: two server processes reach the same daemon, a redacted one configured as `--configuration`
says (per host or global, writable or read-only) and an unfiltered writable reference, and every call
is made on both through MCP, the path a client takes.  The reference output is what redaction is
judged against.  Each run first checks what the agent is told and offered in that configuration: the
instructions pointer, the host_list notes, and the tools withdrawn.  Random credentials of each kind
the design names are planted (environments, logs in raw, base64 and hex, an image-level variable, a
build argument, files in an image, a Compose `.env` value, a value after `--password` on a command
line, and with `--swarm` a service's environment and a Swarm secret printed to its logs), with seven
decoys that look like credentials but are not.  A
value counts as caught only if no raw, base64, hex or URL-encoded form of it appears anywhere in the
redacted output; each must appear in the unfiltered output, or the report says it never surfaced.
Each marker is located in the unfiltered output by the text either side of it and classed as a
planted value, a decoy or other content.  Other content is never printed: on a real daemon it may be
a real credential, so the report gives its length and the redacted text before it.  A value that
surfaced only through calls the redacted server refused or does not offer (exec, when read-only) is
reported as not reached rather than caught: redaction never saw it.

agents: a headless Claude Code with only this server's tools is given the everyday requests in
`_scenarios`, on a small planted app, once read-only and once writable.  Each is scored on whether it
was done (or declined, for a write when read-only), any planted value anywhere in its transcript, the
tool errors it met, and whether it blamed a correct password in triage.  It costs model usage, capped
per request by `--budget`.

cost (in one process, with a redacted and an unfiltered label on the same daemon): `--containers`
stopped containers, each with a credential in its environment, and `--images`
distinct images are created; the first redacted call (which harvests everything) is timed, then the
median of 15 calls of each tool on each label.
"""

import argparse
import asyncio
import contextlib
import base64
import io
import json
import os
import re
import secrets
import shutil
import statistics
import string
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MARKER = re.compile(r"\[REDACTED:[A-Za-z0-9_-]{22}\]")


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a program found on PATH, from an argument list, never through a shell.

    Args:
        argv: the program and its arguments
        **kwargs: passed to `subprocess.run`

    Returns:
        subprocess.CompletedProcess: the finished process

    Raises:
        SystemExit: the program is not on PATH
    """
    program = shutil.which(argv[0])
    if program is None:
        raise SystemExit(f"{argv[0]} is not on PATH")
    return subprocess.run([program, *argv[1:]], **kwargs)  # noqa: S603  (resolved program, argv list, no shell)


# --- the daemon ------------------------------------------------------------------------------------


class Daemon:
    """The daemon measured: an existing one, or a throwaway dind container removed on exit."""

    def __init__(self, docker_host: str | None, dind_image: str, port: int, swarm: bool) -> None:
        """Describe the daemon; nothing is started until the block is entered.

        Args:
            docker_host: an existing daemon's URL, or None for a throwaway dind one
            dind_image: the dind image for a throwaway daemon
            port: the loopback port a throwaway daemon listens on
            swarm: whether the sweep includes a Swarm service and secret
        """
        self.throwaway = docker_host is None
        self.name = f"dm-redaction-measure-{secrets.token_hex(3)}"
        self.url = docker_host or f"tcp://127.0.0.1:{port}"
        self.dind_image, self.port, self.swarm = dind_image, port, swarm

    def docker(self, *args: str, input: str | bytes | None = None, check: bool = True) -> str:
        """Run a docker CLI command against this daemon.

        Args:
            *args: the command's arguments
            input: what to send on stdin; bytes are sent as they are and the output is not decoded
            check: stop the run when the command fails

        Returns:
            str: its stripped standard output, or "" for bytes input

        Raises:
            SystemExit: the command failed and `check` is set
        """
        text = not isinstance(input, bytes)
        result = _run(["docker", "-H", self.url, *args], input=input, capture_output=True, text=text)
        if check and result.returncode != 0:
            raise SystemExit(f"docker {' '.join(args[:2])} failed: {result.stderr!s:.400}")
        return result.stdout.strip() if text else ""

    def __enter__(self) -> Daemon:
        """Start a throwaway daemon if there is to be one, and initialise Swarm when asked.

        Returns:
            Daemon: this daemon, ready

        Raises:
            SystemExit: the throwaway daemon did not start, or Swarm was asked of an existing non-manager
        """
        if self.throwaway:
            _run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--privileged",
                    "--name",
                    self.name,
                    "-e",
                    "DOCKER_TLS_CERTDIR=",
                    "-p",
                    f"127.0.0.1:{self.port}:2375",
                    self.dind_image,
                ],
                capture_output=True,
                check=True,
            )
            for _ in range(60):
                if _run(["docker", "-H", self.url, "version"], capture_output=True).returncode == 0:
                    break
                time.sleep(1)
            else:
                raise SystemExit(f"the throwaway daemon {self.name} did not start")
            self.docker("pull", "-q", "alpine:3")
        if self.swarm and self.docker("info", "--format", "{{.Swarm.LocalNodeState}}") != "active":
            if not self.throwaway:
                raise SystemExit("--swarm on an existing daemon needs it to be a Swarm manager already")
            self.docker("swarm", "init", "--advertise-addr", "127.0.0.1")
        return self

    def __exit__(self, *_: object) -> None:
        """Remove a throwaway daemon, whatever happened inside the block."""
        if self.throwaway:
            _run(["docker", "rm", "-f", self.name], capture_output=True)

    def version(self) -> str:
        """The daemon's engine and API versions, for the report.

        Returns:
            str: for example "Engine 28.5.2, API 1.51"
        """
        return self.docker("version", "--format", "Engine {{.Server.Version}}, API {{.Server.APIVersion}}")


def _server(url: str):
    os.environ["DOCKER_MCP_SERVER_HOSTS"] = f"redacted={url}(redact), unfiltered={url}"
    import docker_mcp.tools  # noqa: F401  - registers every tool
    from docker_mcp.redaction import wire
    from docker_mcp.server import mcp

    return mcp, wire


async def _call(mcp, name: str, arguments: dict, label: str) -> tuple[str, bool]:
    result = await mcp.call_tool(name, {**arguments, "host": label})
    return "\n".join(getattr(item, "text", "") for item in result.content), bool(result.is_error)


# --- sweep -------------------------------------------------------------------------------------------


def _random(n: int, alphabet: str = string.ascii_letters + string.digits) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _words() -> str:
    pool = ["sunflower", "meadow", "copper", "lantern", "harbour", "violet", "falcon", "pebble", "orchard"]
    return f"{secrets.choice(pool).capitalize()}{secrets.choice(pool)}{secrets.randbelow(90) + 10}"


def _planted(swarm: bool) -> dict[str, str]:
    jwt_parts = ('{"alg":"HS256","typ":"JWT"}', '{"sub":"' + _random(10) + '","iat":1700000000}')
    planted = {
        "credential-named env value": _random(32),
        "AWS access key ID": "AKIA" + _random(16, string.ascii_uppercase + string.digits),
        "AWS secret key": _random(40, string.ascii_letters + string.digits + "/+"),
        "GitHub token": "ghp_" + _random(36),
        "JWT under an innocuous name": ".".join(
            base64.urlsafe_b64encode(part.encode()).decode().rstrip("=") for part in jwt_parts
        )
        + "."
        + _random(43, string.ascii_letters + string.digits + "-_"),
        "URL password": _random(20),
        "word-like credential-named password": _words(),
        "Compose .env value": _words(),
        "image-level env value": _random(28),
        "random credential under an innocuous name": _random(40),
        "build argument": _random(24),
        "PEM private key": "",
        "credential-named random secret in a file": _random(36),
        "word-like secret in a file": _words(),
        "value after --password": _random(28),
        "value generated at container start": "",
    }
    if swarm:
        planted["service env value"] = _random(40)
        planted["Swarm secret printed to logs"] = _random(32)
    return planted


DECOYS = {
    "SORT_KEY": "created_at_desc_v2",
    "BUILD_ID": "build-" + _random(12, string.ascii_lowercase + string.digits),
    "GIT_COMMIT": secrets.token_hex(20),
    "REQUEST_ID": "6f1c2e9a-" + secrets.token_hex(2) + "-4c1b-9d2e-" + secrets.token_hex(6),
    "SRI_HASH": "sha384-" + base64.b64encode(secrets.token_bytes(48)).decode(),
    "SSH_PUBLIC_KEY": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + base64.b64encode(secrets.token_bytes(32)).decode()[:43],
    "CACHE_BUSTER": _random(24),
}


def _forms(value: str) -> set[str]:
    raw = value.encode()
    found = {value, base64.b64encode(raw).decode(), raw.hex(), urllib.parse.quote(value, safe="")}
    return {form for form in found if len(form) >= 6}


def _plant(daemon: Daemon, run: str, planted: dict[str, str], work: Path) -> Path:
    key = _run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    planted["PEM private key"] = "".join(key.strip().splitlines()[1:-1])[:64]
    image = work / "image"
    image.mkdir()
    (image / "key.pem").write_text(key)
    (image / "app.conf").write_text(
        f"token={planted['credential-named random secret in a file']}\n"
        f"passphrase {planted['word-like secret in a file']}\n"
    )
    (image / "Dockerfile").write_text(
        'FROM alpine:3\nARG BUILD_TOKEN\nRUN echo "token length ${#BUILD_TOKEN}" > /build-info\n'
        f"ENV IMG_SECRET_KEY={planted['image-level env value']}\n"
        "COPY app.conf /etc/app.conf\nCOPY key.pem /etc/key.pem\n"
    )
    daemon.docker(
        "build", "-q", "-t", f"{run}-image", "--build-arg", f"BUILD_TOKEN={planted['build argument']}", str(image)
    )
    environment = {
        "API_TOKEN": planted["credential-named env value"],
        "AWS_ACCESS_KEY_ID": planted["AWS access key ID"],
        "AWS_SECRET_ACCESS_KEY": planted["AWS secret key"],
        "GITHUB_TOKEN": planted["GitHub token"],
        "SESSION_BLOB": planted["JWT under an innocuous name"],
        "DATABASE_URL": f"postgres://app:{planted['URL password']}@db:5432/app",
        "DB_PASSWORD": planted["word-like credential-named password"],
        "FOO_SETTING": planted["random credential under an innocuous name"],
        **DECOYS,
    }
    script = (
        'echo "token $API_TOKEN"; echo "b64 $(printf %s "$API_TOKEN" | base64)"; '
        'echo "hex $(printf %s "$API_TOKEN" | od -An -tx1 | tr -d " \\n")"; '
        'echo "pw $DB_PASSWORD b64pw $(printf %s "$DB_PASSWORD" | base64)"; '
        'echo "aws $AWS_ACCESS_KEY_ID $AWS_SECRET_ACCESS_KEY gh $GITHUB_TOKEN jwt $SESSION_BLOB"; '
        'echo "db $DATABASE_URL foo $FOO_SETTING"; '
        'echo "generated $(head -c 30 /dev/urandom | base64 | tr -d \\"\\n\\")"; '
        'echo "decoys $SORT_KEY $BUILD_ID $GIT_COMMIT $REQUEST_ID $SRI_HASH $CACHE_BUSTER"; exec sleep 3600'
    )
    arguments = ["run", "-d", "--name", f"{run}-app"]
    for name, value in environment.items():
        arguments += ["-e", f"{name}={value}"]
    daemon.docker(*arguments, f"{run}-image", "sh", "-c", script)
    daemon.docker(
        "run",
        "-d",
        "--name",
        f"{run}-cli",
        "alpine:3",
        "sh",
        "-c",
        "while :; do sleep 60; done",
        "app",
        "--password",
        planted["value after --password"],
    )
    project = work / "project"
    project.mkdir()
    (project / ".env").write_text(f"DB_PASS={planted['Compose .env value']}\n")
    (project / "compose.yaml").write_text(
        "services:\n  db:\n    image: alpine:3\n    environment:\n      DB_PASS: ${DB_PASS}\n"
        '    command: ["sh", "-c", "echo dbpass $$DB_PASS; exec sleep 3600"]\n'
    )
    _run(["docker", "-H", daemon.url, "compose", "-p", run, "up", "-d"], cwd=project, capture_output=True, check=True)
    if daemon.swarm:
        daemon.docker("secret", "create", f"{run}-secret", "-", input=planted["Swarm secret printed to logs"])
        daemon.docker(
            "service",
            "create",
            "-d",
            "--name",
            f"{run}-service",
            "--secret",
            f"{run}-secret",
            "-e",
            f"SVC_TOKEN={planted['service env value']}",
            "alpine:3",
            "sh",
            "-c",
            f'cat /run/secrets/{run}-secret; echo; echo "svc $SVC_TOKEN"; exec sleep 3600',
        )
    time.sleep(8)
    generated = re.search(r"generated (\S+)", daemon.docker("logs", f"{run}-app"))
    if generated is None:
        raise SystemExit("the planted container did not log its generated value")
    planted["value generated at container start"] = generated.group(1)
    return project


def _remove_planted(daemon: Daemon, run: str, project: Path) -> None:
    daemon.docker("rm", "-f", f"{run}-app", f"{run}-cli", check=False)
    _run(["docker", "-H", daemon.url, "compose", "-p", run, "down"], cwd=project, capture_output=True)
    if daemon.swarm:
        daemon.docker("service", "rm", f"{run}-service", check=False)
        time.sleep(3)
        daemon.docker("secret", "rm", f"{run}-secret", check=False)
    daemon.docker("rmi", "-f", f"{run}-image", check=False)


def _replaced_spans(raw: str, redacted: str) -> list[tuple[str | None, str]]:
    """Each marker in the redacted output, located in the unfiltered output by the text either side of it.

    Args:
        raw: the unfiltered output
        redacted: the redacted output of the same call

    Returns:
        list: (the replaced text, or None where it could not be aligned; the redacted text before it)
    """
    spans = []
    markers = list(MARKER.finditer(redacted))
    for position, marker in enumerate(markers):
        left = redacted[max(markers[position - 1].end() if position else 0, marker.start() - 30) : marker.start()]
        stop = markers[position + 1].start() if position + 1 < len(markers) else len(redacted)
        right = redacted[marker.end() : min(stop, marker.end() + 30)]
        found, at = None, raw.find(left) if left else 0
        while at != -1:
            begin = at + len(left)
            end = raw.find(right, begin) if right else len(raw)
            if end != -1 and end - begin <= 20000:
                found = raw[begin:end]
                break
            at = raw.find(left, at + 1) if left else -1
        spans.append((found, redacted[max(0, marker.start() - 50) : marker.start()]))
    return spans


# The configurations a sweep can run.  Each is the redacted server's environment; the reference server
# always runs unfiltered and writable against the same daemon, so every difference is redaction's doing.
CONFIGURATIONS: dict[str, dict[str, str]] = {
    "host": {"DOCKER_MCP_SERVER_HOSTS": "d={url}(redact)"},
    "host-ro": {"DOCKER_MCP_SERVER_HOSTS": "d={url}(ro,redact)"},
    "global": {"DOCKER_MCP_SERVER_HOSTS": "d={url}", "DOCKER_MCP_SERVER_REDACT": "1"},
    "global-ro": {
        "DOCKER_MCP_SERVER_HOSTS": "d={url}",
        "DOCKER_MCP_SERVER_REDACT": "1",
        "DOCKER_MCP_SERVER_READONLY": "1",
    },
}
_SERVER_SWITCHES = ("DOCKER_MCP_SERVER_HOSTS", "DOCKER_MCP_SERVER_REDACT", "DOCKER_MCP_SERVER_READONLY")


@contextlib.asynccontextmanager
async def _session(url: str, overrides: dict[str, str]):
    """An MCP client session with a server process of its own, configured by `overrides`.

    Args:
        url: the daemon both servers reach
        overrides: the redaction and read-only switches for this server

    Yields:
        ClientSession: an initialised session, and the initialise result
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {key: value for key, value in os.environ.items() if key not in _SERVER_SWITCHES}
    env.update({key: value.format(url=url) for key, value in overrides.items()})
    params = StdioServerParameters(command=sys.executable, args=["-m", "docker_mcp"], env=env)
    with open(os.devnull, "w") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write), ClientSession(read, write) as session:
            yield session, await session.initialize()


async def _call_tool(session, name: str, arguments: dict) -> tuple[str, bool]:
    result = await session.call_tool(name, {**arguments, "host": "d"})
    return "\n".join(getattr(item, "text", "") for item in result.content), bool(result.is_error)


async def _surface(session, initialised, configuration: str) -> list[tuple[str, bool]]:
    """What the agent is told and offered in this configuration, checked against what it should be.

    Args:
        session: the redacted server's session
        initialised: its initialise result, which carries the instructions
        configuration: the configuration's name

    Returns:
        list: (expectation, whether it holds)
    """
    tools = {tool.name for tool in (await session.list_tools()).tools}
    listed = await session.call_tool("host_list", {})
    host = json.loads(getattr(listed.content[0], "text", "{}")) if listed.content else {}
    notes = " ".join((host.get("redaction") or {}).get("notes") or [])
    read_only = configuration.endswith("-ro")
    is_global = configuration.startswith("global")
    instructions = initialised.instructions or ""
    if read_only:
        notes_check = (
            "notes on a read-only host say a marker is not swapped back",
            "not swapped back" in notes and "Pass a marker" not in notes,
        )
    else:
        notes_check = ("notes on a writable host say a marker can be passed to a write", "Pass a marker" in notes)
    return [
        (
            "instructions point to host_list for redaction",
            "[REDACTED:...]" in instructions and "host_list" in instructions,
        ),
        ("host_list reports the host as redacted, with notes", bool(host.get("redact")) and "[REDACTED:...]" in notes),
        notes_check,
        (
            "container_archive_get is withdrawn under global redaction, kept otherwise",
            ("container_archive_get" not in tools) == is_global,
        ),
        (
            "container_exec is withdrawn under READONLY, kept otherwise",
            ("container_exec" not in tools) == (configuration == "global-ro"),
        ),
    ]


async def _sweep(daemon: Daemon, run: str, planted: dict[str, str], project: Path, configuration: str) -> None:
    from docker_mcp.redaction import rules

    since = str(int(time.time()) - 600)
    calls: list[tuple[str, dict]] = [
        ("container_list", {"all": True}),
        ("container_inspect", {"id_or_name": f"{run}-app"}),
        ("container_inspect", {"id_or_name": f"{run}-cli"}),
        ("container_logs", {"id_or_name": f"{run}-app"}),
        ("container_top", {"id_or_name": f"{run}-app"}),
        ("container_top", {"id_or_name": f"{run}-cli"}),
        ("container_exec", {"id_or_name": f"{run}-app", "cmd": ["env"]}),
        ("container_exec", {"id_or_name": f"{run}-app", "cmd": ["cat", "/etc/app.conf", "/etc/key.pem"]}),
        (
            "container_exec",
            {
                "id_or_name": f"{run}-cli",
                "cmd": ["sh", "-c", "true", "x", "--password", planted["value after --password"]],
            },
        ),
        ("image_list", {}),
        ("image_inspect", {"id_or_name": f"{run}-image"}),
        ("image_history", {"id_or_name": f"{run}-image"}),
        ("compose_config", {"project_dir": str(project)}),
        ("compose_logs", {"project_dir": str(project)}),
        ("compose_ps", {"project_dir": str(project)}),
        ("system_df", {}),
        ("system_events", {"since": since}),
        ("network_list", {}),
        ("volume_list", {}),
    ]
    if daemon.swarm:
        calls += [
            ("service_list", {}),
            ("service_inspect", {"id_or_name": f"{run}-service"}),
            ("service_ps", {"id_or_name": f"{run}-service"}),
            ("service_logs", {"id_or_name": f"{run}-service"}),
            ("secret_list", {}),
        ]
    calls += [("container_inspect", {"id_or_name": i}) for i in daemon.docker("ps", "-aq").split()]
    calls += [("image_inspect", {"id_or_name": i}) for i in sorted(set(daemon.docker("images", "-q").split()))]

    forms = {name: _forms(value) for name, value in planted.items()}
    exposed: dict[str, set[str]] = {name: set() for name in planted}
    surfaced: dict[str, set[str]] = {name: set() for name in planted}
    counts = {"planted": 0, "decoy": 0, "other": 0, "unaligned": 0}
    decoys_hit: set[str] = set()
    others: list[str] = []
    refused: list[str] = []
    async with (
        _session(daemon.url, CONFIGURATIONS[configuration]) as (redacted_session, initialised),
        _session(daemon.url, {"DOCKER_MCP_SERVER_HOSTS": "d={url}"}) as (reference_session, _),
    ):
        surface = await _surface(redacted_session, initialised, configuration)
        for name, arguments in calls:
            if name == "system_events":
                # Closed after the exec above, and not in the future, where the daemon would wait.
                arguments = {**arguments, "until": str(int(time.time()))}
            redacted, redacted_failed = await _call_tool(redacted_session, name, arguments)
            raw, raw_failed = await _call_tool(reference_session, name, arguments)
            if redacted_failed and not raw_failed:
                refused.append(name)
            # Error text counts too: a refusal or a failure must not carry a credential either.
            for planted_name, planted_forms in forms.items():
                if any(form in redacted for form in planted_forms):
                    exposed[planted_name].add(name)
                if any(form in raw for form in planted_forms):
                    surfaced[planted_name].add(name)
            if redacted_failed:
                continue
            for span, before in _replaced_spans(raw, redacted):
                if span is None:
                    counts["unaligned"] += 1
                elif any(form in span or (len(span) >= 6 and span in form) for fs in forms.values() for form in fs):
                    counts["planted"] += 1
                elif hits := [k for k, v in DECOYS.items() if span in v or v in span]:
                    counts["decoy"] += 1
                    decoys_hit.update(hits)
                else:
                    counts["other"] += 1
                    others.append(f"{name}: {len(span)} characters after {before!r}")

    print(f"\n=== {configuration}: {daemon.version()}; {len(calls)} calls on each server")
    print("What the agent is told and offered")
    for expectation, holds in surface:
        print(f"  {'ok' if holds else 'WRONG':14} {expectation}")
    print("Planted values (caught: no raw, base64, hex or URL-encoded form left in any redacted output or error)")
    for planted_name in planted:
        if exposed[planted_name]:
            # The value itself is never printed; its length and randomness are enough to tell a miss the
            # shape rules allow (a random value that happens to fall under the threshold) from a defect.
            value = planted[planted_name]
            where = (
                f"in {', '.join(sorted(exposed[planted_name]))} "
                f"[{len(value)} characters, {rules.entropy(value):.2f} bits per character]"
            )
            status = "MISSED"
        elif surfaced[planted_name] and surfaced[planted_name] <= set(refused):
            # Only calls the redacted server refused or does not offer showed it, so redaction never saw it.
            status, where = "NOT REACHED", f"(only {', '.join(sorted(surfaced[planted_name]))}, refused here)"
        elif surfaced[planted_name]:
            status, where = "caught", f"(appeared in {', '.join(sorted(surfaced[planted_name]))})"
        else:
            status, where = "NEVER SURFACED", "(so this run says nothing about it)"
        print(f"  {status:14} {planted_name} {where}")
    print(f"Decoys replaced: {len(decoys_hit)} of {len(DECOYS)}: {', '.join(sorted(decoys_hit)) or 'none'}")
    print(f"Replacements: {counts}")
    print("Other content replaced (never printed; a true positive from an earlier run can appear here):")
    for line in others[:60]:
        print(f"  {line}")
    print(f"Refused or unavailable on the redacted server only: {', '.join(sorted(set(refused))) or 'none'}")


# --- cost --------------------------------------------------------------------------------------------


def _populate(daemon: Daemon, containers: int, images: int) -> None:
    def image(n: int) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            data = f"{n}\n".encode()
            info = tarfile.TarInfo("id")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        daemon.docker("import", "-c", f"ENV IMG_N={n}", "-", f"redaction-measure/img:{n}", input=buffer.getvalue())

    def container(n: int) -> None:
        daemon.docker(
            "create",
            "--name",
            f"redaction-measure-{n}",
            "-e",
            f"DB_PASSWORD={secrets.token_urlsafe(18)}",
            "alpine:3",
            "true",
        )

    with ThreadPoolExecutor(12) as pool:
        list(pool.map(image, range(images)))
        list(pool.map(container, range(containers)))


async def _cost(daemon: Daemon) -> None:
    mcp, wire = _server(daemon.url)
    started = time.perf_counter()
    await _call(mcp, "system_ping", {}, "redacted")
    first = time.perf_counter() - started
    target = daemon.docker("ps", "-aq").split()[0]
    size = daemon.docker("info", "--format", "{{.Containers}} containers, {{.Images}} images")
    print(f"\n{daemon.version()}; {size}")
    print(f"First redacted call, which harvests every credential: {first:.1f} s")
    print(f"Store: {wire.REDACTOR.report('redacted')}\n")
    cases = [
        ("system_ping", {}),
        ("container_logs", {"id_or_name": target}),
        ("container_inspect", {"id_or_name": target}),
        ("container_list", {"all": True}),
        ("image_list", {}),
    ]
    for name, arguments in cases:
        medians = {}
        for label in ("unfiltered", "redacted"):
            await _call(mcp, name, arguments, label)
            times = []
            for _ in range(15):
                t = time.perf_counter()
                await _call(mcp, name, arguments, label)
                times.append(time.perf_counter() - t)
            medians[label] = statistics.median(times)
        added = medians["redacted"] - medians["unfiltered"]
        print(
            f"  {name:18} unfiltered {medians['unfiltered'] * 1000:6.0f} ms   redacted "
            f"{medians['redacted'] * 1000:6.0f} ms   added {added * 1000:6.0f} ms"
        )


# --- agents: everyday requests to a real agent, scored ----------------------------------------------------

_AGENT_HOSTS = {
    "readonly": {
        "DOCKER_MCP_SERVER_HOSTS": "d={url}",
        "DOCKER_MCP_SERVER_REDACT": "1",
        "DOCKER_MCP_SERVER_READONLY": "1",
    },
    "writable": {"DOCKER_MCP_SERVER_HOSTS": "d={url}", "DOCKER_MCP_SERVER_REDACT": "1"},
}


def _plant_app(daemon: Daemon, secrets_: dict[str, str]) -> None:
    """A small app with a fault to find: `web` points at a database host that does not exist.

    Its password is right, so an agent that blames the password, misled by markers it cannot compare,
    has got it wrong.  `api` logs an upstream failure carrying its token.

    Args:
        daemon: the daemon to plant on
        secrets_: the planted credentials, by name
    """
    daemon.docker("rm", "-f", *daemon.docker("ps", "-aq").split(), check=False)
    for network in ("appnet",):
        daemon.docker("network", "create", network, check=False)
    daemon.docker(
        "run",
        "-d",
        "--name",
        "db",
        "--network",
        "appnet",
        "-e",
        f"POSTGRES_PASSWORD={secrets_['db']}",
        "postgres:16-alpine",
    )
    daemon.docker(
        "run",
        "-d",
        "--name",
        "web",
        "--network",
        "appnet",
        "-e",
        "DB_HOST=db-old",
        "-e",
        "DB_USER=postgres",
        "-e",
        f"DB_PASSWORD={secrets_['db']}",
        "alpine:3",
        "sh",
        "-c",
        'while :; do nc -z -w2 "$DB_HOST" 5432 2>/dev/null && echo "connected to $DB_HOST" '
        '|| echo "ERROR cannot reach database at $DB_HOST:5432"; sleep 5; done',
    )
    daemon.docker(
        "run",
        "-d",
        "--name",
        "api",
        "--network",
        "appnet",
        "-e",
        f"API_KEY={secrets_['api_key']}",
        "-e",
        f"UPSTREAM_TOKEN={secrets_['token']}",
        "alpine:3",
        "sh",
        "-c",
        'while :; do echo "ERROR upstream rejected request (401): Authorization: Bearer $UPSTREAM_TOKEN"; '
        "sleep 10; done",
    )
    time.sleep(8)


def _env_of(daemon: Daemon, container: str) -> dict[str, str]:
    raw = daemon.docker("inspect", "--format", "{{json .Config.Env}}", container, check=False)
    return dict(item.split("=", 1) for item in json.loads(raw or "[]") if "=" in item)


def _scenarios(project: Path) -> list[dict]:
    """The requests, phrased as a user would, each with how its outcome is judged."""

    def answer_has(*words: str):
        return lambda daemon, run, secrets_: all(word.lower() in run["answer"].lower() for word in words)

    def tool_output_has(*words: str):
        # A psql role listing in what a tool returned without error, not the agent's own words or input.
        return lambda daemon, run, secrets_: any(word in run["outputs"] for word in words)

    def container_with(name_part: str, env: dict[str, str], ports: str | None = None):
        def judge(daemon: Daemon, run: dict, secrets_: dict[str, str]) -> bool:
            for container in daemon.docker("ps", "-a", "--format", "{{.Names}}").split():
                if name_part not in container:
                    continue
                actual = _env_of(daemon, container)
                if all(actual.get(key) == secrets_.get(value, value) for key, value in env.items()):
                    if ports is None or ports in daemon.docker("port", container, check=False):
                        return True
            return False

        return judge

    return [
        {
            "name": "triage",
            "write": False,
            "judge": answer_has("db-old"),
            "prompt": "The web container can't connect to its database. Work out why and tell me what's wrong.",
        },
        {
            "name": "disk",
            "write": False,
            "judge": answer_has("postgres"),
            "prompt": "Which containers and images are using the most disk space on this Docker host?",
        },
        {
            "name": "logs",
            "write": False,
            "judge": answer_has("401"),
            "prompt": "Show me the errors in the api container's logs and summarise what's going wrong.",
        },
        {
            "name": "config",
            "write": False,
            "judge": answer_has("DB_HOST", "DB_PASSWORD"),
            "prompt": "What environment variables is the web container configured with?",
        },
        {
            "name": "recreate",
            "write": True,
            "judge": container_with("web", {"DB_PASSWORD": "db", "DB_HOST": "db-old"}, ports="8080"),
            "prompt": "Recreate the web container so that its port 80 is published on the host's port 8080, keeping "
            "all of its existing settings (environment, network, command).",
        },
        {
            "name": "psql",
            "write": True,
            "judge": tool_output_has("rolname", "Superuser"),
            "prompt": "Check whether the password the web container uses for the database actually works: from inside "
            "the db container, connect with psql over TCP to 127.0.0.1 as the postgres user with that password and "
            "list the roles.",
        },
        {
            "name": "compose",
            "write": True,
            "judge": container_with("report", {"DB_PASSWORD": "db"}),
            "prompt": f"Bring up the Compose project in {project}. Its DB_PASSWORD has to be the same password the "
            "db container was started with.",
        },
        {
            "name": "clone",
            "write": True,
            "judge": container_with("worker", {"API_KEY": "api_key"}),
            "prompt": "Create and start a container called worker from alpine:3 running `sleep 600`, with the same "
            "API_KEY as the api container.",
        },
    ]


def _run_agent(daemon: Daemon, mode: str, scenario: dict, out: Path, model: str, budget: float) -> dict:
    """One request to a headless Claude Code with only this server's tools, and its transcript.

    Returns:
        dict: the parsed transcript: final answer, tool calls, tool errors, cost and turns
    """
    env = {key: value.format(url=daemon.url) for key, value in _AGENT_HOSTS[mode].items()}
    repo = Path(__file__).resolve().parent.parent
    config = {
        "mcpServers": {
            "docker": {
                "command": "uv",
                "args": ["run", "--quiet", "--directory", str(repo), "python", "-m", "docker_mcp"],
                "env": env,
            }
        }
    }
    config_path = out / f"mcp-{mode}.json"
    config_path.write_text(json.dumps(config))
    try:
        result = _run(
            [
                "claude",
                "-p",
                scenario["prompt"],
                "--mcp-config",
                str(config_path),
                "--strict-mcp-config",
                "--tools",
                "",
                "--allowedTools",
                "mcp__docker",
                "--output-format",
                "stream-json",
                "--verbose",
                "--model",
                model,
                "--max-budget-usd",
                str(budget),
                "--no-session-persistence",
                "--setting-sources",
                "",
            ],
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        # Recorded rather than raised, so the runs already paid for are still reported.
        partial = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        result = subprocess.CompletedProcess(exc.cmd, -1, partial, "timed out after 900 s")
    transcript = out / f"{mode}-{scenario['name']}.jsonl"
    transcript.write_text(result.stdout)
    calls, errors, outputs, answer, cost, turns = [], [], [], "", 0.0, 0
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get("message") or {}
        content = message.get("content")
        for item in content if isinstance(content, list) else []:
            if item.get("type") == "tool_use":
                calls.append(item.get("name", "").removeprefix("mcp__docker__"))
            if item.get("type") == "tool_result":
                result_content = item.get("content")
                text = (
                    result_content
                    if isinstance(result_content, str)
                    else " ".join(c.get("text", "") for c in result_content or [])
                )
                if item.get("is_error"):
                    errors.append(text[:300])
                else:
                    outputs.append(text)
        if event.get("type") == "result":
            answer, cost, turns = (
                event.get("result") or "",
                event.get("total_cost_usd") or 0.0,
                event.get("num_turns") or 0,
            )
    return {
        "raw": result.stdout,
        "stderr": result.stderr[-500:],
        "calls": calls,
        "errors": errors,
        "outputs": "\n".join(outputs),
        "answer": answer,
        "cost": cost,
        "turns": turns,
    }


def _agents(daemon: Daemon, model: str, budget: float, only: str | None) -> None:
    pool = string.ascii_letters + string.digits
    secrets_ = {
        "db": "".join(secrets.choice(pool) for _ in range(20)),
        "api_key": "ak_" + "".join(secrets.choice(pool) for _ in range(32)),
        "token": "".join(secrets.choice(pool) for _ in range(40)),
    }
    daemon.docker("pull", "-q", "postgres:16-alpine")
    out = Path(tempfile.mkdtemp(prefix="redaction-agents-"))
    project = out / "report"
    project.mkdir()
    (project / "compose.yaml").write_text(
        'services:\n  report:\n    image: alpine:3\n    command: ["sleep", "600"]\n'
        "    environment:\n      DB_PASSWORD: ${DB_PASSWORD}\n"
    )
    print(f"Transcripts: {out}", flush=True)
    for mode in ("readonly", "writable"):
        for scenario in _scenarios(project):
            if only and scenario["name"] != only:
                continue
            _plant_app(daemon, secrets_)
            run = _run_agent(daemon, mode, scenario, out, model, budget)
            leaked = sorted(name for name, value in secrets_.items() if any(f in run["raw"] for f in _forms(value)))
            done = scenario["judge"](daemon, run, secrets_)
            misled = (
                bool(
                    re.search(
                        r"password[^.\n]{0,60}(mismatch|does ?n.t match|differ|incorrect|wrong)",
                        run["answer"],
                        re.IGNORECASE,
                    )
                )
                and scenario["name"] == "triage"
            )
            _report_agent_run(mode, scenario, run, leaked, done, misled)
            names = daemon.docker("ps", "-a", "--format", "{{.Names}}").split()
            if any(name.startswith("report") for name in names):
                _run(["docker", "-H", daemon.url, "compose", "-p", "report", "down"], cwd=project, capture_output=True)


def _report_agent_run(mode: str, scenario: dict, run: dict, leaked: list[str], done: bool, misled: bool) -> None:
    """Print one scored run as soon as it finishes, so a later failure loses nothing already paid for."""
    expected = "declined, as read-only" if mode == "readonly" and scenario["write"] else "done"
    if done:
        outcome = "done"
    elif mode == "readonly" and scenario["write"]:
        outcome = "not done (expected)"
    else:
        outcome = "NOT DONE"
    print(
        f"\n[{mode}] {scenario['name']}: {outcome} (expected: {expected}); "
        f"{len(run['calls'])} tool calls, {run['turns']} turns, ${run['cost']:.2f}",
        flush=True,
    )
    print(f"  leaked: {', '.join(leaked) or 'nothing'}" + ("; BLAMED THE PASSWORD" if misled else ""))
    if run["errors"]:
        print(f"  tool errors ({len(run['errors'])}):")
        for error in run["errors"][:6]:
            print(f"    - {error}")
    print(f"  tools: {', '.join(run['calls'][:20])}")
    print(f"  answer: {' '.join(run['answer'].split())[:600]}")
    if not run["answer"]:
        print(f"  stderr: {run['stderr']}", flush=True)


# --- main --------------------------------------------------------------------------------------------


def main() -> None:
    """Parse the command line and run the measurement asked for."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    parser.add_argument("measure", choices=["sweep", "cost", "agents"])
    parser.add_argument("--docker-host", help="measure this daemon instead of a throwaway dind one")
    parser.add_argument("--dind-image", default="docker:28-dind")
    parser.add_argument("--port", type=int, default=23760, help="loopback port for the throwaway daemon")
    parser.add_argument("--swarm", action="store_true", help="sweep: include a Swarm service and secret")
    parser.add_argument(
        "--configuration",
        choices=[*CONFIGURATIONS, "all"],
        default="host",
        help="sweep: how redaction is switched on (per host or globally, writable or read-only), or all four",
    )
    parser.add_argument("--containers", type=int, default=2000, help="cost: stopped containers to create")
    parser.add_argument("--images", type=int, default=500, help="cost: distinct images to create")
    parser.add_argument("--model", default="sonnet", help="agents: the model the headless Claude Code runs")
    parser.add_argument("--budget", type=float, default=1.5, help="agents: dollar cap on each request")
    parser.add_argument(
        "--only", choices=[scenario["name"] for scenario in _scenarios(Path())], help="agents: run one scenario"
    )
    options = parser.parse_args()
    if options.measure in ("cost", "agents") and options.docker_host:
        parser.error(f"{options.measure} fills or rebuilds the daemon it measures, so it runs only on a throwaway one")
    with Daemon(options.docker_host, options.dind_image, options.port, options.swarm) as daemon:
        if options.measure == "cost":
            _populate(daemon, options.containers, options.images)
            asyncio.run(_cost(daemon))
            return
        if options.measure == "agents":
            _agents(daemon, options.model, options.budget, options.only)
            return
        run = "redaction-measure-" + secrets.token_hex(2)
        planted = _planted(daemon.swarm)
        configurations = list(CONFIGURATIONS) if options.configuration == "all" else [options.configuration]
        with tempfile.TemporaryDirectory() as work:
            project = _plant(daemon, run, planted, Path(work))
            try:
                for configuration in configurations:
                    asyncio.run(_sweep(daemon, run, planted, project, configuration))
            finally:
                _remove_planted(daemon, run, project)


if __name__ == "__main__":
    sys.exit(main())
