"""Consistency and regression checks over the `skills/l337-docker` agent skill.

The skill is prose plus shell snippets, so nothing in it is type-checked or executed by the rest of
the suite. Every check below exists because the corresponding mistake was actually made while
authoring it and caught only by running the command against a real daemon:

- `docker events --until now` is rejected by the CLI ("failed to parse value as time or duration").
- `timeout(1)` is GNU coreutils and absent from stock macOS, so `timeout N docker ...` is not portable.
- `status` is a read-only variable in zsh, so `local status=...` aborts the function outright.
- `docker compose ps --format json` is NDJSON but `docker compose ls --format json` is a JSON array,
  so `jq -s` is required for one and silently wrong for the other.

Structural checks (cross-references, orphans, parity counts) are derived from `tool_catalog()` and
the filesystem rather than a copy of either, so adding a tool or a reference file fails here until
the skill is updated to match. Daemon-backed behaviour lives in `tests/integration/test_skill.py`.
"""

import re
from pathlib import Path

import pytest

from docker_mcp.server import tool_catalog

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILL_DIR = _REPO_ROOT / "skills" / "l337-docker"
_SKILL_MD = _SKILL_DIR / "SKILL.md"
# The comparison/coverage record lives at the repo root, not inside the skill: it documents both
# systems, and the skill ships standalone (its SKILL.md links to this file on GitHub).
_COMPARISON_MD = _REPO_ROOT / "MCP_VS_SKILLS.md"

# The single canonical provenance prefix. Lowercase deliberately: Docker label keys are
# case-sensitive, so a mixed-case key makes a lowercase `--filter label=...` silently match nothing.
_LABEL_PREFIX = "l337-docker-skill."


def _skill_files() -> list[Path]:
    """Every markdown file that makes up the skill."""
    return sorted(p for p in _SKILL_DIR.rglob("*.md"))


def _fenced_blocks(text: str) -> list[str]:
    """The contents of every ``` fenced code block."""
    return re.findall(r"```[a-z]*\n(.*?)```", text, re.S)


def _fenced_lines(text: str) -> list[str]:
    """Non-blank lines inside fenced code blocks - the commands a reader can copy and run.

    The negative guards below police only these. Prose has to be able to *name* an invalid form in
    order to warn about it ("`--until now` is invalid"), so including inline spans would make the
    warnings themselves fail the check they exist to enforce.
    """
    return [line for block in _fenced_blocks(text) for line in block.splitlines() if line.strip()]


def _command_spans(text: str) -> list[str]:
    """Fenced-block lines plus inline `docker ...` spans that look like real commands.

    Prose naming a command bare ("never use bare `docker logs`") is deliberately excluded: such a
    span is two tokens with no flag, whereas guidance a reader could copy carries arguments. Without
    that filter the bounding checks below would fire on the very sentences that state the rule.
    """
    spans: list[str] = []
    for block in _fenced_blocks(text):
        spans.extend(line for line in block.splitlines() if line.strip())
    for inline in re.findall(r"`(docker [^`]+)`", text):
        if "--" in inline or len(inline.split()) >= 3:
            spans.append(inline)
    return spans


@pytest.fixture(scope="module")
def skill_files() -> list[Path]:
    files = _skill_files()
    assert files, f"no markdown found under {_SKILL_DIR}"
    return files


# --------------------------------------------------------------------------------------------
# Structure and metadata
# --------------------------------------------------------------------------------------------


def test_the_skill_has_frontmatter_naming_it_after_its_own_directory():
    """A skill whose `name` disagrees with its directory does not load."""
    text = _SKILL_MD.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, "SKILL.md must open with a YAML frontmatter block"
    yaml = pytest.importorskip("yaml")
    meta = yaml.safe_load(match.group(1))
    assert isinstance(meta, dict), f"frontmatter must be a YAML mapping, got {type(meta).__name__}"
    assert meta["name"] == _SKILL_DIR.name
    # The description is the whole trigger surface - a client matches on it and nothing else.
    assert len(meta["description"]) > 100, "description is too thin to trigger reliably"
    assert "docker" in meta["description"].lower()


def test_the_skill_conforms_to_the_open_agent_skills_specification():
    """The skill is portable, not Claude-specific, and MCP_VS_SKILLS.md says so.

    Agent Skills is an open spec (https://agentskills.io/specification) with more than one
    implementation: GitHub Copilot reads the same `SKILL.md` from `.github/skills`,
    `.agents/skills` or the very `.claude/skills` directory Claude Code uses. The constraints below
    are the spec's, so this fails if a future edit makes the skill load in Claude but not elsewhere.
    """
    yaml = pytest.importorskip("yaml")
    match = re.match(r"^---\n(.*?)\n---\n", _SKILL_MD.read_text(encoding="utf-8"), re.S)
    assert match
    meta = yaml.safe_load(match.group(1))
    assert isinstance(meta, dict), f"frontmatter must be a YAML mapping, got {type(meta).__name__}"

    # Assert the type before the shape. YAML infers types, so `description: 123` reaches `len()` as
    # an int and `description: [a, b]` is a two-element list that sails through the length check
    # entirely. Without these, a malformed frontmatter either fails with an opaque TypeError or,
    # worse, passes.
    name = meta.get("name")
    assert isinstance(name, str), f"`name` must be a string, got {type(name).__name__}"
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name), f"{name!r} must be lowercase and hyphenated"
    assert len(name) <= 64, "spec caps `name` at 64 characters"
    assert name == _SKILL_DIR.name, "the spec requires `name` to match the skill's directory"

    description = meta.get("description")
    assert isinstance(description, str), f"`description` must be a string, got {type(description).__name__}"
    assert description.strip(), "`description` is required and cannot be blank"
    assert len(description) <= 1024, f"spec caps `description` at 1024 characters, got {len(description)}"


def test_no_tool_permission_frontmatter_preapproves_destructive_commands():
    """A `tools: [Bash(docker:*)]` allow-list would pre-approve `docker rm -f`, defeating the skill's
    own confirmation rule - the host's permission prompts are the only gate that actually refuses.

    All three spellings are checked: `tools`/`disallowedTools` are the documented skill fields, and
    `allowed-tools` is the slash-command spelling that is easy to reach for by mistake.
    """
    text = _SKILL_MD.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match
    frontmatter = match.group(1)
    for field in ("allowed-tools", "disallowedTools", "tools"):
        assert not re.search(rf"^{re.escape(field)}\s*:", frontmatter, re.M), f"{field} must not be declared"


def test_every_referenced_skill_file_exists(skill_files):
    """A dead `reference/x.md` pointer strands the agent mid-task with no fallback."""
    missing = set()
    for path in skill_files:
        for ref in re.findall(r"(?:reference|workflows)/[a-z-]+\.md", path.read_text(encoding="utf-8")):
            if not (_SKILL_DIR / ref).is_file():
                missing.add(f"{path.name} -> {ref}")
    assert not missing, f"broken cross-references: {sorted(missing)}"


def test_every_reference_and_workflow_file_is_reachable_from_the_router(skill_files):
    """SKILL.md is the only file loaded up front, so anything it never names is dead weight."""
    router = _SKILL_MD.read_text(encoding="utf-8")
    orphans = [
        f"{p.parent.name}/{p.name}"
        for p in skill_files
        if p.parent.name in {"reference", "workflows"} and f"{p.parent.name}/{p.name}" not in router
    ]
    assert not orphans, f"not referenced from SKILL.md: {orphans}"


def test_the_skill_carries_its_own_licence_and_points_back_at_this_repo():
    """The skill is meant to be downloadable on its own, so attribution cannot live only in the repo."""
    licence = _SKILL_DIR / "LICENSE"
    assert licence.is_file(), "skills/l337-docker/LICENSE is missing"
    licence_text = licence.read_text(encoding="utf-8")
    assert "MIT License" in licence_text
    assert "Gavin Lucas" in licence_text

    router = _SKILL_MD.read_text(encoding="utf-8")
    assert "github.com/L337-org/docker-mcp" in router
    assert "MIT" in router


def test_the_skill_is_excluded_from_the_other_channels_artifacts():
    """Both ignore files are denylists, so a new top-level directory is included by default.

    The skill is an *alternative* to the server, not part of it: packing it into the `.mcpb` would
    ship ~140 KB of markdown the bundle never reads, and leaving it in the Docker build context
    uploads it to the daemon on every image build for nothing.
    """
    assert "skills/" in (_REPO_ROOT / ".mcpbignore").read_text(encoding="utf-8").splitlines()
    assert "skills" in (_REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()


# --------------------------------------------------------------------------------------------
# Provenance labels
# --------------------------------------------------------------------------------------------


def test_provenance_labels_use_one_lowercase_prefix(skill_files):
    """Docker label keys are case-sensitive: a mixed-case key published in one file and filtered in
    lowercase in another matches nothing, silently, and the teardown workflow then reports a clean
    daemon while resources remain."""
    offenders = []
    for path in skill_files:
        for found in re.findall(r"[A-Za-z0-9_.-]*docker-skill\.[a-z]+", path.read_text(encoding="utf-8")):
            if not found.startswith(_LABEL_PREFIX):
                offenders.append(f"{path.name}: {found}")
    assert not offenders, f"non-canonical provenance labels: {offenders}"


def test_the_skill_does_not_claim_the_mcp_servers_provenance_label(skill_files):
    """Stamping `docker-mcp-server.managed` from the skill would attribute its resources to the
    server. The label may only be *mentioned* as a separate footprint to check, never applied."""
    for path in skill_files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "docker-mcp-server.managed" in line and "--label" in line:
                pytest.fail(f"{path.name} stamps the MCP server's label: {line.strip()}")


# --------------------------------------------------------------------------------------------
# Regression guards - each of these shipped as a bug in a draft of the skill
# --------------------------------------------------------------------------------------------


def test_no_snippet_uses_the_rejected_until_now(skill_files):
    """`docker events --until now` fails: "failed to parse value as time or duration"."""
    for path in skill_files:
        for span in _fenced_lines(path.read_text(encoding="utf-8")):
            assert "--until now" not in span, f"{path.name}: {span.strip()}"


def test_no_snippet_depends_on_the_timeout_binary(skill_files):
    """`timeout(1)` is GNU coreutils and is absent from stock macOS, where these snippets run."""
    for path in skill_files:
        for span in _fenced_lines(path.read_text(encoding="utf-8")):
            assert not re.search(r"\btimeout\s+\d+\s+docker\b", span), f"{path.name}: {span.strip()}"


def test_no_shell_snippet_declares_a_variable_named_status(skill_files):
    """`status` is read-only in zsh; `local status=...` aborts the function with a fatal error."""
    for path in skill_files:
        for span in _fenced_lines(path.read_text(encoding="utf-8")):
            assert not re.search(r"\blocal\b[^;]*\bstatus\b\s*=", span), f"{path.name}: {span.strip()}"
            assert not re.search(r"^\s*status=", span), f"{path.name}: {span.strip()}"


def test_compose_ls_is_not_slurped_and_compose_ps_is(skill_files):
    """The Compose plugin disagrees with itself: `ps --format json` is NDJSON, `ls --format json` is
    already an array. `jq -s` on the array yields a nested `[[...]]` that then fails to iterate."""
    for path in skill_files:
        for span in _fenced_lines(path.read_text(encoding="utf-8")):
            if "compose ls" in span and "--format json" in span:
                assert "jq -s" not in span, f"{path.name} slurps an array: {span.strip()}"
            if "compose ps" in span and "--format json" in span and "| jq" in span:
                assert "jq -s" in span or "jq -rs" in span, f"{path.name} needs jq -s: {span.strip()}"


# --------------------------------------------------------------------------------------------
# Output bounding - the skill's own rule 2, enforced against its own snippets
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "required", "why"),
    [
        ("docker logs", ("--tail", "--since"), "unbounded logs flood the context window"),
        ("docker service logs", ("--tail", "--since"), "unbounded logs flood the context window"),
        ("docker compose logs", ("--tail", "--since"), "unbounded logs flood the context window"),
        ("docker stats", ("--no-stream",), "`docker stats` never returns without --no-stream"),
        ("docker events", ("--until",), "`docker events` streams forever without --until"),
    ],
)
def test_streaming_commands_are_bounded_in_every_snippet(skill_files, command, required, why):
    """The skill tells the agent to bound every stream; its own examples must not contradict it."""
    for path in skill_files:
        for span in _command_spans(path.read_text(encoding="utf-8")):
            stripped = span.strip()
            if not re.search(rf"{re.escape(command)}\b", stripped):
                continue
            if stripped.lstrip("#").strip().startswith(("#", "|")):
                continue
            assert any(flag in stripped for flag in required), f"{path.name}: {why} -> {stripped}"


# --------------------------------------------------------------------------------------------
# Parity with the MCP server, derived from the live catalog
# --------------------------------------------------------------------------------------------


def _parity_domain_counts() -> dict[str, int]:
    """`### containers (25) - ...` and `### networks (7) / volumes (5) - ...` headings."""
    counts: dict[str, int] = {}
    for heading in re.findall(r"^### (.+)$", _COMPARISON_MD.read_text(encoding="utf-8"), re.M):
        for domain, count in re.findall(r"([a-z]+) \((\d+)\)", heading):
            counts[domain] = int(count)
    return counts


def _catalog_domain_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for tool in tool_catalog()["tools"]:
        counts[tool.get("domain") or "uncategorised"] = counts.get(tool.get("domain") or "uncategorised", 0) + 1
    return counts


def test_parity_covers_every_registered_tool_domain():
    """Adding a domain to the server without covering it here leaves a silent gap in the skill."""
    assert _parity_domain_counts().keys() == _catalog_domain_counts().keys()


def test_parity_tool_counts_match_the_live_catalog():
    """The per-domain counts are the cheap proxy for "every tool is accounted for": adding a tool
    changes a count, which fails here until MCP_VS_SKILLS.md is revisited."""
    assert _parity_domain_counts() == _catalog_domain_counts()


def test_parity_names_every_registered_prompt():
    """Each MCP prompt must map to a workflow, or the skill quietly loses that guidance."""
    parity = _COMPARISON_MD.read_text(encoding="utf-8")
    missing = [p["name"] for p in tool_catalog()["prompts"] if p["name"] not in parity]
    assert not missing, f"prompts absent from MCP_VS_SKILLS.md: {missing}"


def test_every_tool_domain_is_routed_somewhere_in_the_skill():
    """A domain nobody can find is not covered, whatever the comparison doc claims."""
    router = _SKILL_MD.read_text(encoding="utf-8")
    combined = router + "".join(p.read_text(encoding="utf-8") for p in _skill_files())
    for domain in _catalog_domain_counts():
        if domain == "uncategorised":
            continue
        assert domain in combined, f"domain {domain!r} is never mentioned in the skill"


# --------------------------------------------------------------------------------------------
# The figure-measurement script agrees with the document it regenerates
# --------------------------------------------------------------------------------------------
#
# Deliberately NOT asserting any token figure: those move on every docstring edit, so a test over
# them would fail constantly and mean nothing (drift is watched on a schedule instead - see the
# script's own docstring).  What is worth asserting is that the script and the document still
# describe the same thing, because that is binary and silent when it breaks: a renamed table row or
# an edited config line leaves the script measuring a configuration the document no longer quotes,
# and every number it emits then answers the wrong question.


@pytest.fixture(scope="module")
def figures_script():
    """The measurement script, imported by path (its filename is not a Python identifier)."""
    import importlib.util

    path = _REPO_ROOT / "scripts" / "measure-comparison-figures.py"
    assert path.is_file(), f"{path} is missing - MCP_VS_SKILLS.md's figures are no longer regenerable"
    spec = importlib.util.spec_from_file_location("measure_comparison_figures", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_measured_configuration_is_still_named_in_the_document(figures_script):
    """A config the script measures but the document no longer quotes is a figure nobody reads."""
    parity = _COMPARISON_MD.read_text(encoding="utf-8")
    for name, config in figures_script._CONFIGS.items():
        assert config["doc_row"] in parity, (
            f"config {name!r} measures a row the document no longer contains: {config['doc_row']!r}"
        )


def test_the_triage_config_matches_the_disable_line_the_document_publishes(figures_script):
    """The document tells a reader to paste one `DOCKER_MCP_SERVER_DISABLE` line; it must be the
    line the triage figures were actually measured with, or every trimmed number is fiction."""
    published = re.search(r"DOCKER_MCP_SERVER_DISABLE=([a-z,]+)", _COMPARISON_MD.read_text(encoding="utf-8"))
    assert published, "MCP_VS_SKILLS.md no longer publishes a DOCKER_MCP_SERVER_DISABLE line"
    domains = {d for d in _catalog_domain_counts() if d != "uncategorised"}
    derived = figures_script._disable_value(domains, figures_script._CONFIGS["triage"]["keep"])
    assert derived is not None
    assert set(published.group(1).split(",")) == set(derived.split(",")), (
        "the triage config in the script and the disable line in the document have diverged"
    )


def test_every_task_composition_names_real_tools_and_files(figures_script):
    """The per-task figures are chosen compositions, so a rename silently changes what they mean."""
    registered = {tool["name"] for tool in tool_catalog()["tools"]}
    for label, composition in figures_script._TASKS.items():
        missing = [t for t in composition["tools"] if t not in registered]
        assert not missing, f"task {label!r} names tools that are not registered: {missing}"
        for relative in composition["skill_files"]:
            assert (_SKILL_DIR / relative).is_file(), f"task {label!r} names a missing skill file: {relative}"
