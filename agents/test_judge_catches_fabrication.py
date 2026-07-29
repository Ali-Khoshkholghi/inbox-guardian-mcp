"""In-process proof Judge actually blocks a fabricated claim -- Step 9 build
order item 2. Matches this project's existing `test_*.py` convention
(test_output_schemas.py, test_dry_run_guard.py, test_shared_lookup.py,
test_shared_download.py): don't trust an untested branch, actually attempt
the failure case and confirm it's caught.

Judge (`_judge_claims` in graph.py) makes zero MCP calls, so this needs no
MCP sessions at all -- it runs directly against `retrieved_docs` content
captured from a real run (agents/README.md's "Retriever + Drafter"
section), not a synthetic stand-in. `temperature=0` (graph.py) makes these
assertions safe to run more than once: repeated real runs against the
unedited draft below gave the identical verdict five times in a row before
this was trusted as a real, deterministic standard rather than a
one-off sample.

Three cases, all real -- the first two came from actually running this
against the real Drafter output, not from designing the test cases first
and writing a draft to match them:

  1. The real, unedited Drafter output -- turned out to include one
     genuine overreach: "since you bought it last month, you're within the
     [30-day] window" asserts a conclusion as fact from an imprecise email
     detail ("last month") combined with an exact policy deadline (30
     days). "Last month" does not actually guarantee within-30-days (day
     31-60 is also "last month"), so Judge correctly refuses to approve
     this claim -- treated here as Judge doing its job on a subtle case,
     not a bug to route around by loosening the prompt until it approves
     everything.
  2. The same draft with only that one risky-inference sentence removed --
     every remaining claim is either a direct policy fact
     (present in REAL_REFUND_POLICY_DOC) or a plain restatement of what the
     customer's own email said. Judge should approve this cleanly.
  3. Case 2's draft, hand-edited to insert one claim with zero support in
     REAL_REFUND_POLICY_DOC (a fabricated "$15 expedited-refund fee" --
     the real doc only mentions expedited *shipping* costing extra, a
     materially different thing this sentence conflates). Judge must
     reject this one, and must name the fabricated claim specifically, not
     just fail generically.
"""

import asyncio

from graph import _judge_claims
from state import Email, RetrievedDoc

# The real customer email from that run -- Judge needs this to tell a
# direct restatement of the email apart from a policy fact that must trace
# to retrieved_docs, and apart from a risky inference combining the two
# (see module docstring and graph.py's `_judge_claims` docstring).
REAL_EMAIL = Email(
    subject="Refund question",
    body="Hi, I bought something last month and I'm not happy with it. How do I get my money back?",
    sender="customer@example.com",
)

# Real content fetched via resources/read against this account's actual
# "Refund Policy" Drive doc (agents/README.md's Retriever+Drafter run) --
# not a hand-written stand-in.
REAL_REFUND_POLICY_DOC = RetrievedDoc(
    file_id="1mjV64wPNFoEhvZCBhh-H7xcG_r4PgEsWZzY_4HlLKbs",
    name="Refund Policy",
    content=(
        "Refund Policy\r\n"
        "Refunds are processed within 5 business days of approval. Approved\r\n"
        "refunds are issued to the original payment method. Refund requests\r\n"
        "must be submitted within 30 days of purchase. Orders paid by gift\r\n"
        "card are refunded as store credit, not cash.\r\n\r\n\r\n"
        "Shipping\r\n"
        "Standard shipping takes 3-7 business days within the continental US.\r\n"
        "Expedited shipping takes 1-2 business days and costs extra."
    ),
)

# The real, unedited Drafter output from that same run.
REAL_DRAFT = (
    "Hi,\n\n"
    "Thank you for reaching out. I’m sorry to hear that you’re not satisfied with your purchase.\n\n"
    "According to our refund policy, refund requests need to be submitted within 30 days of the "
    "purchase date. Since you bought the item last month, you are still within that window. Once a "
    "refund request is approved, the refund is processed within 5 business days and issued to the "
    "original payment method. If the original payment was made with a gift card, the refund will be "
    "provided as store credit rather than cash.\n\n"
    "To move forward, please let us know your order number and the payment method used for the "
    "purchase. With that information we can begin the refund process for you.\n\n"
    "If you have any other questions, feel free to let us know.\n\n"
    "Kind regards,\nCustomer Support Team."
)

# Case 2: the same draft, minus the one sentence that makes an unverified
# inference about the customer's specific timing -- everything else is
# identical, real Drafter text.
CLEANLY_GROUNDED_DRAFT = REAL_DRAFT.replace(
    "According to our refund policy, refund requests need to be submitted within 30 days of the "
    "purchase date. Since you bought the item last month, you are still within that window. Once a "
    "refund request is approved,",
    "According to our refund policy, refund requests need to be submitted within 30 days of the "
    "purchase date. Once a refund request is approved,",
)
assert CLEANLY_GROUNDED_DRAFT != REAL_DRAFT, "the risky-inference sentence was not found to remove"

# Case 3: Case 2's draft, hand-edited to insert one claim with zero support
# in REAL_REFUND_POLICY_DOC.
FABRICATED_CLAIM = "you can also request expedited refund processing within 24 hours for a $15 fee"
FABRICATED_DRAFT = CLEANLY_GROUNDED_DRAFT.replace(
    "provided as store credit rather than cash.",
    f"provided as store credit rather than cash. Additionally, {FABRICATED_CLAIM}.",
)
assert FABRICATED_DRAFT != CLEANLY_GROUNDED_DRAFT, "the fabricated claim was not inserted"


async def main() -> None:
    print("=== Case 1: real, unedited Drafter output -- contains one risky inference ===")
    verdict = await _judge_claims(REAL_DRAFT, [REAL_REFUND_POLICY_DOC], REAL_EMAIL)
    print(f"  approved={verdict['approved']} unsupported_claims={verdict['unsupported_claims']}")
    assert not verdict["approved"], f"expected the risky-inference sentence to be flagged, got: {verdict}"
    flagged_text = " ".join(verdict["unsupported_claims"]).lower()
    assert "last month" in flagged_text or "within that window" in flagged_text or "within the window" in flagged_text, (
        f"expected the flagged claim to be about the last-month/window inference, got: {verdict['unsupported_claims']}"
    )
    print("  OK: Judge correctly refused to approve an inference an imprecise email detail ('last month') "
          "doesn't actually guarantee, even combined with a real policy deadline.\n")

    print("=== Case 2: same draft, risky-inference sentence removed (should approve cleanly) ===")
    verdict = await _judge_claims(CLEANLY_GROUNDED_DRAFT, [REAL_REFUND_POLICY_DOC], REAL_EMAIL)
    print(f"  approved={verdict['approved']} unsupported_claims={verdict['unsupported_claims']}")
    assert verdict["approved"], f"expected the cleanly-grounded draft to be approved, got: {verdict}"
    print("  OK: Judge approved once every remaining claim is a direct policy fact or a plain "
          "restatement of the customer's own email.\n")

    print("=== Case 3: Case 2's draft + one hand-injected fabricated claim (must be blocked) ===")
    print(f"  injected claim: {FABRICATED_CLAIM!r}")
    verdict = await _judge_claims(FABRICATED_DRAFT, [REAL_REFUND_POLICY_DOC], REAL_EMAIL)
    print(f"  approved={verdict['approved']} unsupported_claims={verdict['unsupported_claims']}")
    assert not verdict["approved"], f"expected the fabricated claim to be caught and block approval, got: {verdict}"
    assert verdict["unsupported_claims"], "expected at least one named unsupported claim, got an empty list"
    flagged_text = " ".join(verdict["unsupported_claims"]).lower()
    assert "24 hour" in flagged_text or "$15" in flagged_text or "expedited refund" in flagged_text, (
        f"Judge blocked the draft but didn't clearly name the fabricated claim -- got: {verdict['unsupported_claims']}"
    )
    print("  OK: Judge blocked approval and specifically named the fabricated claim, not a generic rejection.\n")

    print("All three cases passed -- Judge distinguishes grounded facts, restatements, risky inferences, "
          "and outright fabrication.")


if __name__ == "__main__":
    asyncio.run(main())
