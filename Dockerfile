# syntax=docker/dockerfile:1
#
# Container image for the docker-mcp MCP server — an *additional* distribution channel alongside the
# uvx-from-git install (which is unchanged). The only host prerequisite to run this image is Docker
# itself; no Python or uv needed. See README "Run as a container".
#
# Build variants are selected with build args (driven by .github/workflows/images.yaml):
#   full      INSTALL_CLI=1 INSTALL_SCOUT=1                        docker CLI + compose + buildx + scout (published default, :latest)
#   no-scout  INSTALL_CLI=1 INSTALL_SCOUT=0 DISABLE_DOMAINS=scout  as full without scout (published option)
#   lite      INSTALL_CLI=0                                        docker-py SDK only (buildable, not published)
#
# The CLI-backed tool domains (Compose/Stack/Buildx/Scout/Context) shell out to the `docker` CLI and
# its plugins. `no-scout` not only omits the scout plugin but also sets DOCKER_MCP_DISABLE=scout so the
# scout *tools* never register — the agent isn't offered tools whose plugin is absent. `lite` omits the
# CLI entirely; its CLI-backed tools report the plugin as unavailable (has_plugin / require_plugin in
# tools/_cli.py) rather than failing hard.

ARG PYTHON_VERSION=3.14
# Pin the Debian release (bookworm) explicitly: the Docker apt repo publishes per-release dirs and
# bookworm is reliably available, whereas a newer default base (trixie) may not be yet.
ARG DEBIAN_RELEASE=bookworm

# ---- builder: resolve the locked deps + install the package into a self-contained venv ----
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_RELEASE} AS builder

# uv provides fast, lockfile-faithful installs; copy just the binaries from the official image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# `--no-editable` copies the package into site-packages so the runtime stage needs no source tree;
# `--frozen` honours uv.lock exactly; `--no-dev` drops the dev group.
COPY pyproject.toml uv.lock README.md ./
COPY docker_mcp ./docker_mcp
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---- runtime ----
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_RELEASE} AS runtime

ARG INSTALL_CLI=1
ARG INSTALL_SCOUT=1
ARG DEBIAN_RELEASE=bookworm
# Domains to drop from the tool surface at startup (DOCKER_MCP_DISABLE). The `no-scout` variant passes
# `scout` so its absent-plugin tools never register; empty for `full` (a no-op — see _parse_domains).
ARG DISABLE_DOMAINS=""
# Scout has no apt package and its upstream install.sh refuses to run in a minimal image (its
# docker-presence probe fails and it exits 0, silently installing nothing), so we drop the release
# binary straight into the CLI plugins dir. Bump to pick up new scout versions.
ARG SCOUT_VERSION=1.21.0

# Install the docker CLI + compose/buildx plugins (and optionally scout) for the CLI-backed domains.
# Skipped entirely for the `lite` variant (INSTALL_CLI=0).
RUN if [ "$INSTALL_CLI" = "1" ]; then \
        set -eux; \
        apt-get update; \
        apt-get install -y --no-install-recommends ca-certificates curl gnupg; \
        install -m 0755 -d /etc/apt/keyrings; \
        curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc; \
        chmod a+r /etc/apt/keyrings/docker.asc; \
        printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian %s stable\n' \
            "$(dpkg --print-architecture)" "$DEBIAN_RELEASE" > /etc/apt/sources.list.d/docker.list; \
        apt-get update; \
        apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin docker-buildx-plugin; \
        if [ "$INSTALL_SCOUT" = "1" ]; then \
            scout_arch="$(dpkg --print-architecture)"; \
            mkdir -p /usr/libexec/docker/cli-plugins; \
            curl -fsSL "https://github.com/docker/scout-cli/releases/download/v${SCOUT_VERSION}/docker-scout_${SCOUT_VERSION}_linux_${scout_arch}.tar.gz" -o /tmp/docker-scout.tgz; \
            tar -xzf /tmp/docker-scout.tgz -C /usr/libexec/docker/cli-plugins docker-scout; \
            chmod +x /usr/libexec/docker/cli-plugins/docker-scout; \
            rm /tmp/docker-scout.tgz; \
        fi; \
        apt-get purge -y --auto-remove curl gnupg; \
        rm -rf /var/lib/apt/lists/*; \
    fi

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DOCKER_MCP_IN_CONTAINER=1 \
    DOCKER_MCP_DISABLE=${DISABLE_DOMAINS} \
    DOCKER_HOST=unix:///var/run/docker.sock

# stdio transport: the MCP client drives the process over stdin/stdout (`docker run -i`).
ENTRYPOINT ["docker-mcp"]
