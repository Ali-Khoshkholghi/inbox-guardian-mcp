# Inbox Guardian

## Project summary

Inbox Guardian is an MCP-native support triage system. Emails arrive via
Gmail, get triaged, and are answered using facts grounded in a Drive-based
knowledge base — but only if the system can verify its own answer first; if
it can't, the case is escalated to a human instead of risking a bad send.
Built solo as a portfolio project, working step by step from raw MCP
fundamentals (hand-rolled JSON-RPC over stdio) up through advanced MCP
capabilities (multi-server composition, streaming transport, sampling,
elicitation) inside a real multi-agent pipeline.

## Architecture

A five-agent LangGraph pipeline, orchestrated as a linear chain:

```
Triage → Retriever → Drafter → Judge → Dispatcher
```

All five agents sit behind **one shared MCP client/host**, which composes
two MCP servers:

- **Gmail MCP server** — reads incoming mail, sends the final reply.
- **Drive MCP server** — the knowledge base agents retrieve facts from.

Each agent's role:

- **Triage** — classifies the incoming email (topic, urgency, whether it's
  answerable at all) and decides whether to proceed or route straight to a
  human.
- **Retriever** — pulls the specific facts/documents from the Drive
  knowledge base that are relevant to the email's question.
- **Drafter** — writes a candidate reply, citing/using only what the
  Retriever returned.
- **Judge** — checks every factual claim in the drafted reply against the
  facts the Retriever actually returned from Drive. If a claim in the
  draft isn't supported by retrieved content, the Judge blocks the send and
  routes the case to human escalation instead of letting it go out.
- **Dispatcher** — sends the verified reply via the Gmail MCP server (or
  hands the case to a human queue when the Judge blocks it).

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
| 3 | Real Gmail + Drive MCP servers | in progress | **Drive** (this repo's own low-level `Server`, same style as Steps 1-2) — verified via MCP Inspector against a real Drive account: `resources/list` shows correct `gdrive:///{file_id}` URIs and mimeTypes; reading a regular file returns correct content via `get_media`; reading a Google Doc returns correct exported text via `export` (the previously-unverified branch); `search_drive_files` finds both files by keyword. `auth_setup.py` runs once, standalone, never lazy-triggered; `drive.readonly` scope only. **Gmail** — a third-party server (`@gongrzhe/server-gmail-autoauth-mcp`, via `npx`), documented (not vendored) in `step3-real-servers/gmail/README.md`. Verified via Inspector: connects, lists tools, `in:inbox` search returns real messages. Flagged deviation: its OAuth consent grants full send/compose/modify, not read-only, since the package has no scope-narrowing option — follow-up is to switch to the `ArtyMcLabin/Gmail-MCP-Server` fork (`--scopes` flag) before any shared/deployed use. Send/compose not yet verified. |
| 4 | Multi-server composition (shared client/host) | done | One client (`step4-composition/client.py`) held Gmail, Drive, and Step 1's toy server open as three genuinely concurrent sessions via one `AsyncExitStack`. Every tool namespaced client-side as `"{server}::{tool_name}"` in a single registry dict — never looked up by bare name. Proved this against a real collision, not a hypothetical one: both `drive-server` and the toy server were given a genuinely identical `ping` tool, and the client correctly routed `drive::ping` / `toy::ping` to distinct servers with distinct, asserted-different responses. Combined operation ran a real `gmail::search_emails` and a real `drive::search_drive_files` in one script run, both returning live data. Closing the toy session mid-run and calling it again surfaced a loud `ClosedResourceError` instead of being silently swallowed, while `drive`/`gmail` kept working — confirming a dead server doesn't get hidden behind an apparently-fine run. All JSON-RPC frames from all three sessions logged into one `jsonrpc.log`, tagged `[gmail]`/`[drive]`/`[toy]`. **Regression check** — this step's `ping` addition to Step 1's and Step 3's servers was verified not to break either: Step 1's own unmodified `client.py` re-run against the modified server passed exit-0 with identical `get_ticket_summary` behavior (valid + invalid ticket id); Step 3's drive-server (which has no scripted client of its own, only manual Inspector checks) had its same set of checks — `resources/list`, regular-file read, Google-native export read, `search_drive_files` — re-scripted and re-run against the modified server, all reproducing exactly. Details in `step4-composition/README.md`. |
| 5 | Streaming transport | not started | |
| 6 | Sampling | not started | |
| 7 | Elicitation | not started | |
| 8 | Production hardening | not started | |

## Setup / running a step

Dependencies and Python version are declared in [`pyproject.toml`](pyproject.toml)
(project root). A single venv at `.venv/` (repo root) is shared by every
step.

```
python3.12 -m venv .venv
.venv/bin/pip install -e .
```

To run any given step's server/client, `cd` into that step's folder and
invoke scripts with the shared venv's interpreter, e.g.:

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
