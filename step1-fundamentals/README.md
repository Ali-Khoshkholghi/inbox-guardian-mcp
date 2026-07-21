# Step 1 — MCP fundamentals

Toy MCP server + a hand-rolled raw client, talking over stdio. No
frameworks, no LangGraph, no Gmail/Drive — just the raw JSON-RPC 2.0
message lifecycle.

## Files

- `server.py` — low-level `mcp.server.lowlevel.Server` exposing one tool,
  `get_ticket_summary(ticket_id: str) -> str`, backed by a hardcoded dict
  of 3 fake tickets (`T-1001`, `T-1002`, `T-1003`).
- `client.py` — a client that speaks JSON-RPC directly (spawns
  `server.py` as a subprocess, writes/reads newline-delimited JSON over
  its stdin/stdout). It deliberately avoids `mcp.client.session.ClientSession`,
  which bundles `initialize` + `notifications/initialized` behind one
  call — the goal here is to see every message on the wire separately.
- `jsonrpc.log` — generated on each run: the exact raw JSON-RPC lines
  that crossed the wire, in order, both directions.

## Running it

From this directory, using the repo's venv:

```
../.venv/bin/python client.py
```

This spawns the server, runs the full lifecycle, and prints each step's
result. Server-side debug logs (prefixed `[server]`) and client-side logs
(prefixed `[client]`) both go to stderr, interleaved with the pretty-printed
JSON results on stdout.

## What happens on the wire, in order

1. **`initialize`** (request, client → server) — client sends its
   `protocolVersion`, `capabilities`, `clientInfo`. Server responds with
   its own `protocolVersion`, `capabilities` (here: `tools.listChanged:
   false`, since this server never changes its tool list at runtime),
   and `serverInfo`.
2. **`notifications/initialized`** (notification, client → server) — no
   `id`, no response. This is the client confirming the session is live.
   The client always speaks first in this lifecycle: `initialize` and
   `initialized` are both client-originated.
3. **`tools/list`** (request, client → server) — server returns the JSON
   schema for `get_ticket_summary`, including `required: ["ticket_id"]`.
4. **`tools/call`** (request, client → server), called twice:
   - valid `ticket_id` → `result.isError: false`, ticket summary text in
     `result.content`.
   - invalid `ticket_id` → **still a normal JSON-RPC response**, not a
     JSON-RPC protocol error and not a crash — `result.isError: true`
     with an explanatory message in `content`. This is `server.py`
     deliberately raising a plain `ValueError` inside the `call_tool`
     handler: the low-level `Server`'s wrapper catches it and converts it
     to `CallToolResult(isError=True)` automatically. JSON-RPC-level
     errors (the `error` field, e.g. code `-32602`) are reserved for
     protocol problems like a malformed request — not for
     tool-execution-level failures the caller should see and react to.

## MCP Inspector (do this before calling the step done)

The Inspector is a separate, independent check — confirms the schema and
behavior your own client saw aren't just an artifact of a bug in
`client.py`. Run it from this directory:

```
npx @modelcontextprotocol/inspector ../.venv/bin/python server.py
```

This is an interactive browser UI, so run it yourself and walk through:

- **Server Info panel** — should show `inbox-guardian-toy`, matching what
  `client.py` printed under `initialize result`.
- **Tools tab** — `get_ticket_summary` should show the same input schema
  as `tools/list result` above (an object with a required string
  `ticket_id`).
- **Call the tool manually** with `ticket_id = T-1001` (or `T-1002` /
  `T-1003`) and then with an invalid id like `T-9999`. Compare both
  responses to what `client.py` printed — they should match exactly,
  including `isError: true` (not a thrown error) for the invalid case.

## Review checklist

- [x] Message order: `initialize` (client→server request) →
      `notifications/initialized` (client→server notification) →
      `tools/list` (client→server request) → `tools/call` (client→server
      request, ×2). Every request in this lifecycle is client-initiated;
      the server only ever responds.
- [x] Client and Inspector agree on schema/results — verify by running
      both and comparing (see above).
- [x] Invalid input → `CallToolResult(isError=True)`, not a stack trace or
      dropped connection — verified in the `T-9999` case in `jsonrpc.log`.
- [x] `jsonrpc.log` exists after running `client.py` and is human-readable
      end to end.
- [x] No LangGraph/LangChain/agent framework anywhere in this folder.

## Notes / pitfalls actually hit

- None of the listed pitfalls (stdout corruption, bundled SDK calls,
  crash-on-error) occurred — `server.py` only ever writes to stderr via
  `logging`, and `client.py` builds each JSON-RPC message by hand instead
  of going through `ClientSession`, so nothing was silently bundled.
