"""Tests for the bot's link-code lifecycle — run: python tools/test_link.py

Dependency-free: real schema, real accessors, throwaway DB, no Telegram. The
properties here are the ones that, if wrong, either hand a user an unusable code
or let one Telegram bind to an account that's already someone else's.
"""
from __future__ import annotations

import os
import sys
import tempfile

# bot/config.py requires these at import; set them before importing anything.
os.environ.setdefault("DAGMATE_BOT_TOKEN", "test:token")
os.environ.setdefault("DAGMATE_WEBHOOK_SECRET", "x" * 40)
os.environ["DAGMATE_BOT_DB"] = os.path.join(tempfile.mkdtemp(prefix="dagmate-link-"), "bot.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db  # noqa: E402

_failures: list[str] = []


def check(name: str, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        _failures.append(name)


def main_() -> int:
    db.ensure_schema()

    print("a fresh user gets a real, claimable code")
    code = db.new_link_code(1001)
    check("code minted", isinstance(code, str) and len(code) == 8, True)
    check("wrong code doesn't link", db.claim_link_code("ZZZZZZZZ", "acct-A"), False)
    check("the real code links", db.claim_link_code(code, "acct-A"), True)
    check("account is now linked", db.get_by_site_account("acct-A")["telegram_user_id"], 1001)

    print("!! an already-linked user gets None, not a phantom code (L-1)")
    # The upsert is a no-op for a linked row, so the code was never stored —
    # returning it would tell the user to paste something unclaimable.
    check("None returned", db.new_link_code(1001), None)

    print("a spent code can't be reused")
    check("second claim of the same code fails", db.claim_link_code(code, "acct-B"), False)

    print("re-issuing before claiming replaces the old code")
    c1 = db.new_link_code(2002)
    c2 = db.new_link_code(2002)
    check("new code minted", isinstance(c2, str), True)
    check("the OLD code no longer works", db.claim_link_code(c1, "acct-C"), False)
    check("the NEW code works", db.claim_link_code(c2, "acct-C"), True)

    print("one site account can't be claimed by two Telegrams")
    fresh = db.new_link_code(3003)
    check("duplicate account link is refused (no 500)", db.claim_link_code(fresh, "acct-A"), False)

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("all link-code checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_())
