# Step 9 — The real pipeline: Triage → Retriever → Drafter → Judge → Dispatcher

Every step through 8 proved a *capability*. This step is where those
capabilities become a product: one LangGraph graph, five nodes, that takes
a real email, retrieves grounding facts from Drive using Step 8's
sampling-ranked, elicitation-capable search completely unmodified, drafts a
reply, independently verifies every factual claim in that draft against
what was actually retrieved, and only then creates a Gmail draft —
escalating to a human instead of guessing whenever retrieval was ambiguous
or a claim can't be backed up.

## Files

- `state.py` — `GuardianState`, the one `TypedDict` carried node to node.
- `mcp_client.py` — composes Gmail (stdio) + Drive (Streamable HTTP + OAuth
  2.1 + sampling + elicitation) into one `"{server}::{tool}"` registry.
  Neither combination existed anywhere in Steps 1-8: Step 4's composition
  client held both servers but declared neither `sampling_callback` nor
  `elicitation_callback`; Step 8's client had both callbacks but only ever
  talked to Drive. Built here by carrying each piece forward from where it
  was actually proven — Gmail's connection shape from
  `step4-composition/client.py`, Drive's OAuth/sampling/elicitation
  plumbing from `step8-production/client.py` — per this project's standing
  convention of copying forward rather than importing across another
  step's folder.
- `graph.py` — the 5 node functions (`triage_node`, `retriever_node`,
  `drafter_node`, `judge_node`, `dispatcher_node`) plus `build_graph()`.
- `main.py` — entry point: opens both MCP sessions, builds the graph, runs
  it on one email, streams a full state transcript to
  `agents/transcripts/{timestamp}/`.
- `test_judge_catches_fabrication.py` — in-process proof Judge actually
  blocks a hand-injected fabricated claim, not just "would probably catch
  it" (see "Judge" below).

## Two things the step brief assumes exist but didn't — resolved before writing any code

1. **Gmail scope.** Steps 3-8 ran Gmail with full send/compose/modify
   access (`@gongrzhe/server-gmail-autoauth-mcp`), a flagged, deliberate-but-
   temporary deviation from this project's read-only-by-default principle,
   explicitly tied (project memory, not just a README note) to narrowing it
   together with first testing send/draft — at the point a real Dispatcher
   exists to test against. That's this step. Switched to
   [`@artymclabin/gmail-mcp`](https://www.npmjs.com/package/@artymclabin/gmail-mcp)
   (a maintained fork with a `--scopes` flag), re-authenticated with only
   `gmail.readonly` + `gmail.compose`. Full detail, including the real
   before/after `tools/list` evidence, in
   `step3-real-servers/gmail/README.md` — not duplicated here.

2. **No client ever combined Gmail + Drive with sampling/elicitation
   wired.** Built for real in `mcp_client.py` (see "Files" above).

## Retriever + Drafter — built and tested first, in isolation

Per the build order the brief asks for (don't build all five before testing
any), Retriever and Drafter were written and proven end to end — called
directly, not through the compiled graph — before Judge, Triage, or
Dispatcher existed. Real run, against the exact query Step 8's own README
already proved triggers a genuine elicitation round-trip (two real,
conflicting refund-policy docs in this Drive account):

```
=== Retriever: 'how do I get my money back for something I bought' ===
[sampling/createMessage: ranks candidates, flags ambiguous_group with both refund docs]
[elicitation/create: human chose 'Refund Policy']

ambiguity_resolution: was genuinely ambiguous -- human resolved it via elicitation,
  chose 'Refund Policy' (file_id=1mjV64wPNFoEhvZCBhh-H7xcG_r4PgEsWZzY_4HlLKbs)
retrieved: Refund Policy (file_id=1mjV64wPNFoEhvZCBhh-H7xcG_r4PgEsWZzY_4HlLKbs), 1793 chars

=== Drafter ===
Hi,
Thank you for reaching out. I'm sorry to hear that you're not satisfied with your purchase.
According to our refund policy, refund requests need to be submitted within 30 days of the
purchase date. Since you bought the item last month, you are still within that window. Once a
refund request is approved, the refund is processed within 5 business days and issued to the
original payment method. If the original payment was made with a gift card, the refund will be
provided as store credit rather than cash.
...
```

The draft uses only the human-chosen document's actual terms (5 business
days, 30-day window, original payment method, gift-card → store credit) —
none of the *other*, conflicting document's terms (10 business days, 14-day
window, store-credit-only) leak in. That's the real proof retrieval and
grounding work together, not just that a function returned without error.

**Real content, not metadata.** `drive::download_drive_file` (Step 8)
returns file metadata only (`file_id`/`name`/`mimeType`/`size_bytes` — it
exists to demonstrate chunked-download progress/cancellation, not to hand
back text). Actual document content comes from `resources/read` on the
`gdrive:///{file_id}` URI instead — exactly what `search_drive_files`' own
tool description says to do (`mcp_client.read_drive_resource`).

**Elicitation's outcome surfaces into state, not swallowed inside the tool
call.** `search_drive_files`' response shape can't tell "matched after a
human resolved a conflict" apart from "matched, never ambiguous" — Step 8's
server folds a resolved choice back into an ordinary `status="matched"`
list either way. `mcp_client.py`'s `elicitation_handler` records what
actually happened (`_last_elicitation`) at every one of its three return
paths (accept/decline/cancel); `retriever_node` pops that right after the
`search_drive_files` call to build `ambiguity_resolution` from real
evidence, not a guess from response shape.

## Judge — the actual point of this project

Zero MCP calls (per the brief) — a direct Cerebras call (same reasoning as
"Why Cerebras" below), not `sampling/createMessage`, since Judge isn't a
tool inside an MCP server. Extracts every factual claim `draft_reply`
makes and checks each independently against `retrieved_docs` — never
trusts Drafter's own "use only the retrieved content" instruction, which is
the entire premise of this node existing.

**A real subtlety, found by actually testing, not assumed away.** The real
Drafter output above turned out to contain a claim worth arguing about:
*"since you bought it last month, you're within the [30-day] window."*
"Last month" is imprecise — it could mean 3 days ago or 55 days ago — so
asserting "you're within the window" as settled fact is a genuine
overreach, not a safe restatement. The first version of Judge (given only
`draft_reply` + `retrieved_docs`, no email) flagged this — for the right
underlying reason (nothing about timing appears in the retrieved policy
doc) but for the wrong-sounding reason (it looked like it was penalizing
Drafter for referencing the email at all, which would also incorrectly
flag harmless restatements). Fixed not by teaching Judge to wave this
specific case through, but by giving it the email plus a three-way
distinction: **(a)** policy/knowledge-base facts, checked only against
retrieved content; **(b)** direct restatements/quotes of the customer's own
email, always fine; **(c)** inferences that combine an imprecise email
detail with an exact policy number to assert a conclusion as fact —
deliberately still `approved=False`, because the inference itself isn't
actually guaranteed by either source. Verified deterministic
(`temperature=0`, added after the same real draft was observed approved on
some runs and flagged on others at the default temperature) across five
repeated real runs before trusting this as Judge's genuine standard rather
than a one-off sample. This is exactly the brief's "don't trust an untested
branch" pitfall in practice — the fix came from watching real behavior
change across iterations, not from reading the prompt and assuming it
would work.

**Three cases, all real** — the first two came from actually running Judge
against the real Drafter output and observing what it did, not from
designing test cases first and writing a draft to match them
(`test_judge_catches_fabrication.py`):

```
=== Case 1: real, unedited Drafter output -- contains one risky inference ===
  approved=False unsupported_claims=['Since you bought the item last month, you are still within that window.']
  OK: Judge correctly refused to approve an inference an imprecise email detail ('last month')
  doesn't actually guarantee, even combined with a real policy deadline.

=== Case 2: same draft, risky-inference sentence removed (should approve cleanly) ===
  approved=True unsupported_claims=[]
  OK: Judge approved once every remaining claim is a direct policy fact or a plain
  restatement of the customer's own email.

=== Case 3: Case 2's draft + one hand-injected fabricated claim (must be blocked) ===
  injected claim: 'you can also request expedited refund processing within 24 hours for a $15 fee'
  approved=False unsupported_claims=['You can also request expedited refund processing within 24 hours for a $15 fee.']
  OK: Judge blocked approval and specifically named the fabricated claim, not a generic rejection.
```

(Case 3's fabricated claim: the real Refund Policy doc mentions expedited
*shipping* costing extra — nothing about a refund-processing fee — so this
sentence conflates two different things, not just invents a number from
nothing.)

**Fails closed on its own malformed output, too.** If Cerebras's response
can't be parsed as the expected `{"claims": [...]}` shape, `_judge_claims`
does not default to "approved" — it returns `approved=False` with the raw
response surfaced as evidence. An unparseable judge is exactly the failure
mode that must escalate, not silently pass.

## Triage + Dispatcher — added last

**Triage**: heuristic only, no LLM call, per the brief ("don't
over-engineer this node"). `intent` is the email body itself (the
customer's own question is already a reasonable natural-language query for
`drive::search_drive_files`, which ranks against free text, not Drive's own
query syntax); `urgency` is a keyword scan (`urgent`/`asap`/`immediately`/
`emergency`/`right away` → `"high"`, else `"normal"`). Real, genuinely-wired
capability to pull an actual message (`gmail::search_emails` +
`gmail::read_email`, `fetch_email_from_gmail` in `graph.py`) rather than a
hardcoded test email — reachable via `main.py --gmail-query <query>` — but
see "Canonical demo email" below for why the default run doesn't use it.

**Dispatcher**: if `judge_verdict.approved`, calls `gmail::draft_email`
(never `send_email`/`send_draft`, regardless of the verdict) and sets
`final_action = "drafted"`; otherwise sets `final_action = "held_for_review"`
and surfaces `unsupported_claims`, touching no MCP tool at all in that
branch — verified directly (`dispatcher_node` called with an *empty*
registry and a rejected verdict still returns `held_for_review` cleanly,
proving it never reaches for `gmail::draft_email` when not approved).

**`"drafted"`, not the brief's illustrative `"sent"`.** `GuardianState`'s
`final_action` literal type is `"drafted" | "held_for_review"`. Draft-only
is this step's explicit decision (see below); reusing "sent" to mean
"a draft was created" would misrepresent what actually happened to anyone
reading a transcript later.

**Draft-only is an application-level policy, not an OAuth-scope
guarantee.** Google's real `gmail.compose` scope bundles create-draft *and*
send together — there's no official Gmail scope that permits drafting but
not sending, so `send_email`/`send_draft` are still technically callable
under this grant (see `step3-real-servers/gmail/README.md`'s "one honest
caveat"). "Draft-only" here means Dispatcher's code simply never calls
those two tools — enforced by what this function does, not by what the
token allows.

## No conditional edges, anywhere

Five nodes, five `add_edge` calls, one straight line
(`triage → retriever → drafter → judge → dispatcher`) — `build_graph()`
never calls `add_conditional_edges`. Judge's approve/escalate and
Dispatcher's draft/hold outcomes are plain `if`/`else` inside the node
body; the graph's topology is identical regardless of which branch a given
run takes. Same lesson this project already applied once before (an
earlier project's join-mechanics bug traced to branching being expressed as
graph structure instead of in-node logic) — not new caution invented for
this step.

## Why Cerebras, not Anthropic (inherited from Steps 6-8, unchanged)

Same environment constraint as every other LLM call in this project (see
[`step6-sampling/README.md`, "Why Cerebras, not Anthropic"](../step6-sampling/README.md#why-cerebras-not-anthropic--an-environment-constraint-not-a-technical-preference)):
`CEREBRAS_API_KEY` is the only LLM credential this repo's `.env` actually
provisions, so it's what Drafter's and Judge's direct completions were
built against — a provider constraint, not a technical preference. Within
that constraint, `gpt-oss-120b` is the same deliberately-chosen model
Steps 6-8 already verified against this account's real `/v1/models` list.
Drafter and Judge call Cerebras **directly** (a plain `openai` SDK call),
never through MCP `sampling/createMessage` — neither of them runs inside an
MCP server, so there's no server-to-client inversion to make; that
primitive stays exactly where Step 8's `search_drive_files` uses it,
untouched.

## Canonical demo email — synthetic, and said so plainly

`main.py`'s default run uses a built-in synthetic-but-realistic refund
question, not a message pulled from this account's real inbox — that
inbox doesn't currently contain a refund question (its real content is the
"Security alert" / "finish setting up your account" messages
`step4-composition/README.md` already showed). Same transparency this
project used in Step 4's README for its own "this account's test data
doesn't happen to have a message and a doc about the same topic" caveat —
not staged to look real, just honestly not what's currently in the inbox.
Triage's real-Gmail-fetch path (`fetch_email_from_gmail`) stays genuinely
wired and is exercised via `main.py --gmail-query <query>`, just not what
the canonical, elicitation-exercising demo run depends on.

## Full end-to-end run — real OAuth, real Drive, real Cerebras, real Gmail draft

Two complete `main.py` runs against the same query, no flags skipped, human
answering the sampling approval and the elicitation menu for real each
time — deliberately two, not one: the first (transcript
`20260729-095019`) ran before `temperature=0` was added to Judge (see
"Judge" above) and its Drafter output happened to include the risky
"last month... within the window" inference, which the *current* Judge
would now correctly flag; the second (`20260729-100033`), captured after
that fix, is the one worth trusting as current behavior — shown here in
full, with the first kept on disk rather than deleted since a real,
unedited output is still evidence, just of an earlier state.

```
=== [1] triage ===
{"intent": "Hi, I bought something last month and I'm not happy with it. How do I get my money back?", "urgency": "normal"}

[sampling/createMessage -> ambiguous_group with both refund docs, same as the isolated Retriever run above]
[elicitation/create -> human chose 'Refund Policy']

=== [2] retriever ===
{"retrieved_docs": [{"file_id": "1mjV6...", "name": "Refund Policy", "content": "...(1770+ chars, the real doc)..."}],
 "ambiguity_resolution": "was genuinely ambiguous -- human resolved it via elicitation, chose 'Refund Policy' (file_id=1mjV64wPNFoEhvZCBhh-H7xcG_r4PgEsWZzY_4HlLKbs)"}

=== [3] drafter ===
{"draft_reply": "Hello,\n\nThank you for reaching out. To request a refund, please submit your refund request within 30 days of your purchase. Once your request is approved, refunds are processed within 5 business days and will be issued to the original payment method you used for the purchase. If you paid with a gift card, the refund will be provided as store credit rather than cash.\n\nIf you need assistance starting the refund process or have any additional questions, our support team is available Monday-Friday, 9am-6pm EST. We aim to respond to standard tickets within 24 hours.\n\n..."}

=== [4] judge ===
{"judge_verdict": {"approved": true, "unsupported_claims": []}}

=== [5] dispatcher ===
{"final_action": "drafted"}

Transcript written to: agents/transcripts/20260729-100033
```

This run's `draft_reply` happens to avoid the risky-inference pattern
entirely (it states the 30-day/5-day/gift-card facts plainly, without
asserting anything about *this specific customer's* timing), so Judge
approves cleanly — real evidence the current setup does, in fact, produce
a properly grounded draft end to end, not just that Judge can catch a bad
one when handed one directly.

**Independently confirmed the draft is real**, not just trusting the
tool's own success text — a fresh, separate `search_emails` call against
`in:drafts subject:Refund` (a different script, a fresh Gmail session) found
both runs' drafts, timestamps matching each run exactly:

```
ID: 19fad1ae74037101
Subject: Re: Refund question
From: test mcp <mcptest2k26@gmail.com>
Date: Wed, 29 Jul 2026 02:00:49 -0700

ID: 19fad11cb03defca
Subject: Re: Refund question
From: test mcp <mcptest2k26@gmail.com>
Date: Wed, 29 Jul 2026 01:50:52 -0700
```

**The held-for-review path, also verified directly** (not just inferred
from the Judge test passing): `dispatcher_node`, called with a rejected
`judge_verdict` and a deliberately *empty* registry, returns
`{"final_action": "held_for_review"}` cleanly — proving it never reaches
for `gmail::draft_email` on that branch, rather than assuming the `if`
guard is sufficient by reading it. The first (pre-fix) full run above is
also, incidentally, real evidence of what *would* now happen on that
draft — the current Judge would flag its risky sentence and Dispatcher
would hold it for review instead of drafting it.

## Transcript / replayability

`main.py` drives the graph via `astream(..., stream_mode="updates")` and
writes one JSON file per node into `agents/transcripts/{timestamp}/`:
`00_initial.json`, then `01_triage.json` … `05_dispatcher.json`, each
holding that node's own update *and* the full accumulated state at that
point. No checkpointer (`MemorySaver`/`SqliteSaver`) — that machinery is
for resuming interrupted, multi-turn runs across process restarts, which
nothing here asks for; this is the plain, complete story of one run,
readable file by file.

## Running it

Drive credentials (`~/.drive-mcp/token.json`, Step 3) and the narrowed
Gmail credentials (`~/.gmail-mcp/credentials.json`, this step — see
`step3-real-servers/gmail/README.md`) must already exist.
`CEREBRAS_API_KEY` in `.env` at the repo root is required (Drafter and
Judge both call it directly).

```
# terminal 1 -- Drive server, unmodified from Step 8
cd step8-production && ../.venv/bin/python server.py

# terminal 2 -- the pipeline
cd agents
../.venv/bin/python main.py                        # canonical demo: synthetic refund-policy email
../.venv/bin/python main.py --gmail-query "..."     # real email via gmail::search_emails/read_email
../.venv/bin/python test_judge_catches_fabrication.py
```

Both `main.py` and any script that calls `connect_drive()` will print a
`Opening browser for Drive consent: ...` URL and block until a human
approves it — Step 8's client deliberately uses in-memory token storage
(exercising the OAuth dance each run, no persisted credential the way
Gmail's stdio server has one on disk), so this happens on every run, not
just the first.

## Review checklist

- [x] Ran end-to-end on a real question against real Drive docs — the
      refund-policy conflict, the same ready-made test case Step 8 proved
      triggers elicitation. See "Full end-to-end run" above.
- [x] Judge demonstrably blocks a hand-injected fabricated claim — not
      "would probably catch it": `test_judge_catches_fabrication.py`, run
      and shown above, names the specific fabricated sentence it caught.
- [x] Elicitation from Retriever correctly surfaces up through the graph
      state, not swallowed silently — `ambiguity_resolution` in both the
      isolated and full-pipeline runs above states plainly that this was
      genuinely ambiguous and which file a human chose, built from real
      elicitation-handler evidence (`pop_last_elicitation`), not inferred
      from response shape.
- [x] Dispatcher defaults to draft-only, not real send — `gmail::draft_email`
      is the only Gmail write tool `dispatcher_node` ever calls; confirmed
      for real (a real Drafts-folder entry, independently found by a
      separate search) and confirmed the scope-level caveat (`gmail.compose`
      technically permits send too — see "Draft-only is an application-level
      policy" above) is documented, not glossed over.
- [x] Whole run is loggable/replayable — `agents/transcripts/{timestamp}/`,
      one JSON file per node, shown above.

## Pitfalls addressed

- **Judge trusting Drafter's self-report instead of independently checking
  claims** — avoided structurally: Judge never reads anything Drafter
  asserted about its own groundedness, only `draft_reply`'s text itself
  against `retrieved_docs` (and the original email, for the reason above).
- **Skipping the fabricated-claim test "because the prompt already says
  don't hallucinate"** — not skipped; see "Judge" above, including the real
  design gap the first attempt at this test surfaced and how it was fixed,
  not just the test passing on the first try.
- **Conditional graph edges reintroducing the join-mechanics bug class**
  — avoided by keeping the graph strictly linear and moving all branching
  inside node bodies; see "No conditional edges, anywhere" above.
