# Drive MCP server

## Auth setup (run once, manually)

1. Create an OAuth client ID in Google Cloud Console (APIs & Services ->
   Credentials -> OAuth client ID -> Desktop app), download the JSON, and
   save it at:

   ```
   ~/.drive-mcp/gcp-oauth.keys.json
   ```

2. Run the setup script directly — it is a standalone script, not something
   `server.py` invokes or triggers on first use:

   ```
   ../../.venv/bin/python auth_setup.py
   ```

   This opens a browser for the Google consent screen. On success it writes
   the resulting credentials to `~/.drive-mcp/token.json`.

3. Re-run `auth_setup.py` any time you need to re-auth (e.g. the token file
   is deleted or revoked). `server.py` only ever reads `token.json` — it
   never runs the interactive flow itself.

## Scope

```
https://www.googleapis.com/auth/drive.readonly
```

Read-only, on purpose. This server only needs to retrieve knowledge-base
documents, never create/modify/delete Drive content. If a future step needs
write access, that's a deliberate scope change to make here and in the
Cloud Console client — not something to expand casually, and not something
that should happen implicitly.

## Verified

Checked manually via MCP Inspector against a real Drive account, with both
a plain file and a Google Doc present in it:

- **Resources tab** — `resources/list` shows both files with correct
  `gdrive:///{file_id}` URIs and correct `mimeType` (the plain file's real
  type; the Doc as `application/vnd.google-apps.document`).
- **Regular file read** — reading the plain file returns its correct
  content via `files().get_media()`.
- **Google-native file read** — reading the Doc returns the correct
  exported text via `files().export()`. This was the one branch that
  couldn't be exercised before real files existed in the account; now
  confirmed.
- **Tool** — `search_drive_files` correctly finds both files by keyword.

Auth-failure path (missing `token.json`) and clean stdio shutdown were also
verified directly (see commit history) before this pass.

## Regression check (Step 4)

Step 4 added a `ping` tool to this server (needed as a real, genuinely
name-colliding tool alongside step1-fundamentals' own `ping`, to prove
its multi-server client's namespacing against an actual collision, not
a hypothetical one). This server has no scripted client of its own —
the checks above were originally done through Inspector — so the
regression check scripted the same set of operations
(`resources/list`, reading the regular file, reading the Google-native
doc through the export branch, `search_drive_files`) via the SDK
`ClientSession` and re-ran them against the modified server, plus a
call to the new `ping` tool. All four original checks reproduced
exactly (same URIs/mimeTypes, same export text, same search match);
`ping` appeared only as an addition. Exit code 0 — see
`step4-composition/README.md`'s "Regression check" section for full
output.
