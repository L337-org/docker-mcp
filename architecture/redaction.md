<!-- Architecture note: implementation detail for contributors and assistants.
     Not user documentation - see REDACTION.md for that. -->

# Credential redaction

Deep detail behind the summaries in [../AGENTS.md](../AGENTS.md).
Read this before changing anything under `docker_mcp/redaction/`, or the `redact` host option in
`docker_mcp/_hosts.py`.  User documentation, including what the mode cannot catch, is
[../REDACTION.md](../REDACTION.md).

## Package layout

`docker_mcp/redaction/` lives at the package root, not under `tools/`, so `server.py` can import it
without importing `docker_mcp.tools` (the same reason `_hosts.py` and `_env.py` live there).  One
module per concern:

- **`settings.py`** - the global switch (`DOCKER_MCP_SERVER_REDACT`), the names setting
  (`DOCKER_MCP_SERVER_REDACT_NAMES`), and which hosts are redacted.  `load()` runs in
  `docker_mcp/__init__.py` after `_hosts.load()`, because `host_redacted` reads each host's `redact`
  option, and before the tools import.  `redacts(host)` is the one place the global switch and a
  host's own option are combined; `host_redacted(label)` resolves a label and calls it.  A malformed
  names value fails fast with `SystemExit(1)`, like a malformed `DOCKER_MCP_SERVER_HOSTS`.

## Where the settings surface

- `server._switch_report()` merges `settings.report()` into the `switches` that `tool_list` and the
  tool catalog return, so both show the same thing.
- `host_list` and the startup roster of other hosts report `redact` per host from `settings.redacts`,
  the global switch or the host's own option.  `Host.redact` is the per-host option only; never read it directly to decide whether a
  call is redacted.
- `startup_preflight` prints `settings.startup_warnings(readonly=READONLY)` next to the
  `DOCKER_MCP_SERVER_DISABLE` warnings.  READONLY is passed in rather than read, so the settings
  module never imports `server.py`.

## The host option grammar

`redact` is one of the options in a host's trailing brackets, parsed by `_hosts._parse_markers`.
Brackets may hold one option or a comma-separated list, so splitting the host list on commas has to
skip commas inside brackets (`_split_top_level`), and the bare single-host form is detected by an `=`
outside brackets (`_has_top_level_equals`), so that `(tls=<dir>)` does not make a bare endpoint read
as a `label=endpoint` list.  The grammar itself is described in [hosts.md](hosts.md).

## Extension points

Each is a single table, so adding a case is a one-line change plus its test.

### Adding a built-in credential name pattern

1. Add the glob to `BUILTIN_NAME_PATTERNS` in `docker_mcp/redaction/settings.py`, with a comment if
   it is not obviously a credential word.
2. Add a name it catches to `test_the_built_in_patterns_harvest_credential_names` in
   `tests/test_redaction_settings.py`, and any ordinary name it must not catch to
   `test_the_built_in_patterns_leave_ordinary_names_alone`.
3. Add it to the pattern list in `REDACTION.md`, in the same order;
   `test_redaction_md_lists_exactly_the_built_in_patterns` fails until the two agree.

### Adding a host option

1. Add the option to `_apply_bracket` in `docker_mcp/_hosts.py`, and a field to `_Options` and `Host`.
2. Pass it through in `_make_host`, and add it to the unknown-option message.
3. Add parsing cases to `tests/test_hosts.py`, in both the one-bracket and one-per-bracket forms.
4. Report it in `host_list` and update the grammar in [hosts.md](hosts.md) and the README.
