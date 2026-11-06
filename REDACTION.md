# Credential redaction

**Redaction reduces accidental exposure. It is not access control.**  It keeps credentials out of what
this server returns to an agent that reaches Docker only through this server, so the agent does not
see them by accident.  It cannot stop an agent that goes looking: an agent that can run commands in a
container (`container_exec`, `compose_exec`) can print a credential in a form no filter recognises.
Run it with `DOCKER_MCP_SERVER_READONLY`, or mark a redacted host `(ro,redact)`, which removes those
routes.

> **Status: not yet functional.**  This build reads and reports the settings below, but does not
> redact anything yet: output from every host is returned unredacted, and the server says so in a
> configuration warning.

## Turning it on

Redaction is off by default.  There are two ways to turn it on, and they combine.

- **Every host:** `DOCKER_MCP_SERVER_REDACT=1` (also `true`, `yes` or `on`).
- **Chosen hosts:** the `redact` option on a host in `DOCKER_MCP_SERVER_HOSTS`, so that a production
  daemon is redacted while a local one stays unfiltered:

  ```text
  DOCKER_MCP_SERVER_HOSTS=dev=local, prod=ssh://ops@prod.example.com(ro,redact)
  ```

  A host's options go in one comma-separated bracket, `(ro,redact)`, or one per bracket,
  `(ro)(redact)`, and the two forms mix freely.  They are case-insensitive and may come in any
  order, except that `tls=<dir>` takes the rest of its bracket, so that a certificate directory
  containing a comma still works, and therefore has to come last in a list:
  `(ro,redact,tls=/etc/docker/prod)`.  A malformed bracket, such as `(ro,)` or one that is never
  closed, stops the server at startup with an error naming the host.

With the global switch on, every host is redacted whatever its own options say.

## Which names are treated as credentials

Values of variables with credential-like names are treated as credentials, in container, image and
service environments and in Compose `.env` files.  The built-in patterns are case-insensitive globs:

`*PASSWORD*`, `*PASSWD*`, `*PASSPHRASE*`, `*PASS`, `*_PASS_*`, `*SECRET*`, `*TOKEN*`, `*KEY*`,
`*CERT*` and `*CREDENTIAL*`.

`DOCKER_MCP_SERVER_REDACT_NAMES` changes that list.  It is comma-separated, and each entry is a
variable name or a glob using `*`, `?` and `[...]`, matched without regard to case:

```text
DOCKER_MCP_SERVER_REDACT_NAMES=UPSTREAM_*, *_REF, !SORT_KEY
```

- An entry adds names to the built-in patterns: `UPSTREAM_*` treats `UPSTREAM_URL` as a credential.
- An entry starting `!` excludes names, whichever pattern would otherwise match them: `!SORT_KEY`
  stops a harmless `SORT_KEY` being treated as one.  An exclusion also wins over an addition.
- It takes names only, never values.  An entry containing `=` stops the server at startup, and the
  error gives the entry's position, not its text, in case it holds a credential.

The names setting does nothing on its own.  Setting it with redaction on for no host logs a warning
at startup and changes nothing.

## What is redacted, and how

Two things are replaced, wherever they appear in a redacted host's output, dict keys included:

- **Values the server knows**, read from where the daemon reports them under a credential name, in
  their raw form and as base64 (standard and URL-safe, padded or not, and inside a larger base64 blob
  at any alignment), base32, hex in either case, and URL-encoded.  A value of six characters or fewer,
  base64-encoded inside a larger blob, can be missed there: the part of its encoding that does not
  depend on its neighbours is too short to match safely.
- **Values found by their shape or context**: PEM private keys, GitHub, Stripe and Slack tokens, AWS
  access key IDs and JWTs; `Authorization`, `Proxy-Authorization`, cookie and API-key header values;
  passwords in a URL's user info or a connection string; the value assigned to a credential name, when
  it is 8 or more characters and looks random; the value after a credential-named command-line flag,
  such as `--password`, whatever it looks like, including where Docker reports a command as a list; and
  any other run of 32 or more base64 characters that looks random.  Where two of these overlap, the
  whole of both is replaced as one.  Hex alone is never treated as a credential by these rules, nor is a run longer than
  16 KB, and lines wrapped by a base64 encoder count as one run.  A value found this way is remembered,
  so its encoded forms are matched from then on.

Each replacement is a marker, `[REDACTED:<token>]`, where the token is 22 characters.  Every marker is
different, even for the same credential in the same result, so nothing in the output shows that two
places hold the same value; and a marker says nothing about the credential's format or how it was
found.  Markers are valid only in the server process that issued them.

## Fields the rules leave alone

These fields in Docker's own output look random but are public, so the shape rules never replace
their values; a value the server knows is still replaced in them:

`TrustRoot`, `CertIssuerPublicKey`, `CertIssuerSubject` and `SandboxKey`.

## Limits

The server keeps the values it redacts in memory, and stops growing that store at fixed limits:

| Limit | Values read from the daemon | Values found by the rules |
| --- | --- | --- |
| Longest value matched | No limit | 16 KB; a longer run is left alone |
| Values stored | No limit of their own | 100,000 |
| Bytes stored, each value and its encoded forms | No limit of their own | 64 MB |
| Overall budget, both kinds together | 256 MB | |

Past either limit on found values, a new one is still replaced where a rule finds it, but not
remembered, so its other forms are not matched; this is logged once at `WARNING`, naming the host.  At
the overall budget, which only an abnormal harvest reaches, every call to a redacted host is refused
with an error suggesting a restart; it is logged once at `ERROR`.  A restart empties the store and
makes every earlier marker invalid.

## Checking what is in effect

- **`tool_list`** and the `docker-mcp://tool-catalog` resource report, under `switches`:
  `DOCKER_MCP_SERVER_REDACT`, `DOCKER_MCP_SERVER_REDACT_NAMES` as the resolved name list (`builtin`,
  `added` and `excluded`), and `redacted_hosts`, the hosts whose output is redacted.
- **`host_list`** and the `docker-mcp://hosts` resource give each host a `redact` field: whether
  its output is redacted, by its own option or by the global switch.

## Configuration warnings

The server warns:

- for each redacted host that is neither marked `ro` nor covered by `DOCKER_MCP_SERVER_READONLY`,
  whether it is redacted by its own option or by `DOCKER_MCP_SERVER_REDACT`.  On such a host exec and
  the tools that write daemon data to files stay available, and either can show an agent a credential
  that redaction cannot catch;
- when `DOCKER_MCP_SERVER_REDACT_NAMES` is set but redaction is on for no host.

Because an MCP client rarely shows a server's stderr to anyone, each warning goes three places, as
`DOCKER_MCP_SERVER_DISABLE` mistakes do: a `WARNING` line on stderr at startup, an entry in
`tool_list`'s `configuration_warnings`, and a line at the top of the server instructions telling the
agent to check `tool_list` and tell the user.
