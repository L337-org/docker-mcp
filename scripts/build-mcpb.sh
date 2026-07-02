#!/usr/bin/env bash
#
# build-mcpb.sh — pack a local Claude Desktop Extension (.mcpb) bundle for testing.
#
# A developer convenience, NOT a CI tool. CI packs the official release bundle via the mcpb
# job in .github/workflows/release.yaml (which stamps the version from the release tag and
# attaches the asset to the GitHub Release). This script mirrors that pack step so you can
# produce an installable bundle locally to smoke-test in Claude Desktop.
#
# Usage:
#   scripts/build-mcpb.sh [name]
#
#   name   Optional output filename (a ".mcpb" extension is added if missing). Relative names
#          land in dist/; an absolute or ./-prefixed path is used as-is. If omitted, defaults to
#          dist/docker-mcp-server-<version>.mcpb, falling back to -1, -2, … when that file exists.
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
	# Print the header comment block (between the shebang and `set -euo`) as help text.
	sed -n '3,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
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

# Warn (don't fail) if manifest.json has drifted from pyproject — CI restamps it at release time,
# so a local test bundle still packs fine, but a mismatch is worth surfacing.
manifest="$repo_root/manifest.json"
if [ -f "$manifest" ]; then
	manifest_version="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$manifest" | head -n1)"
	if [ -n "$manifest_version" ] && [ "$manifest_version" != "$version" ]; then
		printf 'warning: manifest.json version (%s) != pyproject.toml version (%s); packing as-is.\n' \
			"$manifest_version" "$version" >&2
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
	out="$dist_dir/docker-mcp-server-${version}.mcpb"
	n=1
	while [ -e "$out" ]; do
		out="$dist_dir/docker-mcp-server-${version}-${n}.mcpb"
		n=$((n + 1))
	done
fi

# --- pack ---------------------------------------------------------------------------------------
printf 'Packing %s …\n' "$out"
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
[ -f "$out.sha256" ] && printf 'sha256 %s\n' "$out.sha256"
printf '\nTest it: open Claude Desktop and install the bundle, or inspect with:\n  %s info %s\n' \
	"${mcpb_cmd[*]}" "$out"