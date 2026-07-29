"""Entry point -- Inbox Guardian Step 9.

Opens Gmail (stdio) + Drive (Streamable HTTP + OAuth + sampling +
elicitation) as one composed session via `AsyncExitStack`, same shape
step4-composition/client.py and step8-production/client.py each already
use for their own single-server case, builds the tool registry, drives the
compiled graph, and streams the resulting state transcript to
`agents/transcripts/{timestamp}/` -- the "show someone the full state
transcript for one email" deliverable the review checklist asks for.

Requires step8-production/server.py already running separately (same
client/server process split every prior step uses):

    cd step8-production && ../.venv/bin/python server.py

Usage:

    ../.venv/bin/python main.py                        # canonical demo: synthetic refund-policy email
    ../.venv/bin/python main.py --gmail-query "query"   # real email, fetched via gmail::search_emails
"""

import argparse
import asyncio
import json
import logging
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path

from graph import build_graph, fetch_email_from_gmail
from mcp_client import build_registry, connect_drive, connect_gmail
from state import Email, make_initial_state

STEP_DIR = Path(__file__).parent
TRANSCRIPTS_DIR = STEP_DIR / "transcripts"

logger = logging.getLogger("inbox-guardian.agents.main")

# The canonical demo email -- synthetic, not pulled from this account's real
# inbox, which doesn't currently contain a refund question (see
# agents/README.md). Deliberately the same query Step 8's own README proved
# triggers a genuine elicitation round-trip (two conflicting refund-policy
# docs), so a default run exercises the whole chain for real, not a case
# with nothing interesting to show.
SYNTHETIC_TEST_EMAIL = Email(
    subject="Refund question",
    body="Hi, I bought something last month and I'm not happy with it. How do I get my money back?",
    sender="customer@example.com",
)


def _json_default(value):
    # dataclass/BaseModel-free state should never need this, but a stray
    # non-JSON-serializable value (e.g. an exception object accidentally
    # left in state) should show up as a readable string in the transcript
    # rather than crash the write.
    return str(value)


async def run(email: Email, run_dir: Path) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)

    async with AsyncExitStack() as stack:
        gmail_session = await stack.enter_async_context(connect_gmail())
        drive_session = await stack.enter_async_context(connect_drive())
        registry = await build_registry({"gmail": gmail_session, "drive": drive_session})

        graph = build_graph()
        initial_state = make_initial_state(email)

        accumulated: dict = dict(initial_state)
        (run_dir / "00_initial.json").write_text(json.dumps({"state": accumulated}, indent=2, default=_json_default))

        step = 0
        async for chunk in graph.astream(initial_state, context={"registry": registry}, stream_mode="updates"):
            for node_name, update in chunk.items():
                step += 1
                accumulated.update(update)
                (run_dir / f"{step:02d}_{node_name}.json").write_text(
                    json.dumps({"node": node_name, "update": update, "state_after": accumulated}, indent=2, default=_json_default)
                )
                print(f"=== [{step}] {node_name} ===")
                print(json.dumps(update, indent=2, default=_json_default))

        return accumulated


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gmail-query",
        default=None,
        help="Fetch a real email via gmail::search_emails/read_email instead of the built-in synthetic test email.",
    )
    args = parser.parse_args()

    logging.basicConfig(level="INFO", format="%(asctime)s [main] %(levelname)s %(message)s")

    run_dir = TRANSCRIPTS_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")

    if args.gmail_query:
        # Fetching the real email requires its own session, opened and
        # closed before `run()` opens the sessions it drives the graph
        # with -- kept separate rather than threading a pre-fetched email
        # through extra plumbing, since this path is the exception, not
        # the canonical demo (see module docstring).
        async with connect_gmail() as gmail_session:
            registry = await build_registry({"gmail": gmail_session})
            email = await fetch_email_from_gmail(registry, args.gmail_query)
        print(f"Fetched real email: subject={email['subject']!r} sender={email['sender']!r}")
    else:
        email = SYNTHETIC_TEST_EMAIL
        print(f"Using synthetic test email: subject={email['subject']!r}")

    final_state = await run(email, run_dir)

    print("\n=== Final state ===")
    print(json.dumps(final_state, indent=2, default=_json_default))
    print(f"\nTranscript written to: {run_dir}")


if __name__ == "__main__":
    asyncio.run(main())
