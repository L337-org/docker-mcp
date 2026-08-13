# Security workflows

All read-only. Report findings and the concrete fix; never apply changes as part of an audit.

## Audit running containers for risky configuration

1. Get the set: `docker ps --format '{{.Names}}'`.
2. For each, inspect the runtime config:
   ```bash
   docker inspect <c> --format '{{json .HostConfig}}' | jq '{
     Privileged, NetworkMode, PidMode, IpcMode, CapAdd, CapDrop,
     SecurityOpt, Binds, Memory, NanoCpus, ReadonlyRootfs
   }'
   docker inspect <c> --format '{{json .Config}}' | jq '{User, ExposedPorts}'
   ```

Flag, roughly in severity order:

| Finding | Why it matters |
|---|---|
| `Privileged: true` | Effectively root on the host. Highest severity, no exceptions. |
| A bind of `/var/run/docker.sock` | The container can drive the daemon - equivalent to host root. |
| `NetworkMode: host` | No network isolation; binds directly to host interfaces. |
| `PidMode: host` / `IpcMode: host` | Can see and signal host processes / share host memory. |
| `CapAdd` with `SYS_ADMIN`, `NET_ADMIN`, `SYS_PTRACE`, `SYS_MODULE` | Each is close to a privilege escalation on its own. |
| `SecurityOpt` containing `seccomp=unconfined` or `apparmor=unconfined` | Disables the syscall filter. |
| Writable binds of `/`, `/etc`, `/var/run`, or the user's home | Host filesystem tampering. |
| `User` empty in `Config` | Likely running as root. |
| `Memory: 0` and `NanoCpus: 0` | No limits - one container can exhaust the host. |

**`User` is best-effort.** An empty value means no override was given; the image's own `USER` may
still apply. Confirm before calling it a root finding:

```bash
docker exec <c> id -u 2>/dev/null      # 0 means genuinely root
```

Report as a table (container, findings, severity), then name the single most exposed container and
the one highest-priority remediation. Background on the trust model:
https://docs.docker.com/engine/security/

## Review a Dockerfile

Read the current guidance first rather than relying on memory -
https://docs.docker.com/reference/dockerfile/ and
https://docs.docker.com/build/building/best-practices/ - then read the file and check:

**Security**

1. **Unpinned base image** - `FROM image` or `FROM image:latest`. Recommend a specific tag, or a
   digest for anything reproducible.
2. **No `USER` directive** - the image runs as root. Recommend creating a non-root user and
   switching to it before the entrypoint.
3. **Secrets baked into layers** - credentials in `ENV`/`ARG`, a `COPY`'d private key, a token on
   a `RUN` line. **These persist in the image history even if a later layer deletes them.** Flag
   every one, and recommend `--secret` mounts instead. Verify with
   `docker history --no-trunc <image>`.
4. **`ADD` where `COPY` would do** - `ADD` auto-extracts archives and can fetch URLs; both are
   surprising. `COPY` unless you specifically want extraction.

**Correctness and efficiency**

5. **Missing `HEALTHCHECK`** on a long-running service image - without it, orchestrators cannot
   tell started from working, and every "wait until healthy" degrades to "wait until running".
6. **Cache-inefficient layer order** - `COPY . .` before installing dependencies means every
   source edit busts the dependency layer. Copy the manifest (`package.json`, `requirements.txt`,
   `go.mod`), install, *then* copy the source.
7. **Package manager hygiene** - `apt-get install` without `--no-install-recommends`, or without
   `rm -rf /var/lib/apt/lists/*` **in the same `RUN`** (a separate layer does not reclaim it).
8. **No `.dockerignore`** - check for one. Without it the whole directory uploads as build
   context, commonly including `.git`, `node_modules` and `.env`.

Report grouped by severity, security first, each with the offending line and the concrete
replacement. Propose the diff; do not edit the file.

## Audit an image's CVEs

Requires Scout (`reference/scout.md`), and Scout requires `docker login` - sparse output is more
often an auth problem than a clean image.

1. **Quickview** for counts by severity. If everything is zero and the user wants reassurance,
   stop: `docker scout quickview <image>`.
2. **Actionable set only:**
   `docker scout cves <image> --only-severity critical,high --only-fixed`.
   A critical with no available fix is not something the user can act on today - count it
   separately rather than mixing it in.
3. **Separate base-image issues from yours:**
   `docker scout cves <image> --only-severity critical,high --ignore-base`.
   CVEs present in step 2 but absent here are inherited from the base - the fix is a base bump,
   not a package patch. This changes the recommendation entirely, so state it explicitly.
4. Report package, installed version, fixed version, CVE id. Recommend the smallest change that
   clears the high-priority findings.

## Compare two image versions

```bash
docker scout compare <new> --to <old> --only-severity critical,high --ignore-unchanged
```

Split the diff into three buckets and report them separately:

- **Resolved** - present in old, gone in new.
- **New** - absent in old, present in new. These are **regressions** and the reason to hold a
  release.
- **Carried forward** - unchanged.

Then give a recommendation: proceed, hold, or wait for a base refresh. If new criticals appear,
check whether a different base clears them (`docker scout recommendations <new>`). Stop and ask
before any rebuild or rollback.

## Recommend a safer base image

1. `docker scout recommendations <image>` - separate **refresh** (same major/minor, newer patches,
   low risk) from **update** (different major/minor, can break the build). Never present them as
   equivalent.
2. **Verify the candidate actually helps** - a recommendation is a starting point, not a verdict:
   ```bash
   docker scout compare <candidate> --to <current> --only-severity critical,high
   ```
   A refresh that fixes three highs and introduces four is not progress.
3. **Verify it publishes your platforms**, or the build breaks on another architecture:
   ```bash
   docker buildx imagetools inspect <candidate> --raw \
     | jq -r '.manifests[] | select(.platform.os != "unknown") | "\(.platform.os)/\(.platform.architecture)"'
   ```
   (Filter `unknown/unknown` - those are attestations, not platforms.)

Report the recommended base, CVEs resolved, CVEs introduced, and the exact one-line `FROM` change.
Do not modify the Dockerfile.
