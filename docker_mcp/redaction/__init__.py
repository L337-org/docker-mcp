"""Opt-in credential redaction: keeps credentials out of what the server returns.

User documentation is `REDACTION.md`; the package layout and its extension points are in
`architecture/redaction.md`.  Lives at the package root, not under `tools/`, so `server.py` can
import it without importing `docker_mcp.tools`, as with `_hosts.py` and `_env.py`.
"""
