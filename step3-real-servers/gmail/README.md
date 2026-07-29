# Gmail MCP server

Unlike the Drive server, this is a third-party server, not code in this
repo — it's run via `npx` and configured, not written here. This directory
exists to document what's used and how it was verified, not to vendor it.

## Package (updated at Step 9)

[`@artymclabin/gmail-mcp`](https://www.npmjs.com/package/@artymclabin/gmail-mcp)
(a maintained fork of `@gongrzhe/server-gmail-autoauth-mcp`), run directly
via `npx` (no local install/checkout) — `npx -y @artymclabin/gmail-mcp`.

**Why the switch:** the original package (`@gongrzhe/server-gmail-autoauth-mcp`,
below) offered no scope-narrowing option, so Steps 3-8 ran with full
send/compose/modify access as a flagged, deliberate-but-temporary deviation.
This project's own memory of that decision explicitly tied resolving it to
the point a real Dispatcher exists to test send/draft against — Step 9. This
fork adds exactly the missing `--scopes` flag, confirmed by reading its
`dist/index.js`/`dist/scopes.js` source directly (not just its README)
before switching.

## Setup

1. Create an OAuth client ID in Google Cloud Console (APIs & Services ->
   Credentials -> OAuth client ID -> **Desktop app**), download the JSON.
   (Unchanged from the original setup — the same `gcp-oauth.keys.json` /
   Desktop-app client ID is reused across the package switch; Google scopes
   a consent grant by what the *user* approves at the consent screen, not by
   anything baked into the client ID itself.)

2. Save it at exactly:

   ```
   ~/.gmail-mcp/gcp-oauth.keys.json
   ```

   This filename/path is hardcoded by the server.

3. Auth is a standalone command, run once before first use — not lazy,
   not triggered by the server on first tool call:

   ```
   npx -y @artymclabin/gmail-mcp auth --scopes=gmail.readonly,gmail.compose
   ```

   This opens the Google consent screen in a browser. On success it writes
   `~/.gmail-mcp/credentials.json` with a `scopes` field recording exactly
   what was granted (`{"tokens": {...}, "scopes": ["gmail.readonly", "gmail.compose"]}`)
   — a real, inspectable record of the grant, not just a token.

## Scope — narrowed at Step 9, closing out Step 3's flagged deviation

Now `gmail.readonly` + `gmail.compose` only — no `gmail.modify` (arbitrary
label/read-state changes), no `gmail.labels`, no `gmail.settings.basic`, no
`gmail.full` (permanent delete). This fork enforces scope two independent
ways, both verified against the real re-authenticated account (not assumed
from reading source): `tools/list` itself is filtered by
`hasScope(authorizedScopes, tool.scopes)` (`dist/index.js` line 282) —
label/filter/delete-shaped tools never appear in the list at all — and the
same check runs again at `tools/call` time (line 300) as defense in depth. A
real run against this account's re-authenticated credentials confirmed both:
`tools/list` returned exactly the 14 read/compose-shaped tools
(`search_emails`, `read_email`, `get_thread`, `list_inbox_threads`,
`get_inbox_with_threads`, `download_email`, `download_attachment`,
`list_email_labels`, `draft_email`, `send_email`, `send_draft`,
`update_draft`, `delete_draft`, `reply_all`) — `create_label`,
`delete_label`, `modify_email`, `modify_thread`, `create_filter`,
`delete_email`, `batch_delete_emails` were genuinely absent, not just
undocumented; a direct `delete_email` call was rejected with `Error: Tool
"delete_email" is not available. You may need to re-authenticate with
additional scopes.`

**One honest caveat, not glossed over:** Google's real `gmail.compose` OAuth
scope bundles create-draft *and* send together — there is no narrower
official Gmail scope that permits drafting but not sending. `send_email` and
`send_draft` are therefore still present and callable under this grant. Step
9's Dispatcher enforces "draft-only" as an **application-level policy**
(never calling `gmail::send_email`/`gmail::send_draft`), not as something
the OAuth scope itself blocks — see `agents/README.md` for where that
decision actually lives.

The previous full-scope credentials were preserved, not destroyed, at
`~/.gmail-mcp/credentials.full-scope.json.bak`, before re-authenticating.

## Verified

Checked manually via MCP Inspector against a real Gmail account (original,
full-scope setup):

- Server connects and lists tools correctly.
- Called the search tool with query `in:inbox` — returned real messages
  from the authenticated account.
- Confirmed working end-to-end: auth -> tool call -> real Gmail data
  returned.

**Step 9 (scope narrowing + first send/draft-capable test)**: re-verified
against the new package and narrowed scope — see "Scope" above for the
`tools/list`-filtering and rejected-call evidence. `draft_email` itself
(the Dispatcher's actual use case) is exercised for real in
`agents/README.md`'s end-to-end run, not just checked for scope
availability here.
