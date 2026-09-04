#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml==6.0.3"]
# ///
"""Assert every CI job declares a timeout, and that each one is a real bound.

A job with no `timeout-minutes` inherits the platform's six-hour default.  That matters less
because a hung job wastes six hours than because it holds the concurrency group while it does:
runs queued behind it are silently discarded, and nothing reports that they were.  The symptom is
a check that never appears rather than one that fails, which is the hardest kind to notice.

A SCRIPT RATHER THAN A TEST, DELIBERATELY.  The same two checks are wanted in repositories that
are not primarily Python and have no pytest to hang them off, and a guard that exists in one
shape here and another shape there is one that drifts.  The PEP 723 header means it needs only
`uv` on the runner - no project install, no dependency group - so vendoring it elsewhere is a
file copy.

Run it with `uv run scripts/check-ci-hygiene.py`.  Exit status is 0 when every job passes, 1 when
any does not, and 2 when the scan found nothing to check - see below for why that is not 0.
"""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

# Sized so a bound is meaningful without being flaky.  The upper limit matters as much as the
# lower one: a timeout large enough never to fire is indistinguishable from having none, and the
# usual way one gets there is a flaky job whose timeout is raised until it stops failing.
MIN_MINUTES = 1
MAX_MINUTES = 60

# The scan must not be able to report success by finding nothing.  A skipped check and a passed
# check look identical from outside, so coverage would shrink invisibly the day a path changed or
# a parse silently returned an empty mapping.  This floor is well under the real count, so an
# ordinary job being added or removed does not trip it while a broken scan does.
MIN_JOBS_EXPECTED = 10

WORKFLOWS = pathlib.Path(".github/workflows")


def workflow_jobs(root):
    """Every job in every workflow file, as `(path, job id, job body)`.

    A workflow that cannot be read, does not parse, has a non-mapping top level, or declares no
    jobs is reported rather than skipped: an unexaminable workflow is a finding, not an absence
    of one.

    args:
        root: repository root to scan under.

    returns:
        A `(jobs, problems)` pair, where `jobs` is a list of triples and `problems` is a list of
        human-readable strings describing files that could not be read as workflows.
    """
    jobs = []
    problems = []
    directory = root / WORKFLOWS
    if not directory.is_dir():
        return jobs, [f"{WORKFLOWS} is not a directory, so there are no workflows to check"]
    for path in sorted(directory.iterdir()):
        if path.suffix not in (".yml", ".yaml"):
            continue
        relative = path.relative_to(root)
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            problems.append(f"{relative} is not valid YAML: {exc}")
            continue
        except (OSError, UnicodeDecodeError) as exc:
            # A file that cannot be read is a finding, not a crash. Letting it raise would
            # abort the whole scan on the first bad file, reporting a traceback instead of
            # which workflow could not be checked - and leaving the remaining files
            # unexamined, which is the outcome this script exists to prevent.
            problems.append(f"{relative} could not be read: {exc}")
            continue
        if not isinstance(document, dict):
            problems.append(f"{relative} does not parse as a mapping")
            continue
        declared = document.get("jobs")
        if not isinstance(declared, dict) or not declared:
            problems.append(f"{relative} declares no jobs")
            continue
        for name, body in declared.items():
            if not isinstance(body, dict):
                problems.append(f"{relative}:{name} does not parse as a mapping")
                continue
            jobs.append((relative, name, body))
    return jobs, problems


def timeout_problem(relative, name, body):
    """Why this job's timeout is unacceptable, or `None` if it is fine.

    A `${{ }}` expression is refused even though Actions accepts one, because its value is
    unknowable here: a timeout resolving to 360 at run time would satisfy any static bound while
    leaving the job effectively unbounded.  Nothing here needs an expression, so the bound stays
    real; if something ever does, that is a deliberate change to make here rather than to work
    around.

    args:
        relative: workflow path, for the message.
        name: job id, for the message.
        body: the parsed job mapping.

    returns:
        A string describing the problem, or `None`.
    """
    minutes = body.get("timeout-minutes")
    if minutes is None:
        return (
            f"{relative}:{name} declares no timeout-minutes, so it inherits the six-hour "
            f"platform default and can hold the concurrency group"
        )
    if isinstance(minutes, bool):
        return f"{relative}:{name} has timeout-minutes: {minutes!r}, which is a boolean"
    if isinstance(minutes, str):
        if not re.fullmatch(r"\s*\d+\s*", minutes):
            return (
                f"{relative}:{name} has timeout-minutes: {minutes!r}, which is not a literal "
                f"number.  An expression is valid Actions, but its value cannot be checked "
                f"here, so the bound would stop meaning anything"
            )
        minutes = int(minutes)
    if not isinstance(minutes, int):
        return f"{relative}:{name} has timeout-minutes: {minutes!r}, which is not a number"
    if not MIN_MINUTES <= minutes <= MAX_MINUTES:
        return (
            f"{relative}:{name} has timeout-minutes: {minutes}, outside {MIN_MINUTES}-"
            f"{MAX_MINUTES}.  A bound that never fires is the same as no bound"
        )
    return None


def main():
    """Report every job whose timeout is missing or unusable.

    returns:
        A process exit status: 0 clean, 1 findings, 2 the scan itself is broken.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    jobs, problems = workflow_jobs(root)

    if len(jobs) < MIN_JOBS_EXPECTED and not problems:
        print(
            f"check-ci-hygiene: found only {len(jobs)} job(s), expected at least "
            f"{MIN_JOBS_EXPECTED} - discovery is broken rather than the workflows being clean",
            file=sys.stderr,
        )
        return 2

    findings = [problem for problem in problems]
    findings += [
        problem for relative, name, body in jobs if (problem := timeout_problem(relative, name, body)) is not None
    ]

    if findings:
        print(f"check-ci-hygiene: {len(findings)} finding(s) across {len(jobs)} job(s):", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1

    print(f"check-ci-hygiene: {len(jobs)} job(s) checked, every one bounded within {MIN_MINUTES}-{MAX_MINUTES} minutes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
