"""Regression test: prompts/get must call the same lookup resources/read uses.

Comparing outputs isn't enough here — the earlier version of this step had
handle_read_resource and handle_get_prompt each index TICKETS directly, so
they produced identical text while running two independent copies of the
same logic. That passes any test that only checks "does the prompt contain
the ticket text," because both copies read from the same dict. It would NOT
catch one copy drifting from the other (e.g. someone fixes a bug in one
lookup and forgets the other exists).

This test proves shared code path, not just shared output: it patches
server._read_ticket_resource with a spy and asserts BOTH handlers call it
with the same ticket_id, at least once each. If either handler is ever
changed to index TICKETS directly again, the spy's call count for that
handler drops to zero and this test fails.

Runs in-process (imports server.py directly, no subprocess/stdio) since
the question is "which Python function got called," which the wire
protocol can't observe.
"""

import asyncio
from unittest.mock import patch

import server


def test_read_resource_and_get_prompt_share_lookup() -> None:
    ticket_id = "T-1002"
    expected_text = server.TICKETS[ticket_id]

    with patch.object(
        server, "_read_ticket_resource", wraps=server._read_ticket_resource
    ) as spy:
        contents = asyncio.run(
            server.handle_read_resource(server.AnyUrl(server._ticket_uri(ticket_id)))
        )
        prompt_result = asyncio.run(
            server.handle_get_prompt("draft_reply", {"ticket_id": ticket_id})
        )

        calls = [call.args[0] for call in spy.call_args_list]

    assert calls.count(ticket_id) >= 2, (
        "expected both handle_read_resource and handle_get_prompt to call "
        f"_read_ticket_resource({ticket_id!r}); saw calls: {calls!r}. "
        "If this fails, one of the two handlers has started reading TICKETS "
        "directly instead of going through the shared helper."
    )

    assert contents[0].content == expected_text
    assert expected_text in prompt_result.messages[0].content.text

    print("OK: resources/read and prompts/get both routed through "
          "_read_ticket_resource for the same ticket_id — shared code path "
          "confirmed, not just coincidentally-equal output.")


if __name__ == "__main__":
    test_read_resource_and_get_prompt_share_lookup()
