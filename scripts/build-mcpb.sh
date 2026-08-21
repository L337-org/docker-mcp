#!/usr/bin/env bash
#
# build-mcpb.sh - pack a local Claude Desktop Extension (.mcpb) bundle for testing.
#
# A developer convenience, NOT a CI tool. CI packs the official release bundle via the mcpb
# job in .github/workflows/publish.yaml (which stamps the version from the release tag and
# attaches the asset to the GitHub Release). This script mirrors that pack step so you can
# produce an installable bundle locally to smoke-test in Claude Desktop.
#
# Bundles are stamped as dev builds: the manifest version becomes
# <pyproject-version>-dev.<short-commit>[.dirty] (e.g. 2.1.4-dev.693de829.dirty), so a locally
# built bundle is never mistaken for a release in Claude Desktop's extension list, and the commit
# it was built from is identifiable even though there is no tag. ".dirty" means the working tree
# had uncommitted changes, so the commit id alone does not describe what was packed. The manifest
# is stamped only for the duration of the pack and restored afterwards (including on failure).
#
# Usage:
#   scripts/build-mcpb.sh [name]
#
#   name   Optional output filename (a ".mcpb" extension is added if missing). Relative names
#          land in dist/; an absolute or ./-prefixed path is used as-is. If omitted, defaults to
#          dist/docker-mcp-server-<dev-version>.mcpb, falling back to -1, -2, ... when that file
#          exists.
#
# Options:
#   -f, --force   Overwrite the output file if it already exists (only meaningful with [name];
#                 the default auto-incrementing name never collides).
#   -h, --help    Show this help and exit.
#
# Environment:
#   MCPB   Override the mcpb invocation (e.g. MCPB="mcpb" or MCPB="bunx @anthropic-ai/mcpb").
#
# Runs on macOS and Linux.

set -euo pipefail

# --- locate the repo root (so the script works from any cwd) ------------------------------------
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

usage() {
	# Print the header comment block (from line 3 to the first non-comment line) as help text,
	# so editing the header can't desync the help output from it.
	awk 'NR < 3 { next } !/^#/ { exit } { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

die() {
	printf 'error: %s\n' "$1" >&2
	exit 1
}

# --- parse args ---------------------------------------------------------------------------------
force=0
name=""
while [ $# -gt 0 ]; do
	case "$1" in
		-h|--help) usage; exit 0 ;;
		-f|--force) force=1; shift ;;
		--) shift; break ;;
		-*) die "unknown option: $1 (try --help)" ;;
		*)
			[ -z "$name" ] || die "unexpected extra argument: $1 (only one name is accepted)"
			name="$1"; shift ;;
	esac
done
# Anything after `--` is a positional arg; apply the same single-name rule (no option parsing).
for arg in "$@"; do
	[ -z "$name" ] || die "unexpected extra argument: $arg (only one name is accepted)"
	name="$arg"
done

# --- resolve the mcpb invocation ----------------------------------------------------------------
# Prefer an explicit override, then a globally-installed `mcpb`, then `npx @anthropic-ai/mcpb`
# (matching CI). If none is usable, explain how to fix it.
mcpb_cmd=()
if [ -n "${MCPB:-}" ]; then
	# shellcheck disable=SC2206
	mcpb_cmd=($MCPB)
elif command -v mcpb >/dev/null 2>&1; then
	mcpb_cmd=(mcpb)
elif command -v npx >/dev/null 2>&1; then
	mcpb_cmd=(npx -y @anthropic-ai/mcpb)
else
	cat >&2 <<'EOF'
error: the `mcpb` packer was not found.

The .mcpb bundle is packed with Anthropic's mcpb CLI. Install one of these, then re-run:

  • Node + npx (used by CI):   already have Node? `npx -y @anthropic-ai/mcpb --version`
                               install Node:  https://nodejs.org  (or `brew install node`)
  • mcpb on your PATH:         `npm install -g @anthropic-ai/mcpb`

Or point this script at an existing install:

  MCPB="bunx @anthropic-ai/mcpb" scripts/build-mcpb.sh

mcpb docs: https://github.com/anthropics/mcpb
EOF
	exit 1
fi

# --- read the version from pyproject.toml -------------------------------------------------------
pyproject="$repo_root/pyproject.toml"
[ -f "$pyproject" ] || die "pyproject.toml not found at $pyproject"
version="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$pyproject" | head -n1)"
[ -n "$version" ] || die "could not parse version from $pyproject"

# Warn (don't fail) if manifest.json has drifted from pyproject - CI restamps it at release time,
# so a local test bundle still packs fine, but a mismatch is worth surfacing. pyproject is the
# source of truth (tests/test_pyproject_pins.py asserts the manifest matches it), so the dev stamp
# below is derived from pyproject and overrides the drifted manifest value rather than inheriting
# it - a local bundle should never carry a version the project does not claim.
manifest="$repo_root/manifest.json"
if [ -f "$manifest" ]; then
	manifest_version="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$manifest" | head -n1)"
	if [ -n "$manifest_version" ] && [ "$manifest_version" != "$version" ]; then
		printf 'warning: manifest.json version (%s) != pyproject.toml version (%s); the dev stamp uses %s.\n' \
			"$manifest_version" "$version" "$version" >&2
	fi
fi

# --- derive the dev build version ---------------------------------------------------------------
# A local bundle is not a release, so mark it: <version>-dev.<short-commit>[.dirty]. The pre-release
# suffix is valid semver and sorts below the release of the same version, and the commit id says
# what it was likely built from (".dirty" = uncommitted changes, so the commit id isn't the whole
# story). Outside a git checkout (e.g. an unpacked sdist) fall back to a bare "-dev".
build_version="$version-dev"
if command -v git >/dev/null 2>&1 && git -C "$repo_root" rev-parse --git-dir >/dev/null 2>&1; then
	commit="$(git -C "$repo_root" rev-parse --short=8 HEAD 2>/dev/null || true)"
	if [ -n "$commit" ]; then
		build_version="$version-dev.$commit"
		# --porcelain honors .gitignore, so dist/ and other ignored output don't count as dirty.
		if [ -n "$(git -C "$repo_root" status --porcelain 2>/dev/null)" ]; then
			build_version="$build_version.dirty"
		fi
	fi
fi

# --- decide the output path ---------------------------------------------------------------------
dist_dir="$repo_root/dist"
mkdir -p "$dist_dir"

resolve_path() {
	# Absolute or explicitly-relative (./, ../) paths are honored verbatim; a bare name lands in dist/.
	case "$1" in
		/*|./*|../*) printf '%s' "$1" ;;
		*) printf '%s/%s' "$dist_dir" "$1" ;;
	esac
}

if [ -n "$name" ]; then
	# An explicit name: add the extension if missing, then refuse to clobber unless --force.
	case "$name" in *.mcpb) ;; *) name="$name.mcpb" ;; esac
	out="$(resolve_path "$name")"
	mkdir -p "$(dirname "$out")"
	if [ -e "$out" ] && [ "$force" -ne 1 ]; then
		die "$out already exists (use --force to overwrite)"
	fi
else
	# Default name with auto-incrementing suffix so repeated builds never overwrite each other.
	out="$dist_dir/docker-mcp-server-${build_version}.mcpb"
	n=1
	while [ -e "$out" ]; do
		out="$dist_dir/docker-mcp-server-${build_version}-${n}.mcpb"
		n=$((n + 1))
	done
fi

# --- stamp the dev version into the manifest ----------------------------------------------------
# Mirrors what the release workflow does with the tag, except this is strictly temporary: the
# manifest is restored by the EXIT trap, so a dev build never leaves the working tree modified.
stamp_manifest() {
	local target="$1" tmp status=0
	tmp="$(mktemp)"
	# Capture the writer's status rather than letting `set -e` abort mid-function: an early exit
	# there would skip the checks below and leak $tmp. Every failure path frees it explicitly.
	if command -v jq >/dev/null 2>&1; then
		jq --arg v "$target" '.version = $v' "$manifest" > "$tmp" || status=$?
	else
		# Anchored on the top-level two-space indent so "manifest_version" can't be hit.
		sed 's/^\(  "version"[[:space:]]*:[[:space:]]*\)"[^"]*"/\1"'"$target"'"/' "$manifest" > "$tmp" || status=$?
	fi
	if [ "$status" -ne 0 ] || [ ! -s "$tmp" ]; then
		rm -f "$tmp"
		die "failed to stamp version into $manifest (is it valid JSON?)"
	fi
	cat "$tmp" > "$manifest" || { rm -f "$tmp"; die "failed to write $manifest"; }
	rm -f "$tmp"
}

if [ -f "$manifest" ]; then
	manifest_backup="$(mktemp)"
	cp "$manifest" "$manifest_backup"
	# shellcheck disable=SC2064  # expand $manifest_backup now, not at trap time
	trap "cp '$manifest_backup' '$manifest'; rm -f '$manifest_backup'" EXIT
	stamp_manifest "$build_version"
	packed_version="$(sed -n 's/^  "version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$manifest" | head -n1)"
	[ "$packed_version" = "$build_version" ] || die "version stamp did not apply to $manifest"
fi

# --- pack ---------------------------------------------------------------------------------------
printf 'Packing %s (version %s) ...\n' "$out" "$build_version"
(cd "$repo_root" && "${mcpb_cmd[@]}" pack . "$out")

# --- report -------------------------------------------------------------------------------------
# Write a .sha256 next to the bundle (mirrors CI) and print a short summary.
if command -v shasum >/dev/null 2>&1; then
	(cd "$(dirname "$out")" && shasum -a 256 "$(basename "$out")" > "$out.sha256")
elif command -v sha256sum >/dev/null 2>&1; then
	(cd "$(dirname "$out")" && sha256sum "$(basename "$out")" > "$out.sha256")
fi

size="$(du -h "$out" | cut -f1 | tr -d '[:space:]')"
printf '\nBuilt %s (%s)\n' "$out" "$size"
printf 'version %s  (dev build - not a release)\n' "$build_version"
[ -f "$out.sha256" ] && printf 'sha256 %s\n' "$out.sha256"
printf '\nTest it: open Claude Desktop and install the bundle, or inspect with:\n  %s info %s\n' \
	"${mcpb_cmd[*]}" "$out"