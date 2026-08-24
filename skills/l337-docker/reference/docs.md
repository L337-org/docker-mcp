# Authoritative documentation

Never assert a flag, a default or an API shape from memory. Docker's CLI moves - flags get
renamed (`--keep-storage` -> `--reserved-space`), commands get deprecated (`docker manifest`), and
defaults change between releases.

## Check the installed binary first

The fastest and most authoritative source is the tool actually installed on this machine:

```bash
docker <command> --help
docker <command> <subcommand> --help
docker --version && docker system info --format '{{.ServerVersion}}'
```

This beats any web page, because it reflects the exact version in use. Web docs describe the
*current* release, which may not be this one.

Two caveats:

- A flag missing from `--help` may still exist on a **newer** daemon than this client, or vice
  versa. Check both versions before concluding a feature is unavailable.
- `--help` can be shadowed by a CLI plugin. Verified case: `docker swarm init --help` prints the
  unrelated `docker-init` plugin's help on Docker Desktop, while the command itself works fine.
  When help output looks like it belongs to a different command, that is what happened - go to the
  web reference.

## Web references

Fetch these when `--help` is insufficient (conceptual questions, file formats, API schemas).

### CLI reference

| Topic | URL |
|---|---|
| CLI index (every command) | https://docs.docker.com/reference/cli/docker/ |
| Compose CLI | https://docs.docker.com/reference/cli/docker/compose/ |
| Buildx CLI | https://docs.docker.com/reference/cli/docker/buildx/ |
| Scout CLI | https://docs.docker.com/reference/cli/docker/scout/ |
| Stack CLI | https://docs.docker.com/reference/cli/docker/stack/ |
| Context CLI | https://docs.docker.com/reference/cli/docker/context/ |

### File formats

| Topic | URL |
|---|---|
| Dockerfile instructions | https://docs.docker.com/reference/dockerfile/ |
| Dockerfile best practices | https://docs.docker.com/build/building/best-practices/ |
| Compose file specification | https://docs.docker.com/reference/compose-file/ |
| Compose application model | https://docs.docker.com/compose/intro/compose-application-model/ |
| Bake file reference | https://docs.docker.com/build/bake/reference/ |

### Concepts

| Topic | URL |
|---|---|
| Swarm stack deployment | https://docs.docker.com/engine/swarm/stack-deploy/ |
| Buildx builders and drivers | https://docs.docker.com/build/builders/ |
| Docker Scout | https://docs.docker.com/scout/ |
| Managing contexts | https://docs.docker.com/engine/manage-resources/contexts/ |
| Engine security model | https://docs.docker.com/engine/security/ |

### APIs

| Topic | URL |
|---|---|
| Engine API (all versions) | https://docs.docker.com/reference/api/engine/ |
| Docker Hub API | https://docs.docker.com/reference/api/hub/latest/ |
| OCI distribution spec | https://github.com/opencontainers/distribution-spec/blob/main/spec.md |
| Legacy registry API v2 | https://distribution.github.io/distribution/spec/api/ |

The Engine API reference is versioned - match it to the daemon's `ApiVersion`
(`docker version --format '{{.Server.APIVersion}}'`), not to the newest published spec. Flag
renames like `keep-storage` -> `reserved-space` are recorded there against the version that
introduced them.

## Using them

1. Try `docker <cmd> --help` first.
2. If the question is conceptual, or `--help` looks shadowed or wrong, fetch the matching URL.
3. Quote the version the answer applies to.
4. If a claim cannot be confirmed from either, **say so** rather than filling the gap with a
   plausible flag name. An invented flag fails at the worst moment - mid-deploy, on someone else's
   machine.
