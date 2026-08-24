"""Consistency checks over the repo's own prose.

Documentation cannot be type-checked, and one list has now been wrong in six places across a single
feature branch: the enumeration of the CLI-backed domains, which predated `stack` in several files and
got corrected only where a reviewer happened to point. The check below is derived from `server.py`'s own
domain tuples rather than a copy of them, so adding a CLI-backed domain fails here until every
enumeration in the docs names it.
"""

import asyncio
import re
from pathlib import Path

import pytest

import docker_mcp.tools  # noqa: F401  - importing registers every tool on the server singleton
from docker_mcp.server import _CLI_DOMAINS, _REMOTE_EXEC_DOMAINS, mcp

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ARCHITECTURE_DIR = _REPO_ROOT / "architecture"

# The architecture/ layer is derived rather than listed, so a file added there is covered without
# anyone remembering to update this tuple. The cost of deriving it is that `Path.glob` answers a
# missing directory with an empty iterator rather than an error, so a rename or removal of
# architecture/ would shrink this set in silence and the suite would stay green while covering none
# of that layer. `test_the_derived_half_of_the_doc_set_is_not_empty` is what makes that loud.
_ARCHITECTURE_DOCS = tuple(sorted(str(p.relative_to(_REPO_ROOT)) for p in _ARCHITECTURE_DIR.glob("*.md")))
_DOC_FILES = (
    "README.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    ".github/copilot-instructions.md",
) + _ARCHITECTURE_DOCS

# Only lines that *claim to describe the CLI-backed surface* are enumerations. Without this the check
# false-positives on prose using the same words as nouns ("a build context", "Compose files") and on the
# image-variant tables, which list the plugin binaries installed in each image — where `stack` correctly
# does not appear, being part of the core CLI rather than a plugin.
_ENUMERATION_MARKERS = ("cli-backed", "cli shell-out", "docker cli feature")

# Two domain names is already a list: the mistake this exists to catch shipped as "(Compose, Context)".
_ENUMERATION_THRESHOLD = 2

# Lines that carry a marker phrase without enumerating the surface. Kept as an explicit list with a
# reason each, rather than loosening the rule until it catches nothing: an exemption anyone can read is
# better than a heuristic nobody can predict.
_NOT_ENUMERATIONS = (
    # About which *containers* carry provenance labels. Buildx and scout create no containers and
    # `context` is unrelated, so naming them here would be wrong rather than complete.
    "containers (created via CLI shell-out) are also unstamped",
)

# The two sets a legitimate enumeration can be: every CLI-backed domain, or the subset with the
# remote-exec fallback (`context` deliberately excluded — its tools manage this host's own contexts).
_VALID_SETS = (frozenset(_CLI_DOMAINS), frozenset(_REMOTE_EXEC_DOMAINS))


def _named_domains(line: str) -> frozenset[str]:
    """
    Which CLI-backed domains a line names as whole words.

    Word boundaries matter: `compose_up` and `scout_cves` are tool names rather than domain names, and
    `\\bcompose\\b` skips them because `_` is a word character.

    args: line - one line of documentation
    returns: frozenset[str] - the domains named
    """
    return frozenset(d for d in _CLI_DOMAINS if re.search(rf"\b{d}\b", line, re.IGNORECASE))


def _incomplete_enumeration(line: str) -> list[str] | None:
    """
    The domains a line omits, if it enumerates the CLI-backed surface incompletely.

    args: line - one line of documentation
    returns: list[str] | None - the missing domains, or None when the line is fine or not an enumeration
    """
    # Backticks are stripped before the marker test, not before anything else: the real lines carry
    # the marker phrase as "`docker` CLI feature", and a plain-substring match against the raw line
    # silently misses that - the guard stops firing on exactly the sentence it was written for. The
    # exemption test below still matches the raw line, since those are quoted verbatim from the docs.
    lowered = line.lower().replace("`", "")
    if not any(marker in lowered for marker in _ENUMERATION_MARKERS):
        return None
    if any(exemption in line for exemption in _NOT_ENUMERATIONS):
        return None
    named = _named_domains(line)
    if len(named) < _ENUMERATION_THRESHOLD or named in _VALID_SETS:
        return None
    closest = min(_VALID_SETS, key=lambda candidate: len(candidate ^ named))
    return sorted(closest - named)


def test_the_derived_half_of_the_doc_set_is_not_empty():
    """
    architecture/ still exists and still contributes files to the checked set.

    Without this, renaming or removing that directory would leave `Path.glob` returning nothing and
    every check in this module quietly narrowing to the five hand-listed files - passing, while
    covering none of the layer the enumerations were moved into. A guard that stops guarding without
    saying so is the failure this whole module exists to prevent, so it gets a guard of its own.
    """
    assert _ARCHITECTURE_DIR.is_dir(), (
        f"{_ARCHITECTURE_DIR} is missing. If the architecture/ layer was deliberately renamed or "
        "removed, update _DOC_FILES to match rather than letting the glob silently cover nothing."
    )
    assert _ARCHITECTURE_DOCS, (
        f"{_ARCHITECTURE_DIR} exists but holds no *.md files, so the derived half of _DOC_FILES is "
        "empty and this module is checking only the five hand-listed documents."
    )


def test_doc_enumerations_of_cli_backed_domains_are_complete():
    problems = []
    for relative in _DOC_FILES:
        path = _REPO_ROOT / relative
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            missing = _incomplete_enumeration(line)
            if missing:
                problems.append(f"{relative}:{number} names {sorted(_named_domains(line))} but omits {missing}")
    assert not problems, (
        "A line describing the CLI-backed surface enumerates only some of its domains. Name every domain "
        "in the set it means (all of them, or the four with the remote-exec fallback), or reword so it is "
        "not an enumeration:\n  " + "\n  ".join(problems)
    )


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # The exact shape that shipped in SECURITY.md, and the reason the threshold is two.
        ("docker CLI features (Compose, Context) and direct registry HTTPS access.", ["buildx", "scout", "stack"]),
        ("CLI-backed tools (Compose, Buildx, Context, Scout) shell out to `docker`", ["stack"]),
        # Complete sets: the whole CLI-backed surface, or the four with the fallback.
        ("CLI-backed tools (Compose, Stack, Buildx, Scout, Context) shell out", None),
        ("the CLI-backed ones (**Compose, Stack, Buildx, Scout**) fall back to the remote host", None),
        # Not enumerations: same words, different job — these are why the marker gate exists.
        ("| `full` | docker CLI + compose + buildx + **scout** |", None),
        ("name files that exist here — Compose files, a bake file, a build context", None),
        ("a single domain mentioned in passing: CLI-backed compose only", None),
        # The one recorded exemption: a marker phrase in a sentence about containers, not about tools.
        ("Compose/stack containers (created via CLI shell-out) are also unstamped.", None),
        # Backticked marker phrase: the form CLAUDE.md and architecture/cli-shell-out.md actually use.
        # Before the backticks were stripped these returned None, so those two lines were unguarded.
        (
            "Any tool wrapping a `docker` CLI feature (Compose, Context) MUST go through run_docker",
            ["buildx", "scout", "stack"],
        ),
        ("Any tool wrapping a `docker` CLI feature (Compose, Stack, Buildx, Scout, Context) MUST", None),
        ("`CLI-backed` tools (Compose, Buildx, Context, Scout) shell out", ["stack"]),
    ],
)
def test_the_enumeration_check_itself_flags_what_it_should(line, expected):
    """The guard is only worth having if it catches the real cases and leaves ordinary prose alone."""
    assert _incomplete_enumeration(line) == expected


# A tool docstring is not an internal comment: the server advertises it verbatim as the tool's
# `description`, so it is product prose that a model reads when choosing between 164 tools. 163 em
# dashes had accumulated in these docstrings before anyone noticed, so the rule is pinned here rather
# than left to be remembered.
#
# This is ASCII-only, not merely ASCII *punctuation*, and that is deliberate: the house style lets a
# symbol carrying meaning survive elsewhere in shipped prose, but an allow-list is a thing to argue
# over and none of the 164 descriptions has needed one. The three arrows and one ellipsis found during
# the sweep all read better as words (`dict of str to str`). If a description ever genuinely needs a
# symbol, widen this check deliberately rather than working around it - and update the rule in
# CLAUDE.md and its mirror in the same change, since they promise exactly what this enforces.
#
# The assertion runs against what `list_tools()` actually advertises rather than against the source,
# because the advertised text is the thing that ships and the two could drift.
#
# Scope is deliberately only tool descriptions. Comments, tests and workflow files still hold em
# dashes; none of them ship, and sweeping them is a separate decision rather than a gap here.
def test_advertised_tool_descriptions_are_plain_ascii():
    offenders = [
        f"{t.name}: {char!r} (U+{ord(char):04X})"
        for t in asyncio.run(mcp.list_tools())
        for char in sorted({c for c in (t.description or "") if ord(c) > 127})
    ]
    assert not offenders, "non-ASCII in advertised tool descriptions:\n" + "\n".join(offenders)


def test_the_ascii_check_reads_distinct_real_descriptions():
    # Guards the check above rather than the code it checks. A first attempt at this measurement
    # walked `_tool_registry`, which maps to the `@tool()` wrapper, so every tool reported the
    # decorator's own 95-character docstring: 164 tools, one string, and the check would have passed
    # no matter what the tools actually said. Distinctness is what makes that failure visible.
    descriptions = [t.description or "" for t in asyncio.run(mcp.list_tools())]
    assert len(descriptions) > 100
    assert len(set(descriptions)) > 100


def test_the_instruction_files_name_a_check_that_exists():
    # `CLAUDE.md` and its mirror name this module's checks by full node id, so that the rule points at
    # its own enforcement. A rename would leave the docs promising a check that no longer runs, which is
    # the same failure this file exists to catch in the domain enumerations - documentation that reads
    # as authoritative while being quietly wrong.
    referenced: set[str] = set()
    for name in ("CLAUDE.md", ".github/copilot-instructions.md"):
        text = (_REPO_ROOT / name).read_text(encoding="utf-8")
        referenced |= set(re.findall(r"tests/test_docs\.py::(test_\w+)", text))
    assert referenced, "the instruction files no longer name any check in this module"
    defined = set(re.findall(r"^def (test_\w+)", Path(__file__).read_text(encoding="utf-8"), re.M))
    missing = sorted(referenced - defined)
    assert not missing, f"named in the instruction files but not defined here: {missing}"
