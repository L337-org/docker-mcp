#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml==6.0.3"]
# ///
"""Repository conventions that every repository in this organisation shares.

VENDORED IDENTICALLY.  This file is byte-identical in every repository that carries it, and
`check-repo-hygiene.sha256` beside it records that.  The script checks itself against that
digest before doing anything else, so editing one copy fails that repository's own build rather
than quietly making the copies disagree.  To change it, change it everywhere and regenerate the
digest:

    shasum -a 256 scripts/check-repo-hygiene.py | cut -d' ' -f1 > scripts/check-repo-hygiene.sha256

That is a local check and cannot see the other copies.  Cross-repository divergence is watched
separately, on a schedule, because nothing inside one repository ever could.

A SCRIPT RATHER THAN A TEST MODULE, DELIBERATELY.  These checks are wanted in repositories that
are not primarily Python and have no suite to hang them off.  The PEP 723 header means it needs
only `uv` - no project install, no dependency group - so vendoring it is a file copy.

WHAT IS NOT HERE.  Anything specific to one product stays in that repository: a supported-Python
floor, a licence-header convention, a tag-escaping rule.  The line is whether the check would
mean the same thing in a repository that had never heard of the product.

Exit status is 0 when clean, 1 on findings, and 2 when the scan could not be trusted - a missing
digest, no tracked files, no workflows, no detail layer.  Exit 2 is deliberately not 1: "the
workflows are wrong" and "I could not look at them" are different answers, and collapsing them
sends a reader looking for the wrong thing.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

# No `from __future__ import annotations` here, deliberately. Nothing in this file is annotated,
# so it buys nothing - and a __future__ import must precede every other statement, which would
# force the header block below it and out of the position the convention puts it in.

import hashlib
import pathlib
import re
import shutil
import subprocess
import sys

# --- what the scan must find before its silence means anything (VE.2.4) --------------------
#
# A guard that searched nothing reports the same green as one that found nothing wrong.  These
# floors are well under any real repository here, so an ordinary addition or removal does not
# trip them while a broken listing does.  Repository-specific sentinels - "is this product's
# main module being read?" - stay in that repository's own tests, where they can name files
# that only exist there.
MIN_TRACKED_FILES = 20
UNIVERSAL_FILES = ("README.md", "AGENTS.md", "CLAUDE.md", "copilot-instructions.md")

# --- the instruction-file layer (NP.1.3) --------------------------------------------------
SHARED_INSTRUCTION_FILE = "AGENTS.md"
POINTER_FILES = ("CLAUDE.md", ".github/copilot-instructions.md")
GENERATED_START = "<!-- BEGIN GENERATED -->"
GENERATED_END = "<!-- END GENERATED -->"

# The detail layer is named by convention rather than configured, so this file stays identical
# everywhere.  A repository uses whichever of these it has; having none is exit 2, because a
# routing check with nothing to route is a check that cannot fail.
DETAIL_DIRECTORIES = ("architecture", "schema")


# --- public-repository leaks (SK.9.1) -----------------------------------------------------
#
# Matched by the tracker's key shape rather than one project's prefix, so a second project's
# keys are caught too.  Letters only in the prefix, and at least two: allowing digits made an
# earlier version match `Z0-9` inside a regex character class, and a false positive is what
# gets a guard switched off wholesale.
TRACKER_KEY = re.compile(r"\b[A-Z]{2,10}-\d+\b(?!\.\d)")

# Same shape, not tracker keys.  Each is a real external standard, so this is a list of
# evidenced exceptions rather than a way to quiet a genuine hit.
NOT_A_TRACKER_KEY = re.compile(r"\b(?:ISO-\d+|RFC-\d+|SHA-\d+|UTF-\d+|AES-\d+|PEP-\d+|CVE-\d+|SMETS-\d+|FLEX-\d+)\b")

# Any link into the internal Atlassian site, not only a wiki one.  Review asked whether this
# should require a /wiki/ path, since the name said "wiki link" while the pattern also matched a
# Jira /browse/ URL.  Widened the name rather than narrowing the pattern: a Jira board URL
# carrying no issue key - .../jira/software/projects/X/boards/1 - would otherwise slip past both
# this and the tracker-key check, and it is exactly as much of a leak.  A /browse/ link may now
# be reported twice, once here and once as its issue key.  That redundancy is the cheaper error.
#
# A URL shape is required, not the bare hostname.  Matching the substring flagged a routine
# prompt whose whole purpose is forbidding such links - the guard firing on the instruction not
# to do the thing - and so did requiring only a slash after the host, because the same prompt
# says "an atlassian.net/wiki link".
INTERNAL_LINK = re.compile(r"(?:https?://(?:[^\s)\"'/?#]*\.)?atlassian\.net(?![A-Za-z0-9-]|\.[A-Za-z]))|/wiki/spaces/")

# --- CI job bounds (GB.3.5) ---------------------------------------------------------------
MIN_TIMEOUT_MINUTES = 1
MAX_TIMEOUT_MINUTES = 60

BINARY_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".gz", ".zip", ".deb", ".whl")

# Compared as a resolved path, not a basename: another file of the same name elsewhere in a
# repository would otherwise be dropped from the scan without anything saying so.
SELF = pathlib.Path(__file__).resolve()


class CannotEvaluate(Exception):
    """The scan could not be trusted, so exit 2 rather than reporting a clean run."""


def read_text(path, root=None):
    """Read a file as UTF-8, turning any failure into CannotEvaluate.

    Every read that expects text goes through here, deliberately - the two exceptions are named
    below. Review found the same defect three separate times: a read that could raise OSError or
    UnicodeDecodeError and escape as a traceback rather than the exit-2 "cannot evaluate" this
    script promises. Patching each site as it was reported was clearly not going to converge. One funnel means a read
    added
    later cannot reintroduce it.

    args:
        path: the file to read.
        root: repository root, so the message names a relative path when it can.

    returns:
        The file's contents as text.
    """
    # The two deliberate exceptions, so this docstring cannot drift from the code:
    # is_searchable_text() reads bytes to decide whether a file is text at all, where a decode
    # failure is the answer rather than an error; and check_no_internal_references() reads with
    # errors="replace", because it searches prose for patterns and one undecodable byte should
    # not abort the whole run.
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        name = path.relative_to(root) if root and root in path.parents else path.name
        raise CannotEvaluate(f"{name} could not be read: {exc}") from exc


def verify_own_digest():
    """Fail if this copy of the script no longer matches the recorded digest.

    The point is not integrity against an attacker - anyone who can edit the script can edit
    the digest.  It is that a well-meaning local fix to one copy stops being invisible.

    Takes no repository root: the digest sits beside this file wherever this file is, which is
    what lets a moved or vendored copy still check itself.

    returns:
        Nothing; raises CannotEvaluate if the digest is missing or does not match.
    """
    script = pathlib.Path(__file__).resolve()
    recorded = script.with_suffix(".sha256")
    if not recorded.is_file():
        raise CannotEvaluate(f"{recorded.name} is missing, so this copy cannot be shown to match the others")
    tokens = read_text(recorded).split()
    if not tokens:
        raise CannotEvaluate(f"{recorded.name} is empty, so there is no digest to check against")
    want = tokens[0].strip()
    try:
        got = hashlib.sha256(script.read_bytes()).hexdigest()
    except OSError as exc:
        raise CannotEvaluate(f"{script.name} could not be read to check its digest: {exc}") from exc
    if got != want:
        raise CannotEvaluate(
            f"{script.name} does not match {recorded.name}: expected {want[:12]}, got "
            f"{got[:12]}.  If the change is intended, make it in every repository carrying "
            f"this script and regenerate the digest in each"
        )


def tracked_paths(root):
    """Every file git tracks, as absolute paths, with no filtering by content.

    Deliberately not a filesystem walk: that would sweep in build output and a developer's own
    gitignored configuration, which may hold real credentials and is none of this check's
    business.  Text is decided by sniffing rather than by a suffix allowlist, because the files
    most likely to carry a leaked tracker key - maintainer scripts, CODEOWNERS - have no
    extension at all.

    args:
        root: repository root.

    returns:
        A sorted list of paths.
    """
    # Resolved by lookup rather than hardcoded: git's path differs per platform and per
    # installation method (SU.6.3). The argument list is fixed and carries no caller input,
    # which is why the shell is never involved and S603 is answered rather than suppressed.
    git = shutil.which("git")
    if not git:
        raise CannotEvaluate("git is not on PATH, so the tracked-file listing cannot be built")
    try:
        listing = subprocess.run(  # noqa: S603 - fixed argument list, no shell, no caller input
            [git, "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            check=True,
            timeout=60,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise CannotEvaluate(f"cannot list tracked files with git: {exc}") from exc

    # Decoded explicitly rather than by text=True, which raises on a byte sequence the locale
    # cannot decode - the scan would crash instead of reporting (SU.6.6). surrogateescape rather
    # than replace: a path is bytes, and a replacement character produces a name that no longer
    # opens, so a file with an unusual path would be listed and then silently unreadable.
    # surrogateescape round-trips, which is what Python's own filesystem APIs use.
    names = listing.stdout.decode("utf-8", errors="surrogateescape")
    candidates = (root / name for name in names.split("\0") if name)
    return sorted(p for p in candidates if p.resolve() != SELF)


def searchable_text_files(paths):
    """The subset of `paths` that can be read as text and searched.

    Kept separate from the listing itself, deliberately. An earlier version filtered inside
    `tracked_paths` and the workflow scan drew from the filtered set - so a workflow file that
    was not readable UTF-8 was dropped before it ever reached the YAML parse, and the timeout
    checks silently stopped covering it. Whether a file is searchable prose and whether it is a
    workflow the bounds checks must see are different questions.

    args:
        paths: candidate paths.

    returns:
        A list of the searchable ones, in the order given.
    """
    return [p for p in paths if is_searchable_text(p)]


def is_searchable_text(path):
    """Whether this file can be read as text and searched.

    Sniffed rather than inferred from the name, so a file with no extension - every maintainer
    script here - is covered, and a future binary asset cannot break the scan.

    args:
        path: the file to test.

    returns:
        True when the file is readable UTF-8 text with no NUL bytes.
    """
    if not path.is_file() or path.suffix.lower() in BINARY_SUFFIXES:
        return False
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def workflow_jobs(root, tracked):
    """Every CI job, as `(repo-relative path, job id, job body)`.

    Sourced from the tracked listing rather than a glob, for the same reason: an untracked
    workflow a developer left lying about is not part of the repository.

    args:
        root: repository root.
        tracked: every tracked path, unfiltered - deliberately not the searchable-text subset,
            because a workflow that is not readable text must still be reported rather than
            quietly dropped from the bounds checks.

    returns:
        A list of triples.
    """
    import yaml

    directory = root / ".github" / "workflows"
    workflows = [p for p in tracked if p.parent == directory and p.suffix in (".yml", ".yaml")]
    if not workflows:
        raise CannotEvaluate("no tracked workflow files found, so the CI checks would verify nothing")

    jobs = []
    for path in workflows:
        relative = path.relative_to(root)
        try:
            document = yaml.safe_load(read_text(path, root))
        except yaml.YAMLError as exc:
            raise CannotEvaluate(f"{relative} is not valid YAML: {exc}") from exc
        if not isinstance(document, dict):
            raise CannotEvaluate(f"{relative} does not parse as a YAML mapping")
        declared = document.get("jobs")
        # Not `or {}`: a missing or null `jobs:` would become an empty mapping and the workflow
        # would be skipped in silence while the bounds checks reported success.
        if not isinstance(declared, dict) or not declared:
            raise CannotEvaluate(f"{relative} declares no usable `jobs:` mapping (got {declared!r})")
        for name, body in declared.items():
            if not isinstance(body, dict):
                raise CannotEvaluate(f"{relative}:{name} has a body that is not a mapping")
            jobs.append((relative, name, body))
    return jobs


def check_the_scan_is_real(root, tracked, files):
    """The listing found a plausible repository, not an empty glob.

    args:
        root: repository root.
        tracked: every tracked path. Unused here, and kept for the uniform signature every
            check in CHECKS is called with - renaming it would make one of five differ.
        files: the searchable-text subset.

    returns:
        A list of findings; raises CannotEvaluate when the listing itself is wrong.
    """
    if len(files) < MIN_TRACKED_FILES:
        raise CannotEvaluate(
            f"only {len(files)} tracked text file(s) found, expected at least "
            f"{MIN_TRACKED_FILES} - the listing is wrong rather than the repository being small"
        )
    names = {p.name for p in files}
    absent = [f for f in UNIVERSAL_FILES if f not in names]
    if absent:
        raise CannotEvaluate(
            f"the listing does not include {', '.join(absent)}, which every repository carrying "
            f"this script has - so the listing is not seeing what it should"
        )
    return []


def check_no_internal_references(root, tracked, files):
    """No tracker key or internal Atlassian link in a public repository (SK.9.1).

    Both are leaks and both are dead references to anyone reading the repository, including
    whoever reads generated release notes.  Describe the work instead; the issue carries the
    link in the other direction.

    args:
        root: repository root.
        tracked: every tracked path, unfiltered.
        files: the searchable-text subset.

    returns:
        A list of findings.
    """
    findings = []
    for path in files:
        relative = path.relative_to(root)
        # errors="replace" here and not via read_text(): this scan searches prose for patterns,
        # and one undecodable byte in one file should not abort the whole run. A file that
        # cannot be opened at all is still a hard failure, below.
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise CannotEvaluate(f"{relative} could not be read: {exc}") from exc
        for number, line in enumerate(text.splitlines(), 1):
            for match in TRACKER_KEY.finditer(line):
                span = match.group(0)
                if not NOT_A_TRACKER_KEY.search(span):
                    findings.append(f"{relative}:{number}: tracker key {span}")
            for match in INTERNAL_LINK.finditer(line):
                findings.append(f"{relative}:{number}: internal Atlassian link ({match.group(0)[:48]})")
    return findings


def check_ci_jobs_are_bounded(root, tracked, files):
    """Every CI job declares a timeout, and every timeout is a real bound (GB.3.5).

    A job without one inherits the platform's six-hour default.  The cost is not the wasted
    hours but that a hung job holds the concurrency group while it runs, silently discarding
    the runs queued behind it - a check that never appears rather than one that fails.

    The upper bound matters as much as the lower: a timeout large enough never to fire is
    indistinguishable from having none, and raising a flaky job's timeout until it stops failing
    is the usual route there.

    A quoted digit string is accepted and coerced.  GitHub documents this key as accepting the
    github, needs, strategy, matrix, vars and inputs contexts, and an expression is a string, so
    the field is coerced rather than strictly typed - which makes `timeout-minutes: "15"` a job
    genuinely bounded at fifteen minutes.  An expression is refused for the opposite reason: it
    is equally legal, but its value cannot be known here, and one resolving to 360 at run time
    would satisfy any static bound.  Anything else non-integral is refused as simply invalid and
    says so, because telling someone their literal is "unknowable" sends them looking for an
    expression that is not there.

    args:
        root: repository root.
        tracked: every tracked path, unfiltered.
        files: the searchable-text subset.

    returns:
        A list of findings.
    """
    findings = []
    for relative, name, body in workflow_jobs(root, tracked):
        minutes = body.get("timeout-minutes")
        if minutes is None:
            findings.append(
                f"{relative}:{name} declares no timeout-minutes, so it inherits the six-hour "
                f"platform default and can hold the concurrency group"
            )
            continue
        # Before the int check, because bool is a subclass of it and `true` is not five minutes.
        if isinstance(minutes, bool):
            findings.append(f"{relative}:{name} has timeout-minutes: {minutes!r}, which is a boolean")
            continue
        if isinstance(minutes, str):
            if "${{" in minutes:
                findings.append(
                    f"{relative}:{name} has timeout-minutes: {minutes!r}, whose value cannot be "
                    f"known at review time.  An expression is valid Actions, but one resolving "
                    f"to 360 at run time would satisfy this bound while leaving the job unbounded"
                )
                continue
            if not re.fullmatch(r"\s*\d+\s*", minutes):
                findings.append(
                    f"{relative}:{name} has timeout-minutes: {minutes!r}, which is not a whole "
                    f'number of minutes.  A quoted number such as "15" is accepted; this is not '
                    f"a value Actions will take"
                )
                continue
            minutes = int(minutes)
        if not isinstance(minutes, int):
            findings.append(
                f"{relative}:{name} has timeout-minutes: {minutes!r}, which is not a whole "
                f"number of minutes.  Actions takes an integer here"
            )
            continue
        if not MIN_TIMEOUT_MINUTES <= minutes <= MAX_TIMEOUT_MINUTES:
            findings.append(
                f"{relative}:{name} has timeout-minutes: {minutes}, outside "
                f"{MIN_TIMEOUT_MINUTES}-{MAX_TIMEOUT_MINUTES}.  A bound that never fires is the "
                f"same as no bound"
            )
    return findings


# Markdown links plus backtick-quoted paths, since a router routes both ways.
ROUTED_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)|`([^`]+\.md)`")


def routed_paths(text):
    """Every local .md path a router points at, normalised to repository-relative.

    args:
        text: the router's contents.

    returns:
        A set of repository-relative paths.
    """
    out = set()
    for match in ROUTED_LINK.finditer(text):
        target = (match.group(1) or match.group(2) or "").split("#")[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        # removeprefix, never lstrip: lstrip("./") strips dots and slashes as a character set
        # and turns ".agents/policy/x.md" into "agents/policy/x.md", which then reports every
        # file as both unrouted and dangling.
        target = target.removeprefix("./")
        # An absolute path, or one climbing out of the tree, is not a route this can honour.
        # Keeping it would let a link resolve to a file outside the repository and so make a
        # broken route look valid - the opposite of what the dangling check is for. Dropped
        # rather than reported: a router pointing outside the repository is a different problem
        # from the two this check exists to find, and flagging it here would be over-broad.
        candidate = pathlib.PurePosixPath(target)
        if candidate.is_absolute() or ".." in candidate.parts:
            continue
        out.add(str(candidate))
    return out


def check_the_instruction_layer(root, tracked, files):
    """The shared instruction file exists, and its pointers are only pointers (NP.1.3).

    None of these files is inheritable from an org-wide defaults repository, so a per-repository
    copy is the only way any of them takes effect - and deleting the shared one would leave
    every assistant unbriefed with nothing failing.

    A pointer file is asserted to hold a title and one pointer line and nothing else.  That is
    narrower than forbidding headings, which was the first attempt and too weak: markdown allows
    three spaces before a `##`, and prose needs no heading at all.  The promise is "a pointer and
    nothing else", so that is what is checked, inside the generated block as well as outside it.

    args:
        root: repository root.
        tracked: every tracked path, unfiltered.
        files: the searchable-text subset.

    returns:
        A list of findings.
    """
    findings = []
    shared_path = root / SHARED_INSTRUCTION_FILE
    if not shared_path.is_file():
        raise CannotEvaluate(f"no {SHARED_INSTRUCTION_FILE}, so there is no instruction layer to check")

    shared = read_text(shared_path, root)
    for sentinel in (GENERATED_START, GENERATED_END):
        if sentinel not in shared:
            findings.append(f"{SHARED_INSTRUCTION_FILE} is missing {sentinel!r}")
    if findings:
        return findings

    for pointer in POINTER_FILES:
        findings += pointer_findings(root, pointer)
    return findings


def pointer_findings(root, pointer):
    """Findings for one pointer file, which must hold a title and one pointer line.

    Narrower than forbidding headings, which was the first attempt and too weak: markdown
    allows three spaces before a `##`, and prose needs no heading at all. The promise is "a
    pointer and nothing else", so that is what is checked - inside the generated block as well
    as outside it, since content added inside would otherwise satisfy an outside-only check.

    args:
        root: repository root.
        pointer: repository-relative path of the pointer file.

    returns:
        A list of findings.
    """
    path = root / pointer
    if not path.is_file():
        return [f"{pointer} does not exist, so nothing routes that tool to {SHARED_INSTRUCTION_FILE}"]

    body = read_text(path, root)
    findings = []
    if SHARED_INSTRUCTION_FILE not in body:
        findings.append(f"{pointer} no longer references {SHARED_INSTRUCTION_FILE}")

    before, marker, rest = body.partition(GENERATED_START)
    if not marker:
        return findings + [f"{pointer} has no generated block"]
    inside_text, end_marker, after = rest.partition(GENERATED_END)
    if not end_marker:
        return findings + [f"{pointer} has no closing generated marker"]

    outside = [ln.strip() for ln in (before + after).splitlines() if ln.strip()]
    stray = [ln for ln in outside if not ln.startswith("# ")]
    if stray:
        findings.append(
            f"{pointer} carries content outside its generated block: {stray}.  It should hold a "
            f"title and a pointer, with every rule in {SHARED_INSTRUCTION_FILE}"
        )
    if sum(1 for ln in outside if ln.startswith("# ")) != 1:
        findings.append(f"{pointer} should have exactly one title")

    inside = [ln.strip() for ln in inside_text.splitlines() if ln.strip()]
    if len(inside) != 1:
        findings.append(
            f"{pointer} has {len(inside)} line(s) inside its generated block; a pointer file "
            f"holds exactly one: {inside}"
        )
    elif SHARED_INSTRUCTION_FILE not in inside[0]:
        findings.append(f"{pointer} pointer does not name {SHARED_INSTRUCTION_FILE}")
    return findings


def check_the_detail_layer_routing(root, tracked, files):
    """The detail layer and the router agree, in both directions.

    An unrouted detail file is invisible - nothing else would ever surface it.  A route to a
    file that does not exist is worse than no route, because it reads as authoritative and
    resolves to nothing, and the reader cannot tell the difference from the router alone.

    Scoped to the detail directories rather than to any unlinked document.  A guard that fires
    on a new top-level file is the over-broad kind that gets switched off wholesale (SU.7.2).

    `.agents/policy/` is checked in the dangling direction only, per DK.13.16: the generator
    that owns that directory reports a file it did not write and deliberately leaves it
    unrouted, so an unrouted file there is an accepted exemption rather than a failure.

    args:
        root: repository root.
        tracked: every tracked path, unfiltered.
        files: the searchable-text subset.

    returns:
        A list of findings.
    """
    shared_path = root / SHARED_INSTRUCTION_FILE
    routed = routed_paths(read_text(shared_path, root))

    present = [d for d in DETAIL_DIRECTORIES if (root / d).is_dir()]
    if not present:
        raise CannotEvaluate(
            f"none of {', '.join(DETAIL_DIRECTORIES)} exists, so a routing check would have "
            f"nothing to route and could not fail"
        )

    findings = []
    checked = 0
    for directory in present:
        for found in sorted((root / directory).glob("*.md")):
            checked += 1
            relative = str(found.relative_to(root))
            if relative not in routed:
                findings.append(
                    f"{relative} exists but {SHARED_INSTRUCTION_FILE} does not route to it, so "
                    f"nothing would ever send a reader there"
                )
    if not checked:
        raise CannotEvaluate(f"no .md files under {', '.join(present)}, so the routed direction verifies nothing")

    for relative in sorted(routed):
        if relative.endswith(".md") and not (root / relative).exists():
            findings.append(f"{SHARED_INSTRUCTION_FILE} routes to {relative}, which does not exist")
    return findings


# check_nothing_cites_a_pointer_as_content is deliberately NOT here.
#
# send-to-influx has that check, scoped to its shipped module and its packaging directory,
# because an exit-code table was once cited in both after it had moved into AGENTS.md. It does
# not generalise: "shipped" has no repository-independent meaning, and a version scoped to
# every tracked file flagged fourteen legitimate mentions across the four repositories - a
# CONTRIBUTING.md explaining the pointer arrangement, a .mcpbignore excluding the pointer from a
# bundle, a settings.json naming a path. An over-broad guard is one someone eventually switches
# off wholesale, which costs more than it ever caught (SU.7.2), so it stays product-specific.


CHECKS = (
    ("the scan is real", check_the_scan_is_real),
    ("no internal references in a public repository", check_no_internal_references),
    ("every CI job is bounded", check_ci_jobs_are_bounded),
    ("the instruction layer is intact", check_the_instruction_layer),
    ("the detail layer and the router agree", check_the_detail_layer_routing),
)


def main():
    """Run every check over the repository this script sits in.

    returns:
        A process exit status: 0 clean, 1 findings, 2 the scan cannot be trusted.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    try:
        verify_own_digest()
        tracked = tracked_paths(root)
        files = searchable_text_files(tracked)
        results = [(label, check(root, tracked, files)) for label, check in CHECKS]
    except CannotEvaluate as exc:
        print(f"check-repo-hygiene: cannot evaluate - {exc}", file=sys.stderr)
        return 2

    findings = [(label, f) for label, found in results for f in found]
    if findings:
        print(f"check-repo-hygiene: {len(findings)} finding(s):", file=sys.stderr)
        for label, finding in findings:
            print(f"  [{label}] {finding}", file=sys.stderr)
        return 1

    print(
        f"check-repo-hygiene: {len(CHECKS)} check(s) passed over {len(files)} searchable "
        f"file(s) of {len(tracked)} tracked"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
