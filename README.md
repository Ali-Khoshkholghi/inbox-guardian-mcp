# Inbox Guardian

## Project summary

Inbox Guardian is an MCP-native support triage system. Emails arrive via
Gmail, get triaged, and are answered using facts grounded in a Drive-based
knowledge base — but only if the system can verify its own answer first; if
it can't, the case is escalated to a human instead of risking a bad send.
Built solo as a portfolio project, working step by step from raw MCP
fundamentals (hand-rolled JSON-RPC over stdio) up through advanced MCP
capabilities (multi-server composition, streaming transport, sampling,
elicitation) into a real, working five-node pipeline that creates a real
Gmail draft grounded in real Drive content, gated by an independent
correctness check.

## The journey, step by step

Nine steps, each building on the last without regressing it (every step's
own README has the full evidence — this is the one-line version):

1. **Fundamentals** — hand-rolled JSON-RPC over stdio: `initialize` →
   `initialized` → `tools/list` → `tools/call`, structured errors, no crashes.
2. **Resources & prompts** — a `ticket://` Resource per ticket and a
   `draft_reply` Prompt, both routed through one shared lookup helper, not
   two copies that happen to agree.
3. **Real Gmail + Drive MCP servers** — this repo's own Drive server
   (`drive.readonly`) and a third-party Gmail server, both verified against
   real accounts; Gmail's scope narrowed for real at Step 9 (see below).
4. **Multi-server composition** — one client holding Gmail + Drive + a toy
   server open concurrently, every tool namespaced `"{server}::{tool}"` to
   survive a real, deliberately-engineered name collision.
5. **Transport & streaming** — stdio → Streamable HTTP, plus full MCP-level
   OAuth 2.1 (dynamic client registration, PKCE, consent, signed JWTs),
   real chunked-download progress and cancellation.
6. **Sampling** — the server calls back into the *client's* LLM
   (`sampling/createMessage`) to rank Drive search results by relevance,
   holding no model credential of its own.
7. **Elicitation** — the server pauses mid-`tools/call` and asks a human to
   disambiguate a genuine content conflict, via a schema-validated form, not
   free text.
8. **Production hardening** — tool annotations, validated structured
   output, real leveled logging, a tested `--dry-run` guard, and git-SHA
   versioning, all layered onto Steps 5-7's server without changing its
   behavior.
9. **The real pipeline** — Triage → Retriever → Drafter → Judge →
   Dispatcher as one LangGraph graph, Gmail scope narrowed and a real
   Gmail draft created for the first time, Judge independently verifying
   every claim against retrieved content.

## Architecture

The final, working shape (Step 9): one LangGraph `StateGraph`
(`agents/graph.py`), five nodes, strictly linear edges — no conditional
routing anywhere; every approve/escalate or draft/hold decision is an
`if`/`else` inside a node, not graph structure:

```
Triage → Retriever → Drafter → Judge → Dispatcher
```

All five nodes share **one composed MCP client** (`agents/mcp_client.py`),
built specifically for this step because the combination didn't exist
before it:

- **Gmail** — `@artymclabin/gmail-mcp`, connected over **stdio**
  (`npx`). Scoped to `gmail.readonly` + `gmail.compose` only (narrowed at
  this step — see "Gmail scope" below).
- **Drive** — `step8-production/server.py`, connected over **Streamable
  HTTP with full MCP-level OAuth 2.1**, with `sampling_callback` and
  `elicitation_callback` both wired — required for `search_drive_files`
  to rank candidates and, on a genuine content conflict, pause and ask a
  human, exactly as built and proven in Steps 6-7.

Every tool is addressed as `"{server}::{tool_name}"` through one registry
(Step 4's namespacing pattern), so `gmail::draft_email` and
`drive::search_drive_files` can never be confused even though nothing in
the MCP protocol itself prevents two servers from exposing the same bare
name.

**Each node's role:**

- **Triage** — heuristic only, no LLM call. Classifies urgency by keyword
  and sets the retrieval query; can pull a real message via
  `gmail::search_emails`/`gmail::read_email` (genuinely wired), or take one
  already provided.
- **Retriever** — calls `drive::search_drive_files` (Step 8's tool,
  completely unmodified) and fetches the top match's real content via
  `resources/read`. Surfaces whatever elicitation actually decided (human
  resolved a conflict, or declined/cancelled and left it unresolved) into
  graph state, not swallowed inside the tool call.
- **Drafter** — a direct **Cerebras** completion (`gpt-oss-120b`, the
  OpenAI-compatible endpoint this project's environment provisions — see
  "Models & providers" below), instructed to use only the retrieved
  content. No MCP tools, no `sampling/createMessage` — Drafter doesn't run
  inside an MCP server, so there's no server↔client inversion to make here.
- **Judge — the project's core claim.** Also a direct Cerebras completion,
  **zero MCP calls**. Reads `retrieved_docs` **directly from graph state**
  — the same value Retriever populated, read independently — and checks
  every factual claim in `draft_reply` against that content itself.
  Structurally, there is no path for Drafter's own account of its sources
  to reach Judge at all: `drafter_node` returns only a plain reply string,
  no "sources used" field, nothing to trust even if Judge wanted to.
  Verification happens against the retrieved content, full stop — proven
  by testing it against a real draft, a real risky inference, and a real
  hand-injected fabrication, not by reading the prompt and assuming it
  works (see Step 9's row below and `agents/README.md`).
- **Dispatcher** — creates a Gmail draft (`gmail::draft_email`) when Judge
  approves; **never** calls `send_email`/`send_draft`, regardless of the
  verdict. This is an application-level policy Dispatcher enforces by
  simply never calling those tools — not something the OAuth scope itself
  blocks, since Google's real `gmail.compose` scope permits send too.
  Sets the case to human review, with the specific unsupported claims
  attached, when Judge doesn't approve.

## Models & providers

**Cerebras** (`gpt-oss-120b`, via its OpenAI-compatible endpoint) is the
one LLM this project calls anywhere — Drive's sampling-ranked search
(Steps 6-8, via `sampling/createMessage`) and Step 9's Drafter/Judge (direct
completions, no MCP round-trip) all use it. Named explicitly rather than
left implicit: this is an environment constraint, not a technical
preference — `CEREBRAS_API_KEY` is the only LLM credential this repo's
`.env` actually provisions, so it's what every LLM-calling piece of code
was built against. The specific model was still a deliberate pick within
that constraint (chosen by querying the account's real `/v1/models` list —
see `step6-sampling/README.md`), not a default guessed at.

## Gmail scope — narrowed at Step 9

Steps 3-8 ran Gmail with full send/compose/modify access
(`@gongrzhe/server-gmail-autoauth-mcp`), a flagged, deliberate-but-temporary
deviation from this project's read-only-by-default principle: that
package offered no scope-narrowing option, and testing send/draft for real
was only meaningful once a Dispatcher existed to test it — which is Step 9.
Switched to `@artymclabin/gmail-mcp` (a maintained fork with a `--scopes`
flag), re-authenticated with only `gmail.readonly` + `gmail.compose` — no
`gmail.modify`, no label/filter/delete access. Verified two ways, not
assumed: a re-authenticated `tools/list` genuinely omits label/filter/
delete-shaped tools, and a direct out-of-scope call (`delete_email`) is
rejected with a clear error. Full detail, including the honest caveat that
`gmail.compose` still technically permits send (there's no official
"draft-only" scope — Dispatcher's own code is what enforces that), in
`step3-real-servers/gmail/README.md` and `agents/README.md`.

## Repo structure

One folder per step. Each is self-contained with its own `README.md`
explaining what that step proves, plus a review checklist for what was
verified before moving on.

```
step1-fundamentals/    step2-primitives/    step3-.../   ...
```

## Step progress

| Step | Focus | Status | Verified |
|---|---|---|---|
| 1 | Fundamentals — raw JSON-RPC lifecycle over stdio | done | Hand-rolled client and MCP Inspector independently confirmed the same `initialize` → `initialized` → `tools/list` → `tools/call` lifecycle, schema, and structured error handling (`isError: true` on bad input, not a crash or protocol error). |
| 2 | Resources & prompts | done | Toy server extended with a `ticket://` Resource per ticket and a `draft_reply` Prompt that composes a Resource into a message at request time. Both handlers route through one shared `_read_ticket_resource` helper (not two copies that happen to agree) — proven by `test_shared_lookup.py`, an in-process spy test. Inspector's Prompts tab hit a real `-32601` on `completion/complete` (argument-suggestion requests for `ticket_id`), traced to a genuinely missing handler and fixed with a minimal `@server.completion()`. Confirmed both via raw wire probes and a fresh, fully-restarted Inspector session in the browser (T-1001 and T-1002 both work) — signed off. |
| 3 | Real Gmail + Drive MCP servers | done | **Drive** (this repo's own low-level `Server`, same style as Steps 1-2) — verified via MCP Inspector against a real Drive account: `resources/list` shows correct `gdrive:///{file_id}` URIs and mimeTypes; reading a regular file returns correct content via `get_media`; reading a Google Doc returns correct exported text via `export` (the previously-unverified branch); `search_drive_files` finds both files by keyword. `auth_setup.py` runs once, standalone, never lazy-triggered; `drive.readonly` scope only. **Gmail** — a third-party server, documented (not vendored) in `step3-real-servers/gmail/README.md`. Originally `@gongrzhe/server-gmail-autoauth-mcp` with full send/compose/modify access (no scope-narrowing option), flagged as a deliberate-but-temporary deviation; verified via Inspector: connects, lists tools, `in:inbox` search returns real messages. **Closed out at Step 9**: switched to `@artymclabin/gmail-mcp` (a maintained fork with a `--scopes` flag), re-authenticated with only `gmail.readonly` + `gmail.compose` — verified both by `tools/list` genuinely omitting label/filter/delete-shaped tools under the new grant and by a direct out-of-scope call (`delete_email`) being rejected. Send/compose (`draft_email`) verified for real at Step 9 (a real Gmail draft, independently confirmed via a separate search). |
| 4 | Multi-server composition (shared client/host) | done | One client (`step4-composition/client.py`) held Gmail, Drive, and Step 1's toy server open as three genuinely concurrent sessions via one `AsyncExitStack`. Every tool namespaced client-side as `"{server}::{tool_name}"` in a single registry dict — never looked up by bare name. Proved this against a real collision, not a hypothetical one: both `drive-server` and the toy server were given a genuinely identical `ping` tool, and the client correctly routed `drive::ping` / `toy::ping` to distinct servers with distinct, asserted-different responses. Combined operation ran a real `gmail::search_emails` and a real `drive::search_drive_files` in one script run, both returning live data. Closing the toy session mid-run and calling it again surfaced a loud `ClosedResourceError` instead of being silently swallowed, while `drive`/`gmail` kept working — confirming a dead server doesn't get hidden behind an apparently-fine run. All JSON-RPC frames from all three sessions logged into one `jsonrpc.log`, tagged `[gmail]`/`[drive]`/`[toy]`. **Regression check** — this step's `ping` addition to Step 1's and Step 3's servers was verified not to break either: Step 1's own unmodified `client.py` re-run against the modified server passed exit-0 with identical `get_ticket_summary` behavior (valid + invalid ticket id); Step 3's drive-server (which has no scripted client of its own, only manual Inspector checks) had its same set of checks — `resources/list`, regular-file read, Google-native export read, `search_drive_files` — re-scripted and re-run against the modified server, all reproducing exactly. Details in `step4-composition/README.md`. |
| 5 | Transport & streaming — stdio → Streamable HTTP + MCP OAuth 2.1 | done | A new Drive server variant (`step5-transport-oauth/server.py`, copied from Step 3, not edited in place) over Streamable HTTP. Transport verified unauthenticated first (`curl POST /mcp` → real `initialize` response) before layering auth on top — caught a real bug this way (Starlette treating a bare `async def` endpoint as GET-only, silently 405ing every POST; fixed with a class-based ASGI callable). `download_drive_file` reports genuine byte-level progress via `notifications/progress` (15 real updates across a 1.9MB file) and stops actual server-side work on `notifications/cancelled` — proven via server logs showing no further chunk-request line after cancellation, not inferred from the client giving up on waiting. Full MCP-level OAuth 2.1 in front: RFC 9728 protected-resource metadata, RFC 8414 AS metadata, PKCE auth-code flow (PKCE verification itself via the SDK's own TokenHandler), dynamic client registration, an interactive human-approved consent screen, and JWT access/refresh tokens signed via `joserfc` (authlib's own JOSE implementation) — audience, signature-tampering, and revocation all independently tested to reject, and a missing/garbage token gets a clean 401 with the resource-metadata pointer, never a crash or silent bypass. Independently verified with a fresh MCP Inspector instance (not just the project's own client), which caught two real bugs `client.py` alone never would have: Inspector's own local proxy failing to relay a discovery request (traced via the browser's Network tab to Inspector's port, not the server's — not our bug to fix) and `download_drive_file` 403ing on a Google-native Doc because it called `get_media()` unconditionally instead of branching on mimeType the way `resources/read` already did. Fixed by extracting one shared `_fetch_drive_file_content` helper both handlers now call, proven shared (not just coincidentally-equal output) by `test_shared_download.py`'s spy test — then Inspector re-verified the fix independently, successfully calling the tool against both a regular file and a Google Doc. |
| 6 | Sampling — server calls back into the client's LLM | done | Drive server variant (`step6-sampling/`, Step 5's Streamable HTTP + OAuth 2.1 server carried forward unmodified) whose `search_drive_files` no longer returns Drive's raw match list — it sends candidates to the client via `sampling/createMessage` and lets the client's LLM rank them by relevance, holding no LLM credential itself. The inversion (server → client mid-`tools/call`, the first request in this project to run that direction) proven with the literal 4-message sequence in one log: `tools/call` in, `sampling/createMessage` out, the client's completion back, the reordered result out. Ranking effect proven with a real query ("how do I get my money back") that has zero keyword overlap with any candidate filename — Drive's own raw order (4 files) collapsed to the one correct answer only after the sampling round-trip, logged before and after for a clean diff. The client's human-approval checkpoint was tested denying, not just approving — which caught a real bug (the demo crashed trying to JSON-parse a denial's plain-text error instead of checking `isError` first), fixed and re-verified. LLM access is Cerebras's OpenAI-compatible endpoint (`gpt-oss-120b`, chosen by querying the account's real `/v1/models` list) per this project's own credential setup, not Anthropic — the response is explicitly remapped into MCP's `CreateMessageResult` (including a `finish_reason` → `StopReason` translation), never passed through raw. Full flow re-verified together with Step 5's OAuth 2.1 dance (dynamic registration, PKCE, consent, bearer token) — sampling and auth both hold at once, not just individually. Two things flagged during review so they don't confuse a future reader of the logs: the repeated `ping` lines seen during a slow sampling approval are Streamable HTTP's own SSE keep-alive heartbeat (15s interval), not a bug or retry loop; and the same run's log incidentally re-confirmed Step 5's cancellation still holds unchanged (`notifications/cancelled` → `{"code":0,"message":"Request cancelled"}`, no further chunk requests) — not a dedicated regression test, it just fell out of the same run. |
| 7 | Elicitation — server pauses mid-`tools/call` and asks a human | done | Step 6's sampling-ranked server carried forward unmodified, plus `_disambiguate_with_human`/`elicitation_handler` as a fully separate primitive (own client capability check, own log lines, never conflated with sampling). Verified end to end in one real, cross-checked run (`step7-elicitation/jsonrpc.log`, both client wire log and server-side control flow checked, not just one side's report): OAuth 2.1 (discovery → dynamic client registration → consent → token) completing before any tool call; `sampling/createMessage` correctly returning `ambiguous_group: []` for a non-conflicting query ("a PDF file") and a populated `ambiguous_group` only for the two genuinely conflicting refund-policy docs ("how do I get my money back..." — original-payment-method/30-day vs. store-credit/14-day); `elicitation/create` firing with a schema-validated request (`enum` of exactly the ambiguous `file_id`s) and a schema-shaped human response (`action`/`content`, never a model completion); `download_drive_file` progress matching the real byte count exactly (1,903,362 bytes, 15 `notifications/progress` to 100%); and cancellation stopping genuine server-side work — only 2 progress notifications arrive before `notifications/cancelled`, and the server's own chunk-loop code (`server.py`) proves a `requesting chunk 3` log line was structurally never reached (the cancellation raises mid-pacing-sleep, before `chunk_index` increments a third time), not just unobserved. Decline and cancel tested as two distinct branches, both returning an `{"ambiguous": true, ...}` marker instead of guessing. A real bug (empty sampling completion from a token budget too small for the longer ambiguity-detection prompt) was caught by actually running the decline path, not by inspecting the prompt. Details in `step7-elicitation/README.md`. |
| 8 | Production hardening | done | Steps 5-7's server (`step8-production/server.py`) carried forward with unchanged behavior, hardened five ways. **Annotations**: every tool got `readOnlyHint=true, destructiveHint=false, idempotentHint=true, openWorldHint=true`, traced to actual code properties (no write scope, no accumulating side effect) rather than assumed, verified both on the raw wire (`jsonrpc.log`) and in a client reading only `tools/list` output, no source file open; the pattern for a future write tool (`destructiveHint=true`, requiring explicit human confirmation per spec) documented in `_guard_write`'s docstring since none exists yet. **Structured output**: `search_drive_files`/`download_drive_file` now return one Pydantic-validated shape each (published as each `Tool`'s `outputSchema`), collapsing Step 7's bare-list-vs-dict guessing game into a single `status`-discriminated object; a deliberately malformed value is proven to fail at the boundary two independent ways (Pydantic construction, and the SDK's own `jsonschema` check against the declared `outputSchema`) in `test_output_schemas.py`. **Real logging**: hardcoded `DEBUG`-always (Steps 5-7) replaced with a default-`INFO`, `LOG_LEVEL`/`--log-level`-configurable setup at real levels (DEBUG wire frames/chunk detail, INFO tool calls, WARNING elicitation declines/sampling denials, ERROR unexpected failures) — verified with the identical scenario run twice, INFO producing a 16-line log with zero wire frames, DEBUG producing 169 lines including them on demand. **`--dry-run`**: `_guard_write` plus a synthetic write probe (since no real write tool exists yet) proven, in both directions, to actually block/allow a write-capable code path in `test_dry_run_guard.py` — not just documented as blocking one, the exact pitfall this step warns against. **Versioning**: `serverInfo.version` is a real `git rev-parse HEAD` (with a `-dirty` suffix when the tree isn't clean) computed fresh at every startup, confirmed matching the actual repo state rather than hardcoded. **Verified live, end to end, not just in isolation**: beyond the per-item proofs above (each deliberately isolated via `--no-auth` and synthetic tests), one full real run of `client.py` — OAuth required, no flags skipped — exercised the whole chain together against a real Google Drive account and a real Cerebras completion, with a human answering both the sampling-approval prompt and the elicitation menu for real: OAuth handshake, `search_drive_files` correctly returning an empty `ambiguous_group` for a non-conflicting query and a populated one for the genuinely conflicting refund-policy docs, `elicitation/create` with a typed enum schema and a validated human choice, progress on a real 1.9MB download, and cancellation. `serverInfo.version` in that same session matched `git rev-parse HEAD` exactly, with no `-dirty` suffix (the commit had already landed). Not a mock or a stubbed Inspector session — see `step8-production/README.md`'s "Verified live run" for the full mapping and two explicitly-flagged gaps in that specific artifact (`--dry-run` wasn't exercised since nothing in this run attempted a write, and this session's own real-time logging stderr wasn't captured to a file, unlike the dedicated INFO-vs-DEBUG comparison earlier in the same README). Details in `step8-production/README.md`. |
| 9 | The real pipeline — Triage → Retriever → Drafter → Judge → Dispatcher | done | One LangGraph `StateGraph` (`agents/graph.py`), five nodes, strictly linear edges, **no conditional edges anywhere** — Judge's approve/escalate and Dispatcher's draft/hold outcomes are `if`/`else` inside the node body, not graph structure. `agents/mcp_client.py` composes Gmail (stdio) + Drive (Streamable HTTP + OAuth 2.1 + sampling + elicitation) into one client for the first time in this project — neither combination existed before (Step 4's client had both servers but no sampling/elicitation callbacks; Step 8's client had the callbacks but only ever talked to Drive). **Gmail scope narrowed here** (see Step 3's row above) as the explicit, memory-tracked trigger for that follow-up. **Built in the order the step specifies, not all five nodes at once**: Retriever+Drafter proven first, in isolation, against the real ambiguous refund-policy query already established in Step 8 — real content fetched via `resources/read` (not `download_drive_file`, which only returns metadata), a real elicitation round-trip, and a draft grounded exclusively in the human-chosen document's actual terms, none of the conflicting document's terms leaking in. **Judge** (zero MCP calls, a direct Cerebras completion) is the step's real center of gravity: testing it against a real draft surfaced a genuine subtlety — a claim combining an imprecise email detail ("last month") with an exact policy deadline (30 days) is a risky, unverified inference, not a safe restatement, and Judge (made deterministic with `temperature=0` after the same input was observed approved on some runs and flagged on others) now correctly and consistently refuses to approve it; a separate hand-injected fabrication (an invented "$15 expedited-refund fee") is reliably caught and named, not generically rejected — both proven in `agents/test_judge_catches_fabrication.py`, run repeatedly, not assumed from one pass. Triage (heuristic, no LLM, per the brief) and Dispatcher (creates a Gmail draft via `gmail::draft_email` only — never `send_email`/`send_draft`, an application-level policy since Google's `gmail.compose` scope technically permits send too) added last. **Full end-to-end runs, twice**, real OAuth/Drive/Cerebras/Gmail throughout: a real Gmail draft created and independently reconfirmed via a separate `search_emails` call (not just trusting the tool's own success text), and the `held_for_review` branch separately verified to never touch the Gmail registry when Judge rejects. Every run's complete 5-node state transcript written to `agents/transcripts/{timestamp}/`, one JSON file per node. Details in `agents/README.md`. |

## Running the full pipeline end-to-end

This is the Step 9 product — a real email in, a real Gmail draft (or a
human escalation) out. Three one-time setups, then two processes.

**0. Install dependencies (shared venv, once):**

```
python3.12 -m venv .venv
.venv/bin/pip install -e .
```

Add `CEREBRAS_API_KEY` to a `.env` at the repo root (see "Models &
providers" above — it's the only LLM credential this project needs).

**1. Drive OAuth (Google-level, once) — this repo's own server, read-only:**

1. Google Cloud Console → APIs & Services → Credentials → OAuth client ID
   → **Desktop app** → download the JSON → save as
   `~/.drive-mcp/gcp-oauth.keys.json`.
2. `cd step3-real-servers/drive-server && ../../.venv/bin/python auth_setup.py`
   — opens a real Google consent screen; on success writes
   `~/.drive-mcp/token.json`. Standalone script, run once — `server.py`
   only ever reads the resulting token, never triggers this itself. Scope
   is `drive.readonly`, unchanged since Step 3.

**2. Gmail OAuth (Google-level, once) — the scope-narrowed fork:**

1. Same Google Cloud Console OAuth client type (Desktop app) — a separate
   client ID, JSON saved as `~/.gmail-mcp/gcp-oauth.keys.json`.
2. `npx -y @artymclabin/gmail-mcp auth --scopes=gmail.readonly,gmail.compose`
   — opens a real Google consent screen; on success writes
   `~/.gmail-mcp/credentials.json` with the granted scopes recorded in it.
   Standalone, run once (see "Gmail scope" above for why these two scopes
   specifically, and how the narrowing was verified).

**3. Run it — two processes, two terminals:**

```
# terminal 1 — the Drive MCP server (Streamable HTTP + its own OAuth 2.1 layer)
cd step8-production
../.venv/bin/python server.py

# terminal 2 — the pipeline
cd agents
../.venv/bin/python main.py                        # canonical demo: synthetic refund-policy email
../.venv/bin/python main.py --gmail-query "..."     # real email via gmail::search_emails/read_email
```

**Two different OAuth layers, don't confuse them:** step 1/2 above is
*Google's* OAuth, done once, cached on disk. `step8-production/server.py`
additionally runs its **own** MCP-level OAuth 2.1 authorization server in
front of the Drive tools (Step 5) — deliberately using in-memory token
storage to exercise the full dynamic-registration/PKCE/consent dance every
run rather than caching it. So every `agents/main.py` run prints an
`Opening browser for Drive consent: ...` URL and blocks until that
*separate*, self-hosted consent page (not Google's) is approved — this is
expected, not a bug, and happens on every run, not just the first. Gmail's
credentials, by contrast, are cached after the one-time setup above and
need no per-run browser step.

`main.py` prints each node's output as it runs and writes the complete
state transcript to `agents/transcripts/{timestamp}/` — see
`agents/README.md` for a full annotated run, the Judge fabrication test,
and the review checklist.

## Setup / running an individual step (Steps 1-8)

To run any individual step's server/client in isolation (not the full
Step 9 pipeline above), `cd` into that step's folder and invoke scripts
with the shared venv's interpreter, e.g.:

```
cd step1-fundamentals
../.venv/bin/python client.py
```

Each step's own README has the exact run commands and what to check
(including, where relevant, an MCP Inspector pass:
`npx @modelcontextprotocol/inspector ../.venv/bin/python server.py`).

## Keeping this file current

This README is a living document, not a one-time snapshot. Update the step
progress table (and the architecture notes above, if the design evolves)
at the end of every step, before starting the next one.
