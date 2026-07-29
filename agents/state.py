"""Shared graph state -- Inbox Guardian Step 9.

One `GuardianState` TypedDict carried node to node through the whole
Triage -> Retriever -> Drafter -> Judge -> Dispatcher chain. Plain
`TypedDict`, not a Pydantic `BaseModel`, despite this project using
Pydantic elsewhere (Step 8's tool output schemas): LangGraph only
validates a Pydantic state schema on the very first node, never
re-validates the partial-update dicts nodes 2-5 return, and hands back a
plain dict regardless of which schema type was declared -- so Pydantic
here would buy real per-node-entry overhead for validation that only
ever fires once. Judge and Drafter still build their own Pydantic models
internally to validate their own output before `.model_dump()`-ing it
into the dict a node returns (see graph.py) -- Pydantic at the point of
construction, plain dict at the LangGraph boundary, the same two-step
discipline Step 8's `_build_matched_result` used.

Every node returns only the keys it actually sets; unannotated
`TypedDict` fields use LangGraph's default last-write-wins reducer, which
is correct here since nothing in this state accumulates (no list a later
node appends to) -- each field is wholesale overwritten by exactly one
node.
"""

from typing import Literal, TypedDict


class Email(TypedDict):
    subject: str
    body: str
    sender: str


class RetrievedDoc(TypedDict):
    file_id: str
    name: str
    content: str


class JudgeVerdict(TypedDict):
    approved: bool
    unsupported_claims: list[str]


class GuardianState(TypedDict):
    email: Email
    intent: str | None
    urgency: str | None
    retrieved_docs: list[RetrievedDoc]
    ambiguity_resolution: str | None
    draft_reply: str | None
    judge_verdict: JudgeVerdict | None
    # Literal type is "drafted" | "held_for_review", not the step brief's
    # illustrative "sent" | "held_for_review" -- Dispatcher creates a Gmail
    # draft only, never sends (see agents/README.md), and "sent" would
    # misrepresent what actually happened to a future reader of a
    # transcript.
    final_action: Literal["drafted", "held_for_review"] | None


def make_initial_state(email: Email) -> GuardianState:
    """Build a fresh state for one graph run -- every non-`email` field
    explicitly `None`/empty rather than relying on `.get()` defaults
    downstream, so a node that reads a field before it's been set sees an
    explicit `None`, not a `KeyError`."""
    return GuardianState(
        email=email,
        intent=None,
        urgency=None,
        retrieved_docs=[],
        ambiguity_resolution=None,
        draft_reply=None,
        judge_verdict=None,
        final_action=None,
    )
