# Step 6 — Sampling: the server calls back into the client's LLM

Carries Step 5's Streamable HTTP + OAuth 2.1 server forward unmodified
(`server.py`, `oauth/` copied over — transport, progress, cancellation,
and auth are untouched, since sampling isn't transport-dependent and
there's no reason to regress that work). The one thing that's new:
`search_drive_files` no longer returns Drive's raw match list. It asks
the **client** to rank the candidates by relevance, via
`sampling/createMessage`, and this server never holds an LLM credential
of its own.

## Files

- `server.py` — Step 5's server, with `search_drive_files` rewritten to
  rank candidates via sampling instead of returning Drive's raw order.
- `oauth/` — unchanged from Step 5.
- `client.py` — Step 5's client, plus a `sampling_callback` that
  fulfills server-initiated `sampling/createMessage` requests: prints
  the request, blocks on human approval, and on approval calls a real
  LLM to produce the ranking.

## The inversion

Every primitive before this step ran in one direction: the client asks,
the server answers. Tools, resources, prompts, even the entire OAuth 2.1
dance in Step 5 — all client-initiated. Sampling is the server asking
the client for something mid-request: "run this completion for me,
using whatever model you have configured."

There's no `sampling` field on `mcp.types.ServerCapabilities` to declare
this with — only `ClientCapabilities` has one, because sampling support
is fundamentally the client's to offer, not the server's. What a server
*can* do is check the connected client's declared capabilities before
ever attempting the call:

```python
if not ctx.session.check_client_capability(types.ClientCapabilities(sampling=types.SamplingCapability())):
    raise ValueError("search_drive_files requires a client that declared 'sampling' ...")
```

That check, in `_rank_candidates_by_relevance`, is this server's
explicit, enforced statement of its dependency on the client for model
access — expressed the only way the protocol actually allows, since
there's no server-side capability slot to set instead.

## Why a server would want this instead of calling an LLM API directly

It never needs its own API key, never pays for its own model usage, and
never picks a model out of band from whatever the operator running the
client has already configured and is willing to spend on. The real cost:
every ranking call now depends on a client that implements sampling
*and* a human willing to approve it. For a tool like this — reusing
model access someone else is already paying for and already trusts —
that trade is the entire point. It stops being "sampling" and starts
being "my server calling an LLM API" the moment the server holds its
own key or picks its own model; the defining feature is that the
*client's* credentials and the *client's* model are what actually runs.

## Building the ranking request

`search_drive_files`'s `query` argument changed meaning from Steps 3-5:
it's now a natural-language description of intent ("how do I get my
money back"), not Drive query operator syntax. The handler lists every
file in the account (this account has 4 — a reasonable demo-scale
candidate pool; a larger corpus would want a real pre-filter before
handing candidates to sampling, out of scope here since this step is
about the ranking primitive, not a full retrieval pipeline), then sends
their `file_id`/`name`/`mimeType` to the client as a `sampling/createMessage`
request asking for a ranked, filtered `{"ranked_file_ids": [...]}`.

## Proof: the direction actually flips, mid-tool-call

Logged in one file (`jsonrpc.log`, tagged the same way as Step 5), the
exact four-message sequence for one real `search_drive_files` call —
query `"a PDF file"`, from an actual run:

```
>>> POST /mcp {"method":"tools/call","params":{"name":"search_drive_files","arguments":{"query":"a PDF file"}},"jsonrpc":"2.0","id":3}
    -- client -> server, normal

<<< /mcp data: {"method":"sampling/createMessage","params":{"messages":[{"role":"user","content":{"type":"text",
    "text":"Rank the following candidate Drive files by relevance to this query: 'a PDF file'\n\nCandidates:\n
    - file_id='1P-...' name='Tickets'\n- file_id='1mjV...' name='Refund Policy'\n
    - file_id='1wqPy...' name='Project proposal'\n- file_id='1fi_...' name='UCLA&MIT.pdf'\n\n
    Respond with ONLY a JSON object..."}}],"systemPrompt":"You rank search results for relevance...",
    "maxTokens":500},"jsonrpc":"2.0","id":1}
    -- server -> client: THE INVERSION, mid-tools/call, own id-space (id:1, unrelated to the id:3 request it's nested inside)

>>> POST /mcp {"jsonrpc":"2.0","id":1,"result":{"role":"assistant","content":{"type":"text",
    "text":"{\"ranked_file_ids\": [\"1fi_TlrWlCISvErrGVEFTN-ZhqeN_32UQ\"]}"},
    "model":"gpt-oss-120b","stopReason":"endTurn"}}
    -- client -> server: the client's LLM completion, mapped into CreateMessageResult

<<< /mcp data: {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text",
    "text":"[\n  {\n    \"file_id\": \"1fi_TlrWlCISvErrGVEFTN-ZhqeN_32UQ\",\n    \"name\": \"UCLA&MIT.pdf\",\n
    \"mimeType\": \"application/pdf\"\n  }\n]"}],"isError":false}}
    -- server -> client: final tool result -- 4 candidates in, exactly 1 out
```

Four messages, direction flips twice, all inside what looks from the
outside like one `tools/call`. The correct file was isolated despite a
literal case mismatch, not a full absence of information: the query
says "PDF" (uppercase) and the only clue in the candidate list is the
filename `UCLA&MIT.pdf` (lowercase extension) — no `mimeType` field is
included in what the server sends for ranking, only `file_id`/`name`.
A case-sensitive literal substring match on `"PDF"` finds nothing in
`"UCLA&MIT.pdf"`; the model correctly recognized the file type from the
name regardless of case, which is exactly the judgment call Drive's own
`files().list(q=...)` can't make.

The `initialize` request also shows the capability declaration that
makes this possible:

```
>>> POST /mcp {"method":"initialize","params":{"capabilities":{"sampling":{}},...}}
```

That `"sampling":{}` is there because `client.py` passed a real
`sampling_callback` to `ClientSession` — the SDK derives the declared
capability from whether a non-default callback is set (see
`ClientSession.initialize()`), not from anything this project set by
hand.

**What the repeated `ping` lines around this exchange are** — the same
run's log also shows this, straddling the sampling wait exactly:

```
[server] DEBUG <<< /mcp data: {"method":"sampling/createMessage",...,"id":1}      # 11:16:07.072
[server] DEBUG ping: b': ping - 2026-07-24 10:16:21.657366+00:00\r\n\r\n'          # 11:16:21.657 (~15s later)
[server] DEBUG ping: b': ping - 2026-07-24 10:16:21.660559+00:00\r\n\r\n'
[server] DEBUG >>> POST /mcp {"jsonrpc":"2.0","id":1,"result":{"role":"assistant",...}}  # 11:16:26.386 (human took ~19s to approve)
```

These are Streamable HTTP's own SSE keep-alive heartbeat (`sse-starlette`'s
default `ping_interval=15` seconds), sent on every open SSE stream to
stop a quiet connection from being dropped as idle by proxies/load
balancers -- one appears for each currently-open stream on the
connection, which is why two show up here a few milliseconds apart.
They have nothing to do with sampling, retries, or a stuck request --
they'd fire identically during any sufficiently long gap on the same
connection (e.g. the chunk-to-chunk waits in the progress demo below
also show them). Flagging this so a future reader doesn't mistake a
normal heartbeat during a slow human approval for a bug or a retry loop.

## Proof: ranking demonstrably changes the output

Query: `"how do I get my money back for something I bought"`. None of
this account's file *names* contain "money" or "refund" — there's no
keyword for a naive matcher to even key off of. The account's real
Refund Policy doc is obviously the answer to a human, and to an LLM.
Server log, logged deliberately before and after the sampling call so
the "before" and "after" are a clean diff:

```
[server] INFO search_drive_files: raw Drive order (unranked): ['Tickets', 'Refund Policy', 'Project proposal', 'UCLA&MIT.pdf']
...
[server] INFO search_drive_files: final ranked order: ['Refund Policy']
```

Same pattern on the `"a PDF file"` query used for the direction-flip
example above:

```
[server] INFO search_drive_files: raw Drive order (unranked): ['Tickets', 'Refund Policy', 'Project proposal', 'UCLA&MIT.pdf']
...
[server] INFO search_drive_files: final ranked order: ['UCLA&MIT.pdf']
```

Four candidates in Drive's own (arbitrary) list order collapsed to one,
correctly identified, after a real round-trip to Cerebras's
`gpt-oss-120b`, in both cases. This is not comparing to a hypothetical —
it's the same account's raw retrieval order logged right before the
ranking call that changed it.

## The approval checkpoint — tested both ways, not a formality

`sampling_handler` in `client.py` prints the request's full content
(system prompt, every message, `max_tokens`) and blocks on
`input("Approve this sampling request? [y/N] ")` before calling any
model. This isn't a no-op gate — it was tested denying, not just always
approving, and that test caught a real bug.

**Approve path** (real run):

```
Approve this sampling request? [y/N] y
  Approved -- calling Cerebras (gpt-oss-120b)...
  Cerebras response: {"ranked_file_ids": ["1mjV64wPNFoEhvZCBhh-H7xcG_r4PgEsWZzY_4HlLKbs"]}
```

**Deny path** (real run) — the server's low-level `Server.call_tool`
wrapper does exactly what it's done since Step 1 for any exception
raised inside a tool handler: converts it to `CallToolResult(isError=True)`
rather than crashing or dropping the connection:

```
Approve this sampling request? [y/N] n
  Denied -- server gets an error response, not a completion.
```

Server log, same request (this is from a separate run than the
direction-flip/heartbeat evidence above — denying the first sampling
call means `pdf_matches` never gets populated, so this run doesn't
reach the progress/cancellation demo; there's no single run that
exercises both a denial and the full downstream demo, which is exactly
what you'd expect since a denied search legitimately has nothing to
hand the rest of the script):

```
[server] DEBUG <<< /mcp data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"User denied the sampling request"}],"isError":true}}
```

**The first version of this demo crashed on denial** — `demonstrate_sampling_ranking`
called `json.loads(result.content[0].text)` unconditionally, and on a
denied request `result.content[0].text` is `"User denied the sampling
request"`, not JSON. Running the deny path (not just imagining it) is
what surfaced this — the exact "deny at least once during testing"
pitfall this step calls out. Fixed by checking `result.isError` before
parsing (`_search_drive_files` in `client.py`), so a denial now prints a
clear message and the script continues, instead of an unhandled
`JSONDecodeError` inside an `ExceptionGroup`.

## Step 5's cancellation still holds under Step 6 (fell out of the same run, not a dedicated test)

The same run used for the direction-flip and heartbeat evidence above
carries through to the end of `client.py`'s demo, which still runs
Step 5's progress/cancellation sequence unmodified against the file the
sampling-ranked search found. That run's log independently re-confirms
Step 5's cancellation behavior wasn't disturbed by anything in this
step, without a test written specifically to check it:

```
[server] INFO _fetch_drive_file_content: requesting chunk 1 of 'UCLA&MIT.pdf'
[server] INFO _fetch_drive_file_content: requesting chunk 2 of 'UCLA&MIT.pdf'
[server] DEBUG >>> POST /mcp {"method":"notifications/cancelled","params":{"requestId":5,"reason":"demonstrating cancellation (Step 5 build task 3)"},"jsonrpc":"2.0"}
[server] DEBUG <<< /mcp data: {"jsonrpc":"2.0","id":5,"error":{"code":0,"message":"Request cancelled"}}
```

No `requesting chunk 3` line ever appears — the download genuinely
stopped rather than completing anyway, exactly as Step 5 established.
Worth noting explicitly because it's easy to assume adding a new
primitive (sampling) risked the ones already working; this run is
evidence it didn't, incidentally rather than by a test aimed at it.

## Mapping the LLM response into MCP, not passing it through raw

This project's LLM access is Cerebras's OpenAI-compatible endpoint
(`openai` SDK, `base_url="https://api.cerebras.ai/v1"`,
`CEREBRAS_API_KEY` from `.env` at the repo root), not Anthropic —
`gpt-oss-120b`, chosen by querying the account's actual `/v1/models`
list rather than guessing a model name. The completion response is
explicitly reshaped into MCP's `CreateMessageResult`:

```python
return types.CreateMessageResult(
    role="assistant",
    content=types.TextContent(type="text", text=response_text),
    model=completion.model,
    stopReason=_FINISH_REASON_TO_STOP_REASON.get(choice.finish_reason, choice.finish_reason),
)
```

`_FINISH_REASON_TO_STOP_REASON` maps OpenAI's `finish_reason` strings
(`"stop"`, `"length"`) to MCP's `StopReason` vocabulary (`"endTurn"`,
`"maxTokens"`) rather than leaking an OpenAI-specific term into an
MCP-typed field the server (and any other MCP client) expects to be
spec-shaped.

## Running it

Both Drive credentials (`~/.drive-mcp/token.json`, from Step 3) and
`CEREBRAS_API_KEY` (in `.env` at the repo root) must already exist.

```
../.venv/bin/python server.py                 # OAuth required (default)
../.venv/bin/python server.py --no-auth        # transport-only, isolating auth from sampling bugs
../.venv/bin/python client.py                  # full demo: OAuth + 2 sampling calls + progress + cancellation
```

`client.py` prompts for approval twice (the money-back ranking query,
then a second search to find a PDF for the progress/cancellation demo).
Try denying at least once — the client handles it cleanly now.

## MCP Inspector

Inspector can act as the client side of a sampling request during manual
testing, showing exactly what content the server sent for ranking
before it ever reaches a real LLM call — useful for confirming the
prompt looks sane before spending a real API call debugging it
end-to-end. Not exercised as part of this step's automated verification
(already independently verified against Inspector in Step 5, for the
same transport/OAuth layer this step reuses unmodified); Step 5's
README has that record.

## Review checklist

- [x] Server never holds its own LLM API key — `server.py` imports no
      LLM client at all; every ranking judgment is delegated via
      `ctx.session.create_message(...)`.
- [x] Log shows the real direction-flip: `sampling/createMessage`
      initiated server -> client mid-`tools/call`, not just
      client-initiated requests the whole way through. See "Proof: the
      direction actually flips" above.
- [x] Human-approval checkpoint exists and actually blocks until
      approved — tested both ways; the deny path is not a no-op branch
      (it caught a real client-side bug, since fixed).
- [x] Ranking result demonstrably changes tool output vs. Drive's raw
      match order — proven with a query with no keyword overlap against
      any candidate name, where naive order and relevance order
      genuinely differ. See "Proof: ranking demonstrably changes the
      output" above.
- [x] Can explain why a server would want this instead of just calling
      an LLM API directly: no credential of its own, no cost of its own,
      rides on whatever model access the client's operator already has
      and trusts. See "Why a server would want this" above.
- [x] Reviewed the log for anything that could be mistaken for a bug:
      the repeated `ping` lines during a slow sampling approval are
      Streamable HTTP's own SSE keep-alive heartbeat (`sse-starlette`,
      15s interval), unrelated to sampling, not a retry loop — flagged
      explicitly above so a future reader doesn't chase a phantom.
- [x] Confirmed Step 5's cancellation still holds under this step's
      changes — not a dedicated regression test, but the same run used
      for the direction-flip evidence carries through to the
      progress/cancellation demo and reproduces Step 5's exact signature
      (no `requesting chunk 3` line after `notifications/cancelled`).

## Pitfalls addressed

- **Approval checkpoint as a formality that always auto-approves** —
  avoided by actually running the deny path, which surfaced a real
  crash (`json.loads` on a non-JSON error string) that an always-approve
  test run would never have caught.
- **Conflating sampling with the server just calling a tool that
  happens to be an LLM** — `server.py` never imports an LLM client,
  never holds a credential, and never picks a model; `gpt-oss-120b` is
  chosen and paid for entirely on the client side, and the server only
  ever sees whatever `CreateMessageResult` the client hands back.
