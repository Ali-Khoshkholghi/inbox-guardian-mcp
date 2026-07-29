# Inbox Guardian

## What this is

An MCP-native, five-node multi-agent pipeline that triages real support
email, retrieves grounding facts from a real Google Drive knowledge base,
drafts a reply, and independently verifies every factual claim in that
draft before anything is sent — blocking the reply and escalating to a
human if a claim isn't actually backed up. Real Gmail and Drive MCP
servers, real OAuth, a real correctness check, not a mock.

## The result, shown concretely

One real run (`agents/transcripts/20260729-100033/`), the ready-made test
case this project uses throughout: this Drive account has two real,
conflicting refund-policy documents, so retrieval can't just keyword-match
— it has to ask a human which one is meant.

**Input email:**
```
Subject: Refund question
From: customer@example.com

Hi, I bought something last month and I'm not happy with it. How do I get my money back?
```

**Retrieval hit a genuine conflict and asked a human**, not a guess:
`search_drive_files` flagged two plausibly-relevant, materially conflicting
docs ("Refund Policy": 5-day/30-day/original-payment-method vs. "Return &
Refund Policy": 10-day/14-day/store-credit-only) and paused mid-call to ask
which one was meant, via a schema-validated elicitation form — not free
text. State recorded exactly what happened:

```
ambiguity_resolution: "was genuinely ambiguous -- human resolved it via
elicitation, chose 'Refund Policy' (file_id=1mjV64wPNFoEhvZCBhh-...)"
```

**Retrieved content** (the actual document text fetched via
`resources/read`, not a summary):
```
Refund Policy
Refunds are processed within 5 business days of approval. Approved
refunds are issued to the original payment method. Refund requests
must be submitted within 30 days of purchase. Orders paid by gift
card are refunded as store credit, not cash.
...
```

**Drafted reply** (Cerebras, instructed to use only the retrieved content):
```
Hello,

Thank you for reaching out. To request a refund, please submit your refund
request within 30 days of your purchase. Once your request is approved,
refunds are processed within 5 business days and will be issued to the
original payment method you used for the purchase. If you paid with a
gift card, the refund will be provided as store credit rather than cash.
...
```

**Judge's verdict:** `{"approved": true, "unsupported_claims": []}` — every
claim traces to the retrieved doc. Dispatcher created a real Gmail draft;
independently reconfirmed with a separate `search_emails` call in a fresh
session (not just trusting the tool's own success message).

### The part that actually differentiates this project

Judge doesn't just rubber-stamp a well-behaved draft — it was tested
against a real hand-injected fabrication, and against a subtler, real
failure mode a genuine Drafter run produced on its own
(`agents/test_judge_catches_fabrication.py`):

```
=== Case 1: real, unedited Drafter output -- contains one risky inference ===
  approved=False unsupported_claims=['Since you bought the item last month, you are still within that window.']
```
An earlier real Drafter output asserted the customer was "still within the
[30-day] window" because they said they bought it "last month." Judge
correctly refused to approve this: "last month" doesn't actually guarantee
within 30 days (day 31–60 is also "last month") — a plausible-sounding
inference is not the same as a supported fact, and Judge holds that line
even when nothing in the sentence is an outright invention.

```
=== Case 3: cleanly-grounded draft + one hand-injected fabricated claim ===
  injected claim: 'you can also request expedited refund processing within 24 hours for a $15 fee'
  approved=False unsupported_claims=['You can also request expedited refund processing within 24 hours for a $15 fee.']
```
The real retrieved doc mentions expedited *shipping* costing extra —
nothing about a refund-processing fee. Judge caught the fabrication and
named it specifically, not a generic rejection.

Both blocks happen with **zero MCP calls** and **zero input from
Drafter about its own sourcing** — Judge reads `retrieved_docs` straight
from graph state, the same value Retriever populated, and checks
`draft_reply`'s claims against that content independently.
`agents/README.md` has the full three-case test and the design history
(including making this deterministic with `temperature=0`, after the
same input was observed approved on some runs and flagged on others).

## Architecture

```
                         agents/main.py
                              │
              ┌───────────────────────────────┐
              │   LangGraph, 5 nodes, linear    │
              │                                 │
              │  Triage → Retriever → Drafter    │
              │              → Judge → Dispatcher│
              │                                 │
              │  no conditional edges anywhere — │
              │  approve/escalate, draft/hold    │
              │  are if/else inside a node       │
              └───────────────┬─────────────────┘
                              │
                agents/mcp_client.py
              (one composed client, "server::tool" registry)
                   ┌──────────┴──────────┐
                   │                     │
            gmail:: (stdio)       drive:: (Streamable HTTP)
      @artymclabin/gmail-mcp      step8-production/server.py
      readonly + compose scope   OAuth 2.1 + sampling + elicitation
```

- **Triage** — heuristic only, no LLM call. Sets urgency/intent; can pull a
  real message via `gmail::search_emails`/`gmail::read_email`.
- **Retriever** — calls `drive::search_drive_files` (Step 8's tool,
  unmodified — sampling-ranked, elicits a human on genuine conflict) and
  fetches real content via `resources/read`.
- **Drafter** — direct Cerebras completion, no MCP tools, instructed to
  use only the retrieved content.
- **Judge** — direct Cerebras completion, zero MCP calls, verifies
  `draft_reply` against `retrieved_docs` read independently from state
  (see above).
- **Dispatcher** — creates a Gmail draft (`gmail::draft_email`) on
  approval; never calls `send_email`/`send_draft`, regardless of verdict.

## MCP primitives — what each one is and where it's actually proven

| Primitive | Built | Proven — not just implemented |
|---|---|---|
| **Tools** | Step 1 | `tools/call` schema validation and structured `isError` (not a crash) confirmed for both valid and invalid input, via a hand-rolled client *and* MCP Inspector independently. |
| **Resources** | Step 2 (toy) / Step 3 (real Drive) | `ticket://` and later `gdrive:///{file_id}` resources; both read paths route through one shared lookup helper, proven shared (not just coincidentally equal) by a spy test (`test_shared_lookup.py`). |
| **Prompts** | Step 2 | `draft_reply` prompt composes a Resource into a message; Inspector hit a real `-32601` on `completion/complete` (missing argument-suggestion handler) — a genuine bug found and fixed, not assumed away. |
| **Sampling** | Step 6 | Server-initiated `sampling/createMessage` ranks Drive candidates via the client's LLM. The human-approval gate was tested *denying*, not just approving — caught a real crash (JSON-parsing a denial's plain-text error instead of checking `isError` first). |
| **Elicitation** | Step 7 | Server pauses mid-`tools/call` to ask a human via a schema-validated form. Decline *and* cancel were tested as two distinct branches, not just the accept happy path — both leave the ambiguity correctly unresolved rather than guessing. |
| **HTTP transport (Streamable HTTP)** | Step 5 | Verified unauthenticated first (`curl POST /mcp`) before layering auth on top — this order caught a real bug (Starlette treating a bare `async def` endpoint as GET-only, silently 405ing every POST). |
| **OAuth 2.1** | Step 5 | Dynamic client registration, PKCE, consent, JWT access/refresh tokens. Audience checks, signature tampering, and revocation were all independently tested to actually *reject* — not just implemented and assumed correct. |

## How it was built — step by step

Full evidence for each step lives in that step's own README; this is the
progression, not the point of this document.

1. [Fundamentals](step1-fundamentals/README.md) — hand-rolled JSON-RPC over stdio.
2. [Resources & prompts](step2-primitives/README.md) — `ticket://` resource, `draft_reply` prompt.
3. [Real Gmail + Drive MCP servers](step3-real-servers/) — real accounts, real auth.
4. [Multi-server composition](step4-composition/README.md) — one client, `"server::tool"` namespacing, a real collision test.
5. [Transport & streaming](step5-transport-oauth/README.md) — Streamable HTTP + full OAuth 2.1.
6. [Sampling](step6-sampling/README.md) — server calls back into the client's LLM.
7. [Elicitation](step7-elicitation/README.md) — server pauses and asks a human.
8. [Production hardening](step8-production/README.md) — annotations, structured output, logging, `--dry-run`, versioning.
9. [The real pipeline](agents/README.md) — Triage → Retriever → Drafter → Judge → Dispatcher, real Gmail draft.

## Known limitations / deliberate deviations

Stated precisely rather than glossed over:

- **Dispatcher creates a Gmail draft, never sends.** `gmail::send_email`/
  `send_draft` are never called, regardless of Judge's verdict — an
  explicit choice to keep real send untested and out of scope for now, not
  an oversight.
- **`gmail.compose` scope technically still permits send.** Google has no
  official "draft-only" OAuth scope — the grant narrowed at Step 9
  (`gmail.readonly` + `gmail.compose`) removed label/filter/delete access,
  but `send_email`/`send_draft` remain technically callable under
  `gmail.compose`. Draft-only is enforced by Dispatcher's own code never
  calling them, not by what the token allows.
- **Drive's MCP-level OAuth uses in-memory token storage.** By design, to
  exercise the full dynamic-registration/PKCE/consent flow on every run
  rather than caching it — means a fresh browser-consent click every time
  `agents/main.py` runs, not just the first.
- **Judge's guarantee is a tested behavior, not a formal one.** It's a
  direct LLM call, made deterministic with `temperature=0` and proven
  against specific real cases (a clean draft, a risky inference, a hand-
  injected fabrication) — not a proof that covers every possible claim
  shape a future draft might produce.
- **The canonical demo email is synthetic.** This account's real inbox
  doesn't currently contain a refund question, so the default `main.py` run
  uses a built-in, realistic-but-synthetic one; the real-Gmail-fetch path
  (`--gmail-query`) is genuinely wired and separately available.

## Running it

```
python3.12 -m venv .venv && .venv/bin/pip install -e .
# one-time: Drive OAuth (step3-real-servers/drive-server/auth_setup.py)
# one-time: Gmail OAuth (npx -y @artymclabin/gmail-mcp auth --scopes=gmail.readonly,gmail.compose)

# terminal 1
cd step8-production && ../.venv/bin/python server.py

# terminal 2
cd agents && ../.venv/bin/python main.py
```

Full setup detail (GCP console steps, both auth flows) in `agents/README.md`.
