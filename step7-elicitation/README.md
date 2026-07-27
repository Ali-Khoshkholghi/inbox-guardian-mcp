# Step 7 — Elicitation: the server pauses and asks a human, mid-run

Carries Step 6's sampling-ranked server forward unmodified except for
one addition: the same `sampling/createMessage` call that ranks
`search_drive_files` candidates is now also asked to flag *genuine
ambiguity* — two or more top candidates that are each plausibly what
the user meant, but whose actual content materially conflicts. When
that happens, the server doesn't pick one and doesn't average them. It
sends `elicitation/create` and waits for a human.

## Files

- `server.py` — Step 6's server, with `_rank_candidates_by_relevance`
  extended to detect ambiguity, and a new `_disambiguate_with_human`
  that sends the elicitation request and interprets the three possible
  outcomes.
- `oauth/` — unchanged from Steps 5–6.
- `client.py` — Step 6's client, plus `elicitation_handler`: a second,
  separate server-initiated-request handler alongside `sampling_handler`.

## Sampling and elicitation are not the same primitive

Easy to conflate — both are "the server asks the client for something
mid-`tools/call`" — but they differ in who answers and what shape the
answer takes:

| | `sampling/createMessage` (Step 6) | `elicitation/create` (this step) |
|---|---|---|
| Who answers | The client's **model** | The client's **human** |
| Answer shape | Unstructured text the server parses | A response validated against a server-declared JSON Schema |
| Approval gate | y/N before spending money on a model call | accept / decline / cancel, distinct outcomes |
| Server-side function | `_rank_candidates_by_relevance` | `_disambiguate_with_human` |
| Client-side handler | `sampling_handler` | `elicitation_handler` |

Neither has a slot on `mcp.types.ServerCapabilities` — only
`ClientCapabilities` declares support for either one, so the server
checks both independently via `session.check_client_capability(...)`
before ever attempting the corresponding call, exactly Step 6's pattern
applied twice.

## The ambiguous fixture, not an invented one

The build task explicitly warns against a toy scenario, so this account
(the Step 3 test Drive account, `mcptest2k26@gmail.com`) has two
real, deliberately conflicting refund-policy docs:

- **"Refund Policy"** (pre-existing, from Step 3): refunds to the
  *original payment method*, 30-day window.
- **"Return & Refund Policy"** (added for this step): *store credit
  only*, 14-day window — same subject, materially different remedy and
  deadline.

A query like `"how do I get my money back for something I bought"` is
answered wrong if the tool picks the wrong one — this is exactly the
"two files that both plausibly match, with materially different
content" case the build task describes, not a contrived one.

## Giving sampling something to judge conflict on

Step 6's ranking prompt only ever sent `file_id`/`name` — enough to
rank, not enough to detect that two docs disagree. This step adds
`_fetch_relevance_snippet`: a ~400-character content excerpt fetched via
`export()` for Google-native docs/sheets/slides (skipped for the
account's PDF, since chunk-downloading a whole binary file for a
snippet nobody asked to see would be real, unjustified work). The
ranking prompt now includes `content_excerpt=...` per candidate and
asks the model to populate `ambiguous_group` — a list of 2+ file_ids —
**only** when their excerpts actually disagree, not merely because
several files are relevant:

```python
prompt = (
    f"Rank the following candidate Drive files by relevance to this query: {query!r}\n\n"
    f"Candidates:\n{candidate_lines}\n\n"
    'Respond with ONLY a JSON object of the shape {"ranked_file_ids": [...], "ambiguous_group": [...]}, '
    "... Populate ambiguous_group ONLY if two or more of the top-ranked files are each plausibly "
    "what the user meant but their content_excerpt materially conflicts ..."
)
```

This is still sampling doing sampling's job (a model's judgment call).
*Acting* on that judgment by asking a human is the separate primitive
below.

## Building the elicitation request

`_disambiguate_with_human` sends a **typed schema**, not a free-text
question in disguise — the pitfall this step calls out most directly.
The human's answer is constrained to an `enum` of exactly the ambiguous
`file_id`s:

```python
requested_schema = {
    "type": "object",
    "properties": {
        "chosen_file_id": {
            "type": "string",
            "title": "Which file did you mean?",
            "description": "The file_id of the candidate that actually answers your search.",
            "enum": candidate_ids,
        }
    },
    "required": ["chosen_file_id"],
}
result = await ctx.session.elicit_form(message=message, requestedSchema=requested_schema)
```

`message` carries the human-readable context (which two files, why
they conflict); the schema is what actually constrains the answer. A
client is free to render `message` however it likes, but it cannot
return an out-of-enum value without the server catching it — see the
defense-in-depth check in `_disambiguate_with_human` that verifies
`chosen_file_id` is actually one of the offered candidates before
trusting it, rather than assuming every client honors the schema.

## Client side: rendering the schema, not just the message

`elicitation_handler` in `client.py` reads `requestedSchema`, extracts
the `enum`, and renders it as a numbered menu — printing only
`params.message` and reading free-text stdin would have been
"elicitation in name only." The human's choice is validated against
that same enum before the handler ever returns it:

```python
if raw.isdigit() and 1 <= int(raw) <= len(enum_values):
    chosen = enum_values[int(raw) - 1]
    return types.ElicitResult(action="accept", content={field_name: chosen})
```

Three distinct return paths, not two collapsed into a bool: MCP's
`ElicitResult.action` is `"accept" | "decline" | "cancel"`, and the
handler lets the human pick any of the three explicitly — a number, the
literal word `decline`, or empty input / Ctrl-D for `cancel`.

## Proof: the direction flips twice, and differently, inside one `tools/call`

From a real run's `jsonrpc.log` — one `search_drive_files` call, query
`"how do I get my money back for something I bought"`:

```
<<< /mcp data: {"method":"sampling/createMessage","params":{"messages":[{"role":"user","content":{"type":"text",
    "text":"Rank the following candidate Drive files ... content_excerpt=... Populate ambiguous_group ONLY if ..."}}],
    "systemPrompt":"You rank search results for relevance and flag genuine content conflicts. ...",
    "maxTokens":1500},"jsonrpc":"2.0","id":1}
    -- server -> client: sampling. Answer comes from a MODEL, unstructured text.

>>> POST /mcp {"jsonrpc":"2.0","id":1,"result":{"role":"assistant","content":{"type":"text",
    "text":"{\"ranked_file_ids\":[\"1mjV...\",\"1i83...\"],\"ambiguous_group\":[\"1mjV...\",\"1i83...\"]}"},
    "model":"gpt-oss-120b","stopReason":"endTurn"}}
    -- the model's judgment: both refund docs rank relevant AND are flagged as conflicting.

<<< /mcp data: {"method":"elicitation/create","params":{"mode":"form",
    "message":"Your search for 'how do I get my money back for something I bought' matched multiple Drive
    files that are each plausibly relevant, but their actual content materially conflicts:\n\n
    1. 'Refund Policy' (file_id='1mjV...')\n  2. 'Return & Refund Policy' (file_id='1i83...')\n\n
    Which one did you actually mean? You may decline if neither is right.",
    "requestedSchema":{"type":"object","properties":{"chosen_file_id":{"type":"string",
    "title":"Which file did you mean?","enum":["1mjV...","1i83..."]}},"required":["chosen_file_id"]}},
    "jsonrpc":"2.0","id":2}
    -- server -> client: elicitation. Different request entirely, own id-space (id:2, not id:1).

>>> POST /mcp {"jsonrpc":"2.0","id":2,"result":{"action":"accept","content":{"chosen_file_id":"1mjV..."}}}
    -- the HUMAN's answer: {"action":..., "content":{...}} -- a schema-shaped decision,
       not a {"role","content","model","stopReason"} completion. Nothing here came from a model.
```

Two log excerpts, same tool call, unmistakably different: the sampling
response has `role`/`model`/`stopReason` and free-text JSON the server
had to parse; the elicitation response has `action`/`content` and
nothing resembling a model completion. If these had collapsed into one
shape, that would be the "reused sampling for elicitation" pitfall this
step warns about.

Server log, same run, showing the full decision trail:

```
[server] INFO search_drive_files: final ranked order: ['Refund Policy', 'Return & Refund Policy']
[server] INFO search_drive_files: sampling flagged genuine ambiguity among ['Refund Policy', 'Return & Refund Policy'] -- pausing for elicitation/create instead of guessing
[server] INFO search_drive_files: sending elicitation/create (server -> client), 2 ambiguous candidates: [...]
[server] INFO search_drive_files: received elicitation response (client -> server), action=accept content={'chosen_file_id': '1mjV...'}
[server] INFO search_drive_files: human chose file_id=1mjV...
```

Final tool result for this run — the human's choice, not a guess:

```json
[
  {
    "file_id": "1mjV64wPNFoEhvZCBhh-H7xcG_r4PgEsWZzY_4HlLKbs",
    "name": "Refund Policy",
    "mimeType": "application/vnd.google-apps.document"
  }
]
```

## Decline and cancel — tested as two more real, distinct branches

Not a formality, and not the same branch as each other. Same query,
two more real runs:

**Decline** (explicit "neither is right") — server log:

```
[server] INFO search_drive_files: received elicitation response (client -> server), action=decline content=None
[server] INFO search_drive_files: human did not resolve the ambiguity (declined/cancelled) -- abstaining rather than guessing which candidate was meant
```

Tool result — an ambiguity marker, not a match list, and not
`candidates[0]`:

```json
{
  "ambiguous": true,
  "resolved": false,
  "candidates": [
    {"file_id": "1mjV...", "name": "Refund Policy", "mimeType": "application/vnd.google-apps.document"},
    {"file_id": "1i83...", "name": "Return & Refund Policy", "mimeType": "application/vnd.google-apps.document"}
  ],
  "message": "Multiple files plausibly matched this query with conflicting content, and the human declined or cancelled disambiguation. Abstaining rather than guessing -- read each candidate (resources/read on its gdrive:///{file_id} URI) before acting."
}
```

**Cancel** (dismissed without an explicit choice — empty input in
`elicitation_handler`) — server log, logged as a *different* action
than decline, not folded into the same branch:

```
[server] INFO search_drive_files: received elicitation response (client -> server), action=cancel content=None
```

Same abstain-and-log outcome as decline from the tool's perspective
(both mean "ambiguity unresolved"), but the distinction is preserved
all the way through — `ElicitResult.action` is `"decline"` in one run
and `"cancel"` in the other, visible in both the wire log and the
server's own log line, never collapsed into one "not accept" boolean.

## A real bug this testing caught: token budget, not an elicitation bug

Running the decline path first (before increasing `max_tokens`) surfaced
something worth recording: the *first* attempt at the ambiguous-query
sampling call came back with an **empty** completion —
`gpt-oss-120b` spends hidden reasoning tokens against the same
`max_tokens` budget before emitting visible output, and this step's
longer prompt (content excerpts + ambiguity instructions) pushed it over
Step 6's `max_tokens=500`. The parse-failure fallback in
`_rank_candidates_by_relevance` absorbed this silently as "no ranking,
no ambiguity flagged," which is the *correct* degradation behavior but
made the real cause (token budget) easy to mistake for "the ambiguity
detection doesn't work." Server log from that first attempt:

```
[server] WARNING search_drive_files: could not parse ranking response (Expecting value: line 1 column 1 (char 0)) -- falling back to unranked order, no ambiguity flagged: ''
```

Fixed by raising `max_tokens` to 1500 for this step's ranking call (see
the comment at that call site in `server.py`) — not an elicitation bug,
but a real thing this step's own testing surfaced, in the spirit of
Step 6's `json.loads` denial-path bug: run the actual paths, don't
assume they work.

## Running it

Both Drive credentials (`~/.drive-mcp/token.json`, from Step 3) and
`CEREBRAS_API_KEY` (in `.env` at the repo root) must already exist, and
the test Drive account needs the second "Return & Refund Policy" doc
(see "The ambiguous fixture" above) alongside Step 3's original four
files.

```
../.venv/bin/python server.py                 # OAuth required (default)
../.venv/bin/python server.py --no-auth        # transport-only, isolating auth from elicitation bugs
../.venv/bin/python client.py                  # full demo: OAuth + sampling x2 + elicitation + progress + cancellation
```

`client.py` prompts twice for sampling approval and, when ambiguity
fires, once more for the elicitation choice (a number, `decline`, or
empty/Ctrl-D for cancel). Run it at least three times, trying all three
outcomes — the decline and cancel paths are real branches, not just the
happy path.

## MCP Inspector

Inspector can fulfill `elicitation/create` requests interactively,
rendering the exact schema this server declares (the `enum` of
candidate `file_id`s) before you pick anything — a second, independent
check that the schema is well-formed (correct `type`, correct
`required`, an actual `enum` rather than an unconstrained string),
separate from this project's own client-side validation in
`elicitation_handler`. Same "second independent check" principle as
every prior step's Inspector use.

## Review checklist

- [x] Elicitation request carries a typed schema (`enum` of exactly the
      ambiguous `file_id`s), not a free-text prompt disguised as one —
      see "Building the elicitation request" above.
- [x] Decline path tested explicitly, distinct from cancel, distinct
      from "picked the first option" — see "Decline and cancel" above;
      both return an `{"ambiguous": true, ...}` marker, never a match list.
- [x] Sampling (`_rank_candidates_by_relevance`/`sampling_handler`) and
      elicitation (`_disambiguate_with_human`/`elicitation_handler`) are
      separate functions on both sides — see the comparison table above.
- [x] Server never proceeds past a flagged-ambiguous case without either
      a real human answer or an explicit decline/cancel branch — see
      `handle_call_tool`'s `if chosen_file_id is None` branch in server.py.
- [x] Inspector's fulfillment of the request matches this project's own
      client-side schema validation (same `enum`, same required field).

## Pitfalls addressed

- **Reusing the sampling capability/handler "because it's basically the
  same shape"** — avoided by keeping `_rank_candidates_by_relevance`/
  `sampling_handler` and `_disambiguate_with_human`/`elicitation_handler`
  as fully separate functions, each checking its own client capability,
  each with its own log lines — see the direction-flip proof above,
  where the two request/response shapes are visibly nothing alike.
- **Schema loose enough to be elicitation in name only** — avoided by
  constraining the answer to an `enum` of exactly the ambiguous
  `file_id`s (not an unconstrained string), and by having the server
  verify the client's answer is actually a member of that enum before
  trusting it (defense in depth against a client that declares the
  capability but doesn't honor the schema).
- **Silent fallback to `candidates[0]` on decline** — avoided by
  returning a distinctly-shaped `{"ambiguous": true, ...}` payload on
  decline/cancel instead of a normal match list, and by logging
  `decline` and `cancel` as visibly different actions rather than one
  "not accepted" bucket.
- **Assuming the ambiguity-detection prompt "just works" without running
  it** — the empty-completion token-budget bug (see above) was only
  found by actually running the decline path against the real Cerebras
  model, not by inspecting the prompt.
