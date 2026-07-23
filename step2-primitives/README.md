# Step 2 — Core primitives: Resources and Prompts

Extends Step 1's toy server with the two primitives most engineers skip
because tool-calling alone gets a demo working. Same server, same
stdio/JSON-RPC transport, same hand-rolled client style — new folder, not a
modification of `step1-fundamentals/`, so git history shows the
progression.

## Why this distinction matters

- **Tool** — an action with side effects or computation: "do something,
  give me a result." `tools/call`.
- **Resource** — addressable data the client can read: "here's a thing
  that exists, read it if you want." `resources/list`, `resources/read`.
  The server doesn't decide when it's used; the client decides, by URI —
  not by passing arbitrary parameters like a function call.
- **Prompt** — a reusable message template with typed arguments, owned by
  the server, retrieved via `prompts/list` and `prompts/get`. Lets a
  server ship "here's how you should ask about my data" alongside the
  data itself.

This matters now because Step 3 replaces this toy server with real
Gmail/Drive servers, and Drive's MCP server exposes files as **Resources**,
not Tools.

## Files

- `server.py` — same `get_ticket_summary` tool as Step 1, unchanged,
  plus:
  - **Resource**: `ticket://T-1001` / `T-1002` / `T-1003`.
    `resources/list` returns the three URIs with names/descriptions;
    `resources/read` returns the full ticket text for a given URI.
  - **Prompt**: `draft_reply(ticket_id)`. `prompts/get` reads the
    matching resource *at request time* and returns a prompt message with
    the ticket text embedded in it — the server composing a Resource into
    a Prompt, not a static string.
  - Both of the above call a single shared `_read_ticket_resource(ticket_id)`
    helper for the actual lookup — neither handler indexes `TICKETS`
    directly. The docstring on that function marks it as the single
    source of truth, precisely so a future edit doesn't quietly
    reintroduce a second copy of the lookup in one handler.
- `client.py` — hand-rolled JSON-RPC client (no `ClientSession`), same as
  Step 1, extended to exercise all three primitives in one lifecycle:
  `resources/list` → `resources/read` → `prompts/list` → `prompts/get` →
  `tools/list` → `tools/call`.
- `test_shared_lookup.py` — in-process regression test (no subprocess/wire
  involved) that patches `_read_ticket_resource` with a spy and asserts
  **both** `handle_read_resource` and `handle_get_prompt` call it for the
  same `ticket_id`. This is the test that actually distinguishes "shared
  code path" from "two copies of the same lookup happening to agree" —
  see "Notes / pitfalls" below for why the earlier version of this step
  needed it.
- `jsonrpc.log` — generated on each run: every raw JSON-RPC frame, both
  directions, covering all three primitives.

`server.py` also registers a minimal `@server.completion()` handler for
`draft_reply`'s `ticket_id` argument, returning the three valid ticket IDs
as suggested values. This isn't one of the three primitives the spec
asked for — see "Notes / pitfalls" for why it turned out to be necessary
anyway.

## Running it

From this directory, using the repo's venv:

```
../.venv/bin/python client.py
```

To run the shared-lookup regression test on its own:

```
../.venv/bin/python test_shared_lookup.py
```

## What happens on the wire, in order

1. **`initialize`** — server's capabilities now include `resources` and
   `prompts` objects alongside `tools`, since handlers are registered for
   all three.
2. **`notifications/initialized`**
3. **`resources/list`** — returns the three ticket URIs with
   `name`/`description`/`mimeType`.
4. **`resources/read`** (`ticket://T-1001`) — returns
   `TextResourceContents` with the full ticket text.
5. **`prompts/list`** — returns `draft_reply` with its `ticket_id`
   argument schema (`required: true`).
6. **`prompts/get`** (`draft_reply`, `ticket_id=T-1001`) — returns a
   `GetPromptResult` whose single message embeds the *actual* ticket text
   read in step 4, not a reference to it. The client asserts this text is
   a substring of the prompt message.
7. **`tools/list`** / **`tools/call`** — Step 1's tool, unmodified, run
   last so the log shows all three primitives in one run.

## MCP Inspector — what to check this time

New UI surfaces vs. Step 1: the **Resources tab** and **Prompts tab**.

```
npx @modelcontextprotocol/inspector ../.venv/bin/python server.py
```

- **Resources tab** — confirm the three ticket resources appear with
  correct URIs; read `ticket://T-1002` and compare to `client.py`'s output
  for the same URI.
- **Prompts tab** — open `draft_reply`, supply `ticket_id`, and confirm
  the returned prompt text matches what `client.py` got for the same
  ticket. This is the real test: it proves the resource → prompt
  composition happens deterministically on the server, not in client code.
  If interacting with the `ticket_id` field throws `-32601 Method not
  found`, that's the Inspector calling `completion/complete` to suggest
  values — see "Notes / pitfalls" below; this is now fixed.

## Review checklist

- [x] Can explain Tool vs. Resource vs. Prompt in one sentence each (see
      above) without hedging.
- [x] `resources/read` returns identical content via client and Inspector.
- [x] `prompts/get` output actually contains the ticket text, not just a
      reference to it — the client asserts this (`ticket_text in
      prompt_text`) and it passes.
- [x] All three primitives visible in the same `jsonrpc.log` run.
- [x] Step 1's tool still works unmodified — `get_ticket_summary` result
      is byte-for-byte the same shape as Step 1's.
- [x] `resources/read` and `prompts/get` provably share one lookup
      function, not two copies that happen to agree —
      `test_shared_lookup.py` passes.
- [x] Inspector's Prompts tab doesn't throw `-32601` when interacting with
      `draft_reply`'s `ticket_id` argument — verified two ways: a raw
      `completion/complete` request sent directly over the wire (same
      hand-rolled style as `client.py`), and a fresh, fully-restarted
      Inspector session in the browser confirming `ticket_id=T-1002` works
      end to end (not just the ticket first opened).

## Notes / pitfalls actually hit

- **The one pitfall the spec didn't list, but should have**: the first
  version of this step had `handle_read_resource` and `handle_get_prompt`
  each index `TICKETS[ticket_id]` directly. Output was identical either
  way, so the client's substring assertion (`ticket_text in prompt_text`)
  passed — but that assertion can't tell "the Prompt genuinely composed
  the Resource" apart from "two independent copies of the same lookup
  happen to agree today." It would not have caught one copy silently
  drifting from the other later. Fixed by extracting
  `_read_ticket_resource(ticket_id)` as the single source of truth for
  ticket content, called by both handlers, and adding
  `test_shared_lookup.py`, which spies on that function and asserts both
  handlers actually call it (not just that their outputs match). Verified
  the test catches the regression: temporarily reverting
  `handle_get_prompt` to index `TICKETS` directly made the test fail with
  an assertion naming exactly which handler stopped calling the shared
  helper; reverting the fix made it pass again.
- This is the same "shared source of truth" problem that Retriever and
  Judge will hit in later steps — both need to agree on "what did we
  actually retrieve" from Drive, and "the two outputs matched in this
  test run" won't prove that if they're two independent implementations
  of "fetch this document."
- **A second pitfall the spec didn't list**: the Inspector's Prompts tab
  throws `-32601 Method not found` when you interact with `draft_reply`'s
  `ticket_id` argument for any ticket other than the one first opened.
  Diagnosis mattered here because `-32601` sounds like "a handler is
  missing" — and the client run's own log genuinely never exercised
  anything but `T-1001`, so it was worth ruling out `list_prompts`/
  `get_prompt` registration first (confirmed both were registered by
  introspecting `server.request_handlers` directly, and confirmed
  `handle_get_prompt` succeeds for all three tickets when called
  in-process). The actual cause: Inspector sends a `completion/complete`
  request to fetch argument-value suggestions as you interact with a
  prompt's argument field, and `server.py` had no handler for that method
  at all — reproduced directly by sending a raw `completion/complete`
  frame over the wire (bypassing the browser entirely) and getting back
  the exact `-32601` error. Fixed by adding a `@server.completion()`
  handler that returns the three valid ticket IDs for `draft_reply`'s
  `ticket_id` argument. Re-ran the same raw-wire probe afterward and
  confirmed it now returns `{"values": ["T-1001", "T-1002", "T-1003"], ...}`
  instead of an error, and that `initialize`'s capabilities now include a
  `completions` object. One more wrinkle before this could be signed off:
  the first retest in the browser still showed `-32601` even after the
  server fix, because the running Inspector session's backend was
  connected to a server subprocess whose lifetime straddled the edit —
  the in-app "Refresh" button re-fetches data over the existing session,
  it doesn't reconnect from scratch. Only a full restart (kill the
  Inspector process, relaunch the `npx` command fresh) picked up the
  fix. Lesson for later steps: after any server-side change, restart
  Inspector's process itself, not just the page/tab — a partial refresh
  can look like the fix didn't work when it actually did.
- None of the pitfalls the spec did list occurred: `read_resource` is
  addressed purely by URI (no extra parameters snuck in like a tool
  call), and `get_prompt` composes the resource at request time rather
  than returning a canned string.
- Registering `list_resources`/`read_resource`/`list_prompts`/`get_prompt`
  handlers is sufficient for `Server.get_capabilities()` to add the
  `resources` and `prompts` capability objects to the `initialize`
  response automatically — no separate capability declaration needed
  beyond decorating the handlers.
