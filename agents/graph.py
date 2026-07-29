"""LangGraph pipeline -- Inbox Guardian Step 9.

Five nodes, strictly linear (Triage -> Retriever -> Drafter -> Judge ->
Dispatcher), five `add_edge` calls, **no `add_conditional_edges` anywhere**
-- the same "branching is a decision inside a node, not a graph edge" rule
this project applied to an earlier join-mechanics bug. Judge's
approve/escalate and Dispatcher's draft/hold outcomes are plain `if`/`else`
inside the node body; the graph topology never changes based on them.

Built and tested in the order the step brief asks for, not all five nodes
at once: Retriever+Drafter first (the risk-bearing grounding logic, proven
directly against a real query before anything else existed -- see
`agents/README.md`), then Judge (proven against a hand-injected fabricated
claim in `test_judge_catches_fabrication.py`), then Triage+Dispatcher last.

External dependencies (the live MCP tool registry) are threaded into nodes
via LangGraph's `Runtime[GraphContext]` -- the current (1.x) idiom for
run-scoped, non-serializable context that doesn't belong in graph state
(live `ClientSession` objects can't be copied through state-update dicts).
"""

import logging
import os
import re
from pathlib import Path
from typing import TypedDict

from langgraph.graph import StateGraph
from langgraph.runtime import Runtime
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from mcp_client import Registry, dispatch, pop_last_elicitation, read_drive_resource, result_text
from state import Email, GuardianState, JudgeVerdict, RetrievedDoc

STEP_DIR = Path(__file__).parent
REPO_ROOT = STEP_DIR.parent
CEREBRAS_MODEL = "gpt-oss-120b"

logger = logging.getLogger("inbox-guardian.agents.graph")


class GraphContext(TypedDict):
    registry: Registry


def _drafter_cerebras_client() -> OpenAI:
    """Separate OpenAI-compatible client instance from mcp_client.py's --
    Drafter/Judge call Cerebras directly (no MCP `sampling/createMessage`
    round-trip, per the brief: Drafter "no MCP tools", Judge "zero MCP
    calls"), so this has no reason to share the same object as the
    sampling handler's client, even though it's the same provider/model
    for the reason documented in agents/README.md ("Why Cerebras" section
    -- environment-constrained, same as Steps 6-8)."""
    api_key = os.environ.get("CEREBRAS_API_KEY")
    if not api_key:
        raise RuntimeError("CEREBRAS_API_KEY not set -- check .env at the repo root")
    return OpenAI(api_key=api_key, base_url="https://api.cerebras.ai/v1")


_cerebras = _drafter_cerebras_client()


# --- Retriever ---------------------------------------------------------


async def _retrieve(registry: Registry, query: str) -> tuple[list[RetrievedDoc], str]:
    """Call drive::search_drive_files (Step 8's tool, completely
    unmodified -- still sampling-ranked, still elicits a human on genuine
    ambiguity) and fetch the top match's real content via resources/read.

    Returns (retrieved_docs, ambiguity_resolution) -- the latter always a
    human-readable note of what actually happened this call, built from
    real evidence (the tool's own structuredContent, plus
    `pop_last_elicitation()` for whether elicitation genuinely fired
    during *this* call -- see mcp_client.py's docstring for why that can't
    be inferred from the response shape alone) rather than a guess.
    """
    result = await dispatch(registry, "drive::search_drive_files", {"query": query})
    elicitation = pop_last_elicitation()

    if result.isError:
        resolution = f"search_drive_files call failed: {result_text(result)}"
        logger.warning(resolution)
        return [], resolution

    data = result.structuredContent
    if data is None:
        raise RuntimeError("drive::search_drive_files returned no structuredContent")

    status = data["status"]
    matches = data["matches"]

    if status == "ambiguous":
        names = [m["name"] for m in matches]
        detail = f"human {elicitation['action']}d disambiguation" if elicitation else "no elicitation outcome recorded"
        resolution = f"ambiguous, unresolved -- {detail} among {names}; no docs retrieved, escalating rather than guessing"
        logger.warning(resolution)
        return [], resolution

    if not matches:
        resolution = "matched -- zero relevant files found for this query"
        return [], resolution

    top = matches[0]
    content = await read_drive_resource(registry, top["file_id"])
    doc = RetrievedDoc(file_id=top["file_id"], name=top["name"], content=content)

    if elicitation and elicitation.get("action") == "accept":
        resolution = (
            f"was genuinely ambiguous -- human resolved it via elicitation, chose {top['name']!r} "
            f"(file_id={elicitation['chosen_file_id']})"
        )
    elif len(matches) > 1:
        resolution = f"matched, no ambiguity flagged -- {len(matches)} relevant files, using top-ranked {top['name']!r}"
    else:
        resolution = f"matched -- single relevant file {top['name']!r}, no ambiguity"

    return [doc], resolution


async def retriever_node(state: GuardianState, runtime: Runtime[GraphContext]) -> dict:
    registry = runtime.context["registry"]
    query = state["intent"] or state["email"]["subject"]
    docs, resolution = await _retrieve(registry, query)
    return {"retrieved_docs": docs, "ambiguity_resolution": resolution}


# --- Drafter -------------------------------------------------------------

_DRAFTER_SYSTEM_PROMPT = (
    "You draft customer-support email replies. Use ONLY the retrieved content "
    "provided to you -- never invent a policy detail, number, deadline, or fact "
    "that isn't explicitly present in it. If the retrieved content doesn't answer "
    "the question, say plainly that you don't have enough information rather than "
    "guessing. Write a complete, polite reply body, no subject line, no markdown."
)


async def _draft(email: Email, retrieved_docs: list[RetrievedDoc]) -> str:
    """Pure LLM call, no MCP tools -- direct Cerebras completion, not
    `sampling/createMessage` (Drafter doesn't run inside an MCP server, so
    there's no server-to-client inversion to make here)."""
    if not retrieved_docs:
        return (
            "Thanks for reaching out. I wasn't able to find documentation that "
            "confidently answers this, so I don't want to guess -- I'm looping in "
            "a teammate to take a closer look and get back to you."
        )

    docs_block = "\n\n".join(f"--- {d['name']} ---\n{d['content']}" for d in retrieved_docs)
    prompt = (
        f"Customer email:\nSubject: {email['subject']}\nFrom: {email['sender']}\n\n{email['body']}\n\n"
        f"Retrieved content (the ONLY source of facts you may use):\n{docs_block}\n\n"
        "Draft a reply using only the retrieved content above."
    )

    completion = _cerebras.chat.completions.create(
        model=CEREBRAS_MODEL,
        messages=[
            {"role": "system", "content": _DRAFTER_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        max_tokens=600,
    )
    return completion.choices[0].message.content or ""


async def drafter_node(state: GuardianState, runtime: Runtime[GraphContext]) -> dict:
    draft = await _draft(state["email"], state["retrieved_docs"])
    return {"draft_reply": draft}


# --- Judge -----------------------------------------------------------------
#
# The actual point of this project (per the brief) -- checks every factual
# claim in draft_reply against retrieved_docs, independently, rather than
# trusting Drafter's own "I only used the retrieved content" instruction.
# Zero MCP calls: this is plain business logic sitting in the graph, not a
# tool inside an MCP server, so there's no sampling/createMessage inversion
# to make here -- just a direct Cerebras call, like Drafter's.

_JUDGE_SYSTEM_PROMPT = (
    "You are a strict fact-checker for customer-support email replies. You are given "
    "three things: the customer's original email, a draft reply, and the retrieved "
    "source content the draft was supposed to be grounded in. Extract every concrete "
    "factual claim the draft makes about policy terms, dollar amounts, deadlines, "
    "procedures, or eligibility rules -- not pleasantries, not requests for more "
    "information. For each claim, decide which of these it is:\n"
    "  (a) A policy/knowledge-base fact (a rule, number, deadline, or procedure that "
    "would have to come from a documented source) -- check this ONLY against the "
    "retrieved source content. Supported only if the retrieved content actually "
    "states it, not if it merely sounds plausible.\n"
    "  (b) A direct restatement or quote of something the CUSTOMER's own email "
    "already said -- supported as long as it accurately reflects the email, even "
    "though it isn't itself present in the retrieved content.\n"
    "  (c) An inference the draft makes about the CUSTOMER's specific situation by "
    "combining an imprecise detail from their email (e.g. 'last month', 'a while "
    "ago') with an exact policy number (e.g. a 30-day deadline) to assert a "
    "conclusion as fact (e.g. 'you are still within the window'). Treat this as "
    "UNSUPPORTED unless the email's own wording is precise enough to make the "
    "conclusion certain -- an imprecise time reference does not make a hard deadline "
    "conclusion certain, and asserting it as fact risks being wrong. "
    "Respond with ONLY a JSON object: "
    "{\"claims\": [{\"claim\": \"...\", \"supported\": true|false}, ...]}. No other "
    "text, no markdown fences."
)


class _JudgeClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim: str
    supported: bool


class _JudgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: list[_JudgeClaim] = Field(default_factory=list)


def _extract_json_object(text: str) -> str:
    """Cerebras (like Step 8's sampling calls) sometimes wraps JSON in
    markdown fences or stray text despite instructions -- strip to the
    outermost {...} rather than fail on a strict json.loads first try."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return match.group(0) if match else text


async def _judge_claims(draft_reply: str, retrieved_docs: list[RetrievedDoc], email: Email | None = None) -> JudgeVerdict:
    """Registry-independent core: extract draft_reply's factual claims and
    check each against retrieved_docs, via a direct Cerebras call. Callable
    standalone (no MCP sessions needed) so `test_judge_catches_fabrication.py`
    can exercise it directly against a hand-edited draft.

    `email` is passed through so Judge can tell a direct restatement of
    something the customer's own email said (fine, even though it isn't
    itself in retrieved_docs) apart from a policy/knowledge-base fact that
    must trace to retrieved_docs -- and, a real distinction that only
    showed up once this was tested against a real draft rather than
    assumed to work: apart from a *risky inference* that combines an
    imprecise email detail with an exact policy number to assert a
    conclusion as fact (e.g. the real Drafter output's "since you bought it
    last month, you're within the [30-day] window" -- "last month" doesn't
    actually guarantee within-30-days, so asserting it as settled fact is a
    real overreach, not a safe restatement). Deliberately kept as
    `approved=False` for that category rather than taught to wave it
    through -- see agents/README.md's "Judge" section for the real,
    repeated (`temperature=0`) runs that showed this is Judge's genuine,
    deterministic standard, not a flake. `email=None` is still accepted
    (defaults to "no email context available") so the standalone
    fabrication test can pass only the two things it actually wants to
    isolate.

    An empty retrieved_docs with a non-trivial draft_reply is treated as
    "nothing can be supported" -- Drafter's own empty-docs fallback text
    ("I don't have enough information...") makes no factual claims, so it
    still comes back approved; a draft that *does* assert something with
    zero grounding correctly fails every claim.
    """
    if not draft_reply.strip():
        return JudgeVerdict(approved=True, unsupported_claims=[])

    docs_block = (
        "\n\n".join(f"--- {d['name']} ---\n{d['content']}" for d in retrieved_docs)
        if retrieved_docs
        else "(no content was retrieved)"
    )
    email_block = f"Subject: {email['subject']}\nFrom: {email['sender']}\n\n{email['body']}" if email else "(not provided)"
    prompt = (
        f"Customer's original email:\n{email_block}\n\n"
        f"Draft reply:\n{draft_reply}\n\n"
        f"Retrieved source content:\n{docs_block}\n\n"
        "Extract and check every factual claim."
    )

    completion = _cerebras.chat.completions.create(
        model=CEREBRAS_MODEL,
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        max_tokens=1200,
        # temperature=0: a fact-checker that gives a different verdict on
        # an identical input run to run is not trustworthy as a gate.
        # Observed directly during testing -- the same real, grounded draft
        # was approved on some runs and incorrectly flagged on others at
        # the default temperature (both before and after the email-context
        # fix above); explicitly zeroing this measurably reduced that
        # variance (see agents/README.md's "Judge" section for the before/
        # after evidence, not just this comment's claim).
        temperature=0,
    )
    raw = completion.choices[0].message.content or ""

    try:
        parsed = _JudgeResponse.model_validate_json(_extract_json_object(raw))
    except Exception as exc:
        # An unparseable judge response must not be silently treated as
        # "approved" -- that would defeat the entire point of this node.
        # Fail closed: escalate, and surface the raw response as evidence.
        logger.error("Judge: could not parse response (%s): %r", exc, raw)
        return JudgeVerdict(approved=False, unsupported_claims=[f"[Judge response unparseable, escalating: {raw[:300]!r}]"])

    unsupported = [c.claim for c in parsed.claims if not c.supported]
    return JudgeVerdict(approved=len(unsupported) == 0, unsupported_claims=unsupported)


async def judge_node(state: GuardianState, runtime: Runtime[GraphContext]) -> dict:
    verdict = await _judge_claims(state["draft_reply"] or "", state["retrieved_docs"], state["email"])
    return {"judge_verdict": verdict}


# --- Triage ----------------------------------------------------------------
#
# Heuristic only, no LLM call -- per the brief, this node isn't the
# interesting one. `intent` is the query text handed straight to
# drive::search_drive_files (the customer's own question is already a
# reasonable natural-language retrieval query -- Step 8's tool ranks
# against exactly this kind of free text, not Drive's own query syntax).
# `urgency` is a keyword scan, nothing more.

_URGENT_KEYWORDS = ("urgent", "asap", "immediately", "emergency", "right away")


def _classify(email: Email) -> tuple[str, str]:
    """Returns (intent, urgency). `intent` is the email body itself
    (falling back to the subject if the body is empty) -- simple on
    purpose; this node's job is routing, not summarization."""
    intent = email["body"].strip() or email["subject"].strip()
    haystack = f"{email['subject']} {email['body']}".lower()
    urgency = "high" if any(kw in haystack for kw in _URGENT_KEYWORDS) else "normal"
    return intent, urgency


async def triage_node(state: GuardianState, runtime: Runtime[GraphContext]) -> dict:
    intent, urgency = _classify(state["email"])
    return {"intent": intent, "urgency": urgency}


# --- Fetching a real email (Triage's "real message" capability) -----------
#
# Genuinely wired -- gmail:: namespaced, per Step 4's registry convention --
# but not what the canonical demo run uses by default; see agents/README.md
# for why (this account's real inbox doesn't currently contain a refund
# question). `main.py --gmail-query <query>` exercises this path for real.

_ID_LINE_RE = re.compile(r"^ID:\s*(\S+)", re.MULTILINE)
_SUBJECT_LINE_RE = re.compile(r"^Subject:\s*(.*)$", re.MULTILINE)
_FROM_LINE_RE = re.compile(r"^From:\s*(.*)$", re.MULTILINE)


async def fetch_email_from_gmail(registry: Registry, query: str) -> Email:
    """Search Gmail for `query` (real gmail::search_emails call), take the
    first match, and fetch its full content (real gmail::read_email call).
    Both text-formatted (no structuredContent -- third-party server, unlike
    Drive's Step 8 schemas), so this parses the same `ID:`/`Subject:`/
    `From:` line shape step4-composition/client.py's own printed output
    already showed for this server family."""
    search_result = await dispatch(registry, "gmail::search_emails", {"query": query, "maxResults": 1})
    if search_result.isError:
        raise RuntimeError(f"gmail::search_emails failed: {result_text(search_result)}")

    search_text = result_text(search_result)
    id_match = _ID_LINE_RE.search(search_text)
    if not id_match:
        raise RuntimeError(f"gmail::search_emails returned no message ID for query {query!r}:\n{search_text}")
    message_id = id_match.group(1)

    read_result = await dispatch(registry, "gmail::read_email", {"messageId": message_id})
    if read_result.isError:
        raise RuntimeError(f"gmail::read_email failed: {result_text(read_result)}")
    read_text = result_text(read_result)

    subject_match = _SUBJECT_LINE_RE.search(read_text)
    from_match = _FROM_LINE_RE.search(read_text)
    subject = subject_match.group(1).strip() if subject_match else "(no subject)"
    sender = from_match.group(1).strip() if from_match else "(unknown sender)"

    # Body: everything after the last recognized header line -- read_email's
    # output puts headers first, blank line, then the body; splitting on the
    # first blank line after the header block is more robust than assuming
    # a fixed header count.
    body = read_text.split("\n\n", 1)[1].strip() if "\n\n" in read_text else read_text.strip()

    return Email(subject=subject, body=body, sender=sender)


# --- Dispatcher --------------------------------------------------------------
#
# Draft-only, never send -- gmail::draft_email only, never gmail::send_email
# / gmail::send_draft, regardless of judge_verdict. This is an application-
# level policy this function enforces by simply never calling the other
# tools, not something the OAuth scope itself blocks (see
# step3-real-servers/gmail/README.md's "one honest caveat" -- gmail.compose
# permits send too; Google has no draft-only scope).


async def _create_gmail_draft(registry: Registry, email: Email, draft_reply: str) -> None:
    subject = email["subject"] if email["subject"].lower().startswith("re:") else f"Re: {email['subject']}"
    result = await dispatch(
        registry,
        "gmail::draft_email",
        {"to": [email["sender"]], "subject": subject, "body": draft_reply},
    )
    if result.isError:
        raise RuntimeError(f"gmail::draft_email failed: {result_text(result)}")
    logger.info("Dispatcher: created Gmail draft -- %s", result_text(result))


async def dispatcher_node(state: GuardianState, runtime: Runtime[GraphContext]) -> dict:
    registry = runtime.context["registry"]
    verdict = state["judge_verdict"]

    if verdict is not None and verdict["approved"]:
        await _create_gmail_draft(registry, state["email"], state["draft_reply"] or "")
        return {"final_action": "drafted"}

    logger.warning(
        "Dispatcher: holding for human review -- unsupported_claims=%s",
        verdict["unsupported_claims"] if verdict else "(no verdict)",
    )
    return {"final_action": "held_for_review"}


# --- Graph assembly ----------------------------------------------------------


def build_graph():
    """Five nodes, five edges, strictly linear -- no `add_conditional_edges`
    anywhere. Judge's approve/escalate and Dispatcher's draft/hold outcomes
    are internal `if`/`else` branches inside `judge_node`/`dispatcher_node`
    above; the graph topology never changes based on them."""
    g = StateGraph(GuardianState, context_schema=GraphContext)
    g.add_node("triage", triage_node)
    g.add_node("retriever", retriever_node)
    g.add_node("drafter", drafter_node)
    g.add_node("judge", judge_node)
    g.add_node("dispatcher", dispatcher_node)

    g.set_entry_point("triage")
    g.add_edge("triage", "retriever")
    g.add_edge("retriever", "drafter")
    g.add_edge("drafter", "judge")
    g.add_edge("judge", "dispatcher")
    g.set_finish_point("dispatcher")

    return g.compile()
