# Step 4 — Multi-server composition

One Python process holding two real MCP servers (Gmail, Drive) open at
the same time as independent sessions, plus Step 1's toy server reused
as a controllable third connection for the collision and disconnect
tests below. This is the shape Inbox Guardian actually needs: one client
that can reason across email and files in the same run, not three
separate Inspector sessions run one at a time.

## Files

- `client.py` — the composition client. See its module docstring for the
  full design rationale.
- `jsonrpc.log` — generated on each run: every JSON-RPC frame from all
  three sessions, each line tagged `[gmail]` / `[drive]` / `[toy]`, in
  the order they actually crossed the wire. One file, not three.

Two prior steps' servers were given a small, additive change for this
step (both keep every tool they already had):

- `step1-fundamentals/server.py` gained a `ping` tool.
- `step3-real-servers/drive-server/server.py` gained a `ping` tool.

Both `ping` tools are genuinely identical in name and take no arguments
— that's the real collision this step's namespacing is tested against,
not a hypothetical one.

## Why this is a real problem, not busywork

MCP has no built-in cross-server namespacing. A client that does
`tools[name]` over a merged list from N servers will let whichever
server was merged in last silently shadow any earlier one with the same
tool name — no error, no warning, just the wrong server quietly
answering. Namespacing every call as `"{server}::{tool_name}"` is
**client-side responsibility**, not something the spec solves. This
project doesn't collide today by accident (Gmail exposes
`search_emails`, Drive exposes `search_drive_files`) — the `ping` tools
were added deliberately so this client is exercised against a real
collision, not code that merely looks correct.

## What `client.py` does

1. **Three genuinely concurrent sessions.** `connect(server_tag, params)`
   spawns one subprocess, wraps its stdio in a logging pump tagged with
   `server_tag`, and yields an initialized `ClientSession`. `main()`
   opens all three (`gmail`, `drive`, `toy`) through an `AsyncExitStack`
   and keeps them alive together through every step below — not
   connect-use-disconnect-reconnect per operation.

2. **The registry — the actual deliverable.** `build_registry()` calls
   `tools/list` on all three sessions and builds one dict:
   `"{server}::{tool_name}" -> (session, real_tool_name)`. It also scans
   for bare tool names shared by more than one server and logs a warning
   naming them — proving the collision was noticed, not just silently
   absorbed into distinct keys by accident.

3. **Collision proof.** `demonstrate_collision()` calls `drive::ping` and
   `toy::ping` and asserts the two responses differ and each names its
   own server. This is the untested-branch trap called out in the spec:
   building the registry but never actually calling two colliding names
   would leave the namespacing logic unexercised. Actual result from a
   real run:

   ```
   drive::ping -> 'pong from inbox-guardian-drive (step3 server)'
   toy::ping   -> 'pong from inbox-guardian-toy (step1 server)'
   OK: each namespaced call reached the correct server, not the other one.
   ```

   Both calls went out as `tools/call` with the identical
   `{"name": "ping", "arguments": {}}` payload — the JSON-RPC wire frame
   itself doesn't distinguish them at all, confirmed in `jsonrpc.log`:
   the routing happens entirely in the client's registry (which session
   the call is sent to), not in anything the wire protocol carries. That
   is the concrete proof that namespacing is the client's job.

4. **Combined operation.** `demonstrate_combined_operation()` calls
   `gmail::search_emails` (query `"account"`) and `drive::search_drive_files`
   (query `"name contains 'Refund'"`), and prints both result sets
   together. Not the real Retriever/Judge logic (that's Step 5+) — just
   proof this client can hold and use both real sessions coherently in
   one script. The two queries are independently real, not staged to
   look thematically related — this account's test data doesn't happen
   to have an email and a doc about the same topic. Actual results from
   a real run:

   ```
   gmail::search_emails(query='account') ->
   ID: 19f8e4f8f7f86254
   Subject: Security alert
   From: Google <no-reply@accounts.google.com>
   Date: Thu, 23 Jul 2026 09:30:04 GMT

   ID: 19f8e4f34b59e538
   Subject: Test, finish setting up your new Google Account
   From: Google <no-reply@google.com>
   Date: Thu, 23 Jul 2026 02:29:38 -0700

   drive::search_drive_files(query="name contains 'Refund'") ->
   [
     {
       "file_id": "1mjV64wPNFoEhvZCBhh-H7xcG_r4PgEsWZzY_4HlLKbs",
       "name": "Refund Policy",
       "mimeType": "application/vnd.google-apps.document"
     }
   ]
   ```

5. **Disconnect is surfaced, not hidden.** `demonstrate_disconnect_is_not_hidden()`
   closes the toy server's subprocess mid-run (via its own, separate
   `AsyncExitStack`, independent from gmail/drive's), then calls
   `toy::ping` again and shows it raises loudly instead of returning
   stale data or being silently swallowed. It then confirms `drive::ping`
   still works — one server dying doesn't take the others down, but it
   also doesn't get quietly ignored. Actual result from a real run:

   ```
   === Disconnect handling: closing toy server mid-run ===
     toy::ping failed loudly after disconnect, as it must: ClosedResourceError:
     Drive and Gmail sessions are untouched by toy's disconnect -- confirming below:
     drive::ping -> 'pong from inbox-guardian-drive (step3 server)'
   ```

   The `except Exception` around that one `dispatch()` call in
   `demonstrate_disconnect_is_not_hidden()` is the only place in the
   whole client that catches a session error, and it exists purely to
   print the failure and prove it happened — everywhere else (`dispatch()`
   itself, `build_registry()`) lets exceptions propagate uncaught, so a
   real caller of this client would see a dead server as a raised
   exception, never as a quietly-degraded result.

## Running it

Both Gmail and Drive need their one-time OAuth setup already done (see
`step3-real-servers/gmail/README.md` and
`step3-real-servers/drive-server/README.md`). From the repo root:

```
.venv/bin/python step4-composition/client.py
```

Server-side debug logs (`[server]`) and client-side logs (`[client]`) go
to stderr; the demonstration output (registry, collision test, combined
search results, disconnect test) prints to stdout.

## MCP Inspector — limited role this step

Inspector only ever talks to one server at a time, so it can't verify
multi-server composition directly. Its only use here is a quick sanity
check, run once per server beforehand, that neither Gmail nor Drive's
behavior drifted since Step 3 — so a bug found later is known to be in
this step's own composition code, not upstream.

## Regression check: did adding `ping` break Steps 1 or 3?

Two prior steps' servers were modified in place for this step (`ping`
added to `step1-fundamentals/server.py` and
`step3-real-servers/drive-server/server.py`). Before trusting either
server in Step 4's own tests, each step's original verification was
re-run, unmodified, against the now-changed server files.

**Step 1** — ran `step1-fundamentals/client.py` (the exact file from
Step 1's sign-off, untouched) against the modified `server.py`:

```
=== tools/list result ===
{
  "tools": [
    {"name": "get_ticket_summary", ...},
    {"name": "ping", "description": "No-op liveness check. ...", "inputSchema": {"type": "object", "properties": {}}}
  ]
}

=== tools/call result (valid ticket_id=T-1001) ===
{"content": [{"type": "text", "text": "Customer can't reset password; reset link sent, awaiting confirmation."}], "isError": false}

=== tools/call result (invalid ticket_id=T-9999) ===
{"content": [{"type": "text", "text": "Unknown ticket_id: 'T-9999'"}], "isError": true}
```

Exit code 0. `get_ticket_summary` behaves identically to Step 1's
sign-off in both the valid and invalid-ticket cases (the client's own
`assert error_call_result.get("isError") is True` passed silently); the
only visible difference is `ping` now appearing alongside it in
`tools/list`, additively.

**Step 2** was not touched by this step (`step2-primitives/server.py` is
a separate file from Step 1's — the `ping` addition never reached it)
and was already re-confirmed unmodified/passing in the prior turn of
this session (`client.py` full lifecycle + `test_shared_lookup.py`'s
shared-lookup spy test, both exit 0).

**Step 3 (drive-server)** — this server never had its own scripted
client; Step 3's sign-off verified it manually through MCP Inspector
(see `step3-real-servers/drive-server/README.md`'s "Verified" section).
Since Inspector is an interactive, browser-driven tool, the regression
check instead scripted the identical set of checks Inspector was used
for — `resources/list`, reading a regular file, reading a Google-native
doc through the export branch, and `search_drive_files` — using the SDK
`ClientSession` (the same primitive Step 4's own `connect()` wraps),
plus a call to the new `ping` tool:

```
=== resources/list ===
 - Tickets gdrive:///1P-zuj0m3eKw6VX1RmrouJGw3JCcglYjJxhnps-4AGVA application/vnd.google-apps.spreadsheet
 - Refund Policy gdrive:///1mjV64wPNFoEhvZCBhh-H7xcG_r4PgEsWZzY_4HlLKbs application/vnd.google-apps.document
 - Project proposal gdrive:///1wqPy7LqrI8vdlw02GCJkbmmuAUQg9Ygh-GdTvDIN8-E application/vnd.google-apps.document
 - UCLA&MIT.pdf gdrive:///1fi_TlrWlCISvErrGVEFTN-ZhqeN_32UQ application/pdf

=== resources/read: regular file (UCLA&MIT.pdf) ===
  mimeType=application/pdf kind=blob size=2537816 bytes/chars

=== resources/read: Google-native doc (Refund Policy, export branch) ===
  mimeType=text/plain
  text (first 200 chars): '﻿Refund Policy\r\nRefunds are processed within 5 business days of approval. Approved\r\nrefunds are issued to the original payment method. Refund requests\r\nmust be submitted within 30 days of purchase. Or...'

=== tools/call: search_drive_files(query="name contains 'Refund'") ===
  [{"file_id": "1mjV64wPNFoEhvZCBhh-H7xcG_r4PgEsWZzY_4HlLKbs", "name": "Refund Policy", "mimeType": "application/vnd.google-apps.document"}]

=== tools/call: ping (regression target -- newly added) ===
  pong from inbox-guardian-drive (step3 server)
```

Exit code 0. Every behavior from Step 3's original "Verified" list —
correct resource URIs/mimeTypes, the regular-file `get_media` branch,
the Google-native `export` branch, and `search_drive_files` — reproduced
exactly, with `ping` again appearing only as an addition, not a
replacement of anything.

**Conclusion:** adding `ping` to both servers was additive in practice,
not just in intent — nothing from either step's original signed-off
behavior changed.

## Review checklist

- [x] Both real sessions (gmail, drive) — plus toy — genuinely concurrent:
      all three subprocesses are alive at once via one `AsyncExitStack`,
      not connect-use-disconnect-reconnect per call.
- [x] Synthetic collision test exists and actually proves correct
      routing: `drive::ping` and `toy::ping` return different,
      self-identifying text, asserted in code, not just "no crash."
- [x] Combined operation touches both real servers in one script run,
      with real results from each (see output above / run it yourself).
- [x] Single `jsonrpc.log`, frames tagged `[gmail]`/`[drive]`/`[toy]`,
      human-readable interleaving.
- [x] Can explain why MCP doesn't solve this: no cross-server
      namespacing exists in the protocol; a client doing bare-name
      lookups over a merged tool list would let one server silently
      shadow another. Namespacing is the client's job — see "Why this is
      a real problem" above.
- [x] Adding `ping` to Step 1's and Step 3's servers didn't regress
      either step's originally signed-off behavior — each step's own
      verification was re-run against the modified files; see
      "Regression check" above.

## Pitfalls addressed

- **Untested-branch trap** — avoided by giving two real servers a
  genuinely identical `ping` tool and actually calling both, instead of
  writing namespacing code that's never exercised against a real
  collision.
- **Silently hiding a dead server** — `dispatch()` does not catch and
  continue; `demonstrate_disconnect_is_not_hidden()` closes toy mid-run
  and shows the resulting error surfaces to the caller instead of being
  absorbed and the run continuing on drive/gmail alone without saying
  why toy went quiet.
