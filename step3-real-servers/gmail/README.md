# Gmail MCP server

Unlike the Drive server, this is a third-party server, not code in this
repo — it's run via `npx` and configured, not written here. This directory
exists to document what's used and how it was verified, not to vendor it.

## Package

[`@gongrzhe/server-gmail-autoauth-mcp`](https://www.npmjs.com/package/@gongrzhe/server-gmail-autoauth-mcp),
run directly via `npx` (no local install/checkout).

## Setup

1. Create an OAuth client ID in Google Cloud Console (APIs & Services ->
   Credentials -> OAuth client ID -> **Desktop app**), download the JSON.

2. Save it at exactly:

   ```
   ~/.gmail-mcp/gcp-oauth.keys.json
   ```

   This filename/path is hardcoded by the server — unlike the Drive
   server's `auth_setup.py`, there's no flexibility here.

3. Auth is a standalone command, run once before first use — not lazy,
   not triggered by the server on first tool call:

   ```
   npx @gongrzhe/server-gmail-autoauth-mcp auth
   ```

   This opens the Google consent screen in a browser. On success it writes
   `~/.gmail-mcp/credentials.json`.

## Scope — known deviation, flagged honestly

The consent screen presents **full send/compose/modify access**, not
read-only. This server doesn't offer scope narrowing out of the box, so
full access was granted to unblock local dev testing.

This is a deliberate but temporary deviation from this project's
read-only-by-default principle (see the Drive server's
`drive.readonly` scope for the pattern this should follow). **Follow-up
before any shared or deployed use:** switch to
[`ArtyMcLabin/Gmail-MCP-Server`](https://github.com/ArtyMcLabin/Gmail-MCP-Server),
a fork that supports a `--scopes` flag, and re-authenticate with the
minimum scope the pipeline actually needs (read + send, not full modify).

## Verified

Checked manually via MCP Inspector against a real Gmail account:

- Server connects and lists tools correctly.
- Called the search tool with query `in:inbox` — returned real messages
  from the authenticated account.
- Confirmed working end-to-end: auth -> tool call -> real Gmail data
  returned.

Not yet exercised: send/compose (the Dispatcher's actual use case) — only
search has been verified so far.
