"""Regression test: --dry-run must genuinely block a write-capable code
path, not just be documented as blocking one.

No tool in this server writes anything yet (SCOPES is still
drive.readonly, same discipline kept since Step 3) -- the pitfall this
step calls out explicitly is building a --dry-run flag that's a no-op
because there's nothing real to block. `_synthetic_write_probe` stands
in for whatever Step 9's Dispatcher eventually calls (e.g. a real Gmail
send), and this test proves `_guard_write` actually stops it under
DRY_RUN=True and actually lets it through under DRY_RUN=False -- both
directions, not just the happy one.

Runs in-process, no live account needed: this is a question about
in-process control flow (does a guarded coroutine raise or return),
not something the wire protocol needs to answer.
"""

import anyio

import server


def test_dry_run_blocks_the_synthetic_write() -> None:
    server.DRY_RUN = True
    try:
        anyio.run(server._synthetic_write_probe)
    except server.DryRunBlocked as exc:
        assert "synthetic" in str(exc)
    else:
        raise AssertionError("expected DryRunBlocked to be raised while DRY_RUN=True")
    finally:
        server.DRY_RUN = False


def test_normal_mode_lets_the_synthetic_write_through() -> None:
    server.DRY_RUN = False
    result = anyio.run(server._synthetic_write_probe)
    assert result.startswith("synthetic write executed")


if __name__ == "__main__":
    test_dry_run_blocks_the_synthetic_write()
    print("OK: --dry-run (DRY_RUN=True) blocked the synthetic write path via DryRunBlocked, as required")
    test_normal_mode_lets_the_synthetic_write_through()
    print("OK: normal mode (DRY_RUN=False) let the same synthetic write path proceed, as expected")
