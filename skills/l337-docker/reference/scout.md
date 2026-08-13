# Docker Scout - CVEs, SBOMs, base images

A CLI plugin. Confirm it exists first: `docker scout version`.

**Scout needs `docker login`.** Unauthenticated, it returns thin or empty results rather than a
clear error - so sparse output is far more often an auth problem than a clean image. Check
`docker system info --format '{{.Registry}}'` and `~/.docker/config.json` for an auth entry, and
if results look empty, say "this may be unauthenticated" rather than reporting zero CVEs.

Scout analyses images from the local store or straight from a registry (`docker scout cves
alpine:3.20` works without pulling first). It can also target a directory or an archive.

## Triage order

Start cheap and narrow down. Running the full `cves` report first buries the answer in noise.

### 1. Quickview - one screen

```bash
docker scout quickview <image>
docker scout quickview fs://.                             # a local filesystem/project
```

Counts by severity plus base-image status. If everything is zero and the user just wants
reassurance, stop here.

### 2. Actionable CVEs only

```bash
docker scout cves <image> --only-severity critical,high --only-fixed
```

**Severity names are case-sensitive and a non-matching one fails silently.** `--only-severity
CRITICAL` exits 0 and prints "No vulnerable packages detected" on an image that `critical` reports
three critical CVEs for: Scout matches the string exactly and filters everything out otherwise.
Uppercase is the spelling Scout uses in its own output (`✗ CRITICAL CVE-2026-42496`), so it is the
easy mistake to make, and the result looks like a clean scan rather than an error. Always pass
these lowercase: `critical`, `high`, `medium`, `low`, `unspecified`.

`--exit-code` does not protect you here: it counts what survived the filter, so
`-e --only-severity CRITICAL` exits 0 on the same image that `-e --only-severity critical`
exits 2 on. The scan did run either way (the `packages` count is identical); only the filter
differed.

`--only-fixed` is the important half: a critical with no upstream fix available is not something
the user can act on today, and mixing the two makes the report look unmanageable. Report fixable
criticals/highs first, and mention the unfixable count separately.

### 3. Separate your problem from the base image's

```bash
docker scout cves <image> --only-severity critical,high --ignore-base
```

`--ignore-base` filters out everything inherited from the base image. CVEs that appear in the
unfiltered run but not this one are **base-image** issues - the fix is a base bump, not a package
patch. This distinction changes the recommendation entirely, so make it explicitly.

Further narrowing:

```bash
docker scout cves <image> --only-package-type deb,apk        # OS packages only
docker scout cves <image> --only-package 'openssl|libssl'
docker scout cves <image> --format sarif --output scan.sarif  # for CI / code scanning
docker scout cves <image> --format packages           # Scout's own default, grouped by package
```

Write large reports to a **file** with `--output` and summarise. Never stream a full CVE dump into
the context.

## Comparing two images

```bash
docker scout compare <new-image> --to <old-image> \
  --only-severity critical,high --ignore-unchanged
```

`--ignore-unchanged` drops carried-forward findings so only the delta remains. Read it in three
buckets and report them separately:

- **resolved** - in old, gone in new
- **new** - absent in old, present in new; these are regressions and the reason to hold a release
- **unchanged** - carried forward

`compare` is marked experimental and its output format has moved between versions; parse
defensively and prefer reporting what it printed over asserting a schema.

## Base-image recommendations

```bash
docker scout recommendations <image>
docker scout recommendations <image> --only-refresh        # same major/minor, newer patch
docker scout recommendations <image> --only-update         # different major/minor
```

A **refresh** is low-risk (same release line, newer patches). An **update** crosses a major/minor
boundary and can break the build - never present the two as equivalent.

Do not take a recommendation on faith. Verify it actually helps before proposing it:

```bash
docker scout compare <candidate-base> --to <current-base> --only-severity critical,high
docker buildx imagetools inspect <candidate-base> --raw | jq '.manifests[].platform'
```

A refresh that fixes three highs and introduces four is not progress, and a candidate that does
not publish your build platforms is not viable regardless of its CVE count.

## SBOM

```bash
docker scout sbom <image>
docker scout sbom <image> --format spdx --output sbom.spdx.json
docker scout sbom <image> --format list                   # human-readable package list
```

SBOMs are large. Always `--output` to a file and report the path plus a summary (package count,
ecosystems present); never inline the document.

Use it to answer "does this image contain package X, and at what version" - which is the fastest
response to a newly-published CVE across a fleet.

## Reporting

Group by what the user can do, not by severity alone:

1. Fixable critical/high introduced by **this image** → bump the package, give the version.
2. Fixable critical/high from the **base** → bump the base, give the exact `FROM` line.
3. Unfixable → note the count; there is no action, so do not lead with it.

Give the concrete one-line change. "Update openssl" is not actionable; `openssl 3.0.11 → 3.0.13`
is.
