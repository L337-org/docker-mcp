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

- **`store.py`** - `ValueStore`, every value redacted, once per host, with its tier (harvested or
  discovered), and **every limit as a named constant at the top**, each with its reason. Values are held
  in `Secret`, whose text form is a placeholder and which refuses to be pickled. Nothing is evicted.
- **`forms.py`** - the encoded forms of a value the matcher looks for, each with a one-byte identifier
  a marker records, and `render(value, form_id)` to restore exactly the replaced text.
- **`markers.py`** - `MarkerCodec`: one AES block per marker under a per-process key, holding value
  identifier, form, host, a 5-byte counter and a check field. `read` cannot tell an altered marker from
  one issued by an earlier process; both fail the check field.
- **`rules.py`** - the shape rules as a table, `PUBLIC_FIELDS`, and the credential-named flag rule.
- **`matcher.py`** - the Aho-Corasick index (`pyahocorasick`) over every stored value's forms.
- **`redactor.py`** - `Redactor`: `remember` (store a harvested value), `redact` (walk a result and
  replace), `resolve` (a marker back to its text, or a `Refusal`).

### Concurrency

The MCP SDK runs each request as its own task and sync tools on worker threads, so calls genuinely
overlap (checked on mcp 2.3: four calls on four threads at once). The store and the marker counter each
take a lock; the matcher builds a new index and swaps it in whole, so a reader never sees a half-built
one. `tests/test_redaction_store.py`, `test_redaction_markers.py` and `test_redaction_redactor.py`
each run their part from eight threads.

### Matching

Python's `re` cannot do this job: with 3,500 literal alternatives (500 values) it took 3.2 seconds to
scan 120 KB, where an Aho-Corasick automaton takes milliseconds. Building an automaton is still not cheap
(about 19 seconds for the 700,000 forms of 100,000 values), so new forms go into a small automaton that
is rebuilt on each addition and merged into the large one when it reaches a quarter of its size, keeping
total rebuild work proportional to the store. A merge near the limits stalls the call that triggers it;
accepted, since only an abnormal store reaches that size. A known value found in another host's output is
stored for that host too, so its marker can be swapped back there.

### Running out of identifiers

Value identifiers (4 bytes) and the marker counter (5 bytes) are never reused. If either runs out, the
redactor starts a new generation - new key, empty store - so every outstanding marker is refused. A
`redact` call in which that happens is refused with `StateResetError` rather than finished against the
empty store, where a value the rules cannot see would come back unredacted. Neither is reachable before
the overall budget in practice.

## Where the settings surface

- `server._switch_report()` merges `settings.report()` into the `switches` that `tool_list` and the
  tool catalog return, so both show the same thing.
- `host_list` and the startup roster of other hosts report `redact` per host from `settings.redacts`,
  the global switch or the host's own option.  `Host.redact` is the per-host option only; never read
  it directly to decide whether a call is redacted.
- `server.configuration_warnings()` joins `settings.startup_warnings(readonly=READONLY)` to the
  `DOCKER_MCP_SERVER_DISABLE` warnings, so they reach stderr at startup, `tool_list` and the
  instructions pointer together.  READONLY is passed in rather than read, so the settings module never
  imports `server.py`.  The warnings are judged per redacted host, because per-host `ro` removes exec
  and the file-writing tools just as READONLY does.
- `settings.OUTPUT_REDACTED` says whether the build redacts output at all.  While it is False,
  configuring redaction warns that nothing is redacted, and a test holds the status note at the top of
  `REDACTION.md` to the same flag.

## The host option grammar

`redact` is one of the options in a host's trailing brackets, parsed by `_hosts._parse_markers`.
Brackets may hold one option or a comma-separated list, so splitting the host list on commas has to
skip commas inside brackets (`_split_top_level`), and the bare single-host form is detected by an `=`
outside brackets (`_has_top_level_equals`), so that `(tls=<dir>)` does not make a bare endpoint read
as a `label=endpoint` list.  The grammar itself is described in [hosts.md](hosts.md).

## Extension points

Each is a single table, so adding a case is a one-line change plus its test.

### Adding an encoded form

1. Add a renderer to `_FORMS` in `docker_mcp/redaction/forms.py` under the next unused identifier.
   Identifiers are recorded in markers, so never reuse or renumber one.
2. Extend `test_form_identifiers_are_fixed` and add the encoding to
   `test_the_common_encodings_are_all_matched` in `tests/test_redaction_forms.py`.
3. Add it to the list of forms in `REDACTION.md`.

### Adding a shape rule

1. Add a finder to `_RULES` in `docker_mcp/redaction/rules.py`.
2. Add a case it must find to `test_each_rule_finds_its_credential`, and anything it must not to
   `test_what_is_not_a_credential_is_left_alone`, in `tests/test_redaction_rules.py`.
3. Add it to the list of rules in `REDACTION.md` and in the module docstring.

### Adding a public field

1. Add it to `PUBLIC_FIELDS` in `docker_mcp/redaction/rules.py`, and to the list in `REDACTION.md`;
   `test_redaction_md_lists_exactly_the_public_fields` fails until the two agree. Keep the list short: a
   field on it is never redacted by a rule, even if a credential ends up in it.

### Changing a limit

1. Change the constant at the top of `docker_mcp/redaction/store.py`, and its reason if that changed.
2. Update the table in `REDACTION.md`.

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
4. Report it in `host_list`, and add it to the flag list in the `docker-mcp://hosts` resource's
   description (`get_hosts_resource` in `docker_mcp/tools/resources.py`), which repeats the field set.
5. Update the grammar in [hosts.md](hosts.md) and the README.
