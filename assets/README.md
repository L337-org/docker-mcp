# Bundle assets

`manifest.json` references **`icon.png`** in this directory as the Claude Desktop Extension icon.

Drop the icon here as `assets/icon.png`:

- **Format:** PNG, square, transparent background.
- **Resolution:** 512×512 is the recommended source size — it downscales cleanly everywhere the
  icon is shown. (The manifest references this one file; if you later want per-size/theme variants,
  switch `manifest.json`'s `"icon"` to the `"icons"` array form — see the
  [MCPB manifest docs](https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md).)

`mcpb pack` and the release workflow both fail if `assets/icon.png` is missing, so the bundle can't
be published without it.
