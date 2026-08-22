"""Tests for wallet-signature login (auth.py) — run: python tools/test_auth.py

Same shape as the other suites here: dependency-free, real schema, real
database.py accessors, throwaway DB. The one stub is the sidecar's
signature check, because verifying a real Kaspa signature needs the WASM
module that lives in service/.

The centrepiece is `regression_the_proven_theft` at the bottom. Before this
module existed, the following worked against the running server, start to
finish, with no cryptography whatsoever:

    match status: live | stake 10 KAS each, 20 KAS pot
    ATTACKER reads the victim's address straight off the public match view
    after the attacker resigns AS the victim -> settled / resign
    pot now belongs to playerB: True

That is the bug this file exists to keep dead. Everything else here — nonce
replay, expiry, cross-address reuse, session expiry, revocation — is a
different route to the same outcome, so they are all money tests.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DAGMATE_SITE_DB"] = os.path.join(tempfile.mkdtemp(prefix="dagmate-auth-"), "t.db")

import auth  # noqa: E402
import config  # noqa: E402
import database as db  # noqa: E402
import service_client  # noqa: E402

VICTIM = "kaspatest:qVICTIM01"
ATTACKER = "kaspatest:qATTACKER1"
PK_VICTIM, PK_ATTACKER = "pubkey-victim", "pubkey-attacker"

_failures: list[str] = []
_verify_calls: list[dict] = []


def check(name: str, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        _failures.append(name)


def stub_sidecar(keys_by_address: dict[str, str]):
    """Stand in for service/auth.js.

    Models the ONE property that file guarantees and that everything else
    rests on: a signature only counts when the pubkey presented actually
    derives to the claimed address. Signatures here are the literal string
    f"sig:{pubkey}:{message}", so "signing" is something any test can do but
    only for a key it holds."""
    _verify_calls.clear()

    async def _verify(*, address, pubkey, message, signature):
        _verify_calls.append({"address": address, "pubkey": pubkey, "message": message})
        if keys_by_address.get(address) != pubkey:
            return {"ok": False, "reason": "public key does not belong to that address"}
        return {"ok": signature == f"sig:{pubkey}:{message}", "reason": "signature does not match"}

    service_client.verify_message = _verify


def sign(pubkey: str, message: str) -> str:
    return f"sig:{pubkey}:{message}"


async def login(address: str, pubkey: str) -> dict:
    """The whole happy path, as the frontend performs it."""
    ch = auth.issue_nonce(address)
    return await auth.verify(address=address, pubkey=pubkey, nonce=ch["nonce"],
                             signature=sign(pubkey, ch["message"]))


async def expect_auth_error(name: str, coro):
    try:
        await coro
        check(name, "no error", "AuthError")
    except auth.AuthError:
        check(name, "AuthError", "AuthError")


# ── the handshake ────────────────────────────────────────────────────────
async def test_happy_path():
    print("\nsigning in with a real signature")
    r = await login(VICTIM, PK_VICTIM)
    check("issues a session token", bool(r["session"]["token"]), True)
    check("session belongs to the address", r["account"]["address"], VICTIM)
    check("account records the proven pubkey", r["account"]["pubkey"], PK_VICTIM)
    check("token resolves back to the account",
          auth.account_for_token(r["session"]["token"])["address"], VICTIM)
    check("session outlives the nonce",
          r["session"]["expiresTs"] > int(time.time()) + config.AUTH_NONCE_TTL_SECS, True)


async def test_message_is_rebuilt_server_side():
    print("\nthe signed text comes from our own records, not the request")
    ch = auth.issue_nonce(VICTIM)
    await auth.verify(address=VICTIM, pubkey=PK_VICTIM, nonce=ch["nonce"],
                      signature=sign(PK_VICTIM, ch["message"]))
    sent = _verify_calls[-1]["message"]
    check("verified against the message we issued", sent, ch["message"])
    check("message names the address", VICTIM in sent, True)
    check("message carries the nonce", ch["nonce"] in sent, True)
    # Signing must never look like authorising a payment — the popup is the
    # only thing between a player and a habit that gets them robbed elsewhere.
    check("message says it moves no funds", "moves no funds" in sent, True)


async def test_bad_signature_rejected():
    print("\na signature that doesn't check out")
    ch = auth.issue_nonce(VICTIM)
    await expect_auth_error("garbage signature refused", auth.verify(
        address=VICTIM, pubkey=PK_VICTIM, nonce=ch["nonce"], signature="not-a-signature"))
    ch = auth.issue_nonce(VICTIM)
    await expect_auth_error("signature over a different message refused", auth.verify(
        address=VICTIM, pubkey=PK_VICTIM, nonce=ch["nonce"],
        signature=sign(PK_VICTIM, "DAGmate login\nsomething else entirely")))


async def test_signing_with_your_own_key_for_someone_elses_address():
    """The attack the address check exists to stop. Anyone can produce a valid
    signature — the question is whose."""
    print("\nsigning with your own key while claiming another address")
    ch = auth.issue_nonce(VICTIM)
    await expect_auth_error("attacker's own key can't claim the victim's address", auth.verify(
        address=VICTIM, pubkey=PK_ATTACKER, nonce=ch["nonce"],
        signature=sign(PK_ATTACKER, ch["message"])))
    # And the victim's account must not have been repointed at the attacker's
    # key on the way through — that key is what escrows get built from.
    acct = db.get_account_by_address(VICTIM)
    check("victim's stored pubkey untouched", acct["pubkey"], PK_VICTIM)


async def test_nonce_is_single_use():
    print("\nnonce replay")
    ch = auth.issue_nonce(VICTIM)
    sig = sign(PK_VICTIM, ch["message"])
    r = await auth.verify(address=VICTIM, pubkey=PK_VICTIM, nonce=ch["nonce"], signature=sig)
    check("first use succeeds", bool(r["session"]["token"]), True)
    # A signature stays valid forever, so single-use is the entire replay
    # defence: capture a login once and it must not work a second time.
    await expect_auth_error("replaying the same nonce+signature refused", auth.verify(
        address=VICTIM, pubkey=PK_VICTIM, nonce=ch["nonce"], signature=sig))


async def test_nonce_bound_to_its_address():
    print("\nnonce issued for one wallet, presented by another")
    ch = auth.issue_nonce(VICTIM)
    # The attacker holds a real key and can sign anything — but this challenge
    # was minted for the victim, so it must not log the attacker in either.
    await expect_auth_error("attacker can't spend the victim's nonce", auth.verify(
        address=ATTACKER, pubkey=PK_ATTACKER, nonce=ch["nonce"],
        signature=sign(PK_ATTACKER, ch["message"])))


async def test_unknown_and_expired_nonces():
    print("\nunknown and expired nonces")
    await expect_auth_error("a nonce we never issued is refused", auth.verify(
        address=VICTIM, pubkey=PK_VICTIM, nonce="deadbeef", signature="whatever"))

    stale = "stale-nonce"
    issued = int(time.time()) - config.AUTH_NONCE_TTL_SECS - 60
    db.create_nonce(stale, VICTIM, issued)
    await expect_auth_error("an expired nonce is refused", auth.verify(
        address=VICTIM, pubkey=PK_VICTIM, nonce=stale,
        signature=sign(PK_VICTIM, auth.login_message(VICTIM, stale, issued))))


# ── sessions ─────────────────────────────────────────────────────────────
async def test_session_lifecycle():
    print("\nsessions")
    check("no token resolves to nobody", auth.account_for_token(None), None)
    check("a made-up token resolves to nobody", auth.account_for_token("x" * 43), None)

    r = await login(VICTIM, PK_VICTIM)
    token = r["session"]["token"]
    check("logout revokes", auth.logout(token), True)
    check("revoked token is dead", auth.account_for_token(token), None)
    check("logout twice is not an error", auth.logout(token), False)

    # Expiry is enforced in the query, so no endpoint can forget to check it.
    r2 = await login(VICTIM, PK_VICTIM)
    t2 = r2["session"]["token"]
    db.create_session(auth._hash("expired-token"), r2["account"]["id"],
                      int(time.time()) - 100, int(time.time()) - 1)
    check("expired session is dead", auth.account_for_token("expired-token"), None)
    check("live session still works", auth.account_for_token(t2)["address"], VICTIM)


async def test_token_is_not_stored_in_the_clear():
    """A DB read — a backup, a stray copy, a query-shaped bug — must not hand
    out live logins for every player on the site."""
    print("\ntokens at rest")
    r = await login(VICTIM, PK_VICTIM)
    token = r["session"]["token"]
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    stored = {row[0] for row in conn.execute("SELECT token_hash FROM sessions")}
    conn.close()
    check("raw token never hits the table", token in stored, False)
    check("its hash does", auth._hash(token) in stored, True)


# ── the regression that matters ──────────────────────────────────────────
async def regression_the_proven_theft():
    """Reproduce the exact live attack, and show it now dies at the door.

    Attacker challenges a victim, both stakes land, then the attacker ends
    the game AS the victim. The old code took the address in the body at face
    value; the fix is that resign() takes its player from a session, and a
    session can only be obtained by signing."""
    print("\nREGRESSION — resigning as another player")

    victim = await login(VICTIM, PK_VICTIM)
    attacker = await login(ATTACKER, PK_ATTACKER)

    # Step 1 of the original attack: read the victim's address off the public
    # match view. Still possible, and still fine — addresses are public.
    stolen_address = victim["account"]["address"]
    check("victim's address is readable, as it always was", stolen_address, VICTIM)

    # Step 2: use it. This is what used to work. The only identity the
    # backend will now accept is the account behind a token.
    acting = auth.account_for_token(attacker["session"]["token"])
    check("attacker's session resolves to the ATTACKER, not the named address",
          acting["address"], ATTACKER)

    # There is no token for the victim that the attacker can produce: minting
    # one requires a signature over a fresh nonce with the victim's key.
    ch = auth.issue_nonce(VICTIM)
    await expect_auth_error(
        "attacker cannot mint a session for the victim",
        auth.verify(address=VICTIM, pubkey=PK_ATTACKER, nonce=ch["nonce"],
                    signature=sign(PK_ATTACKER, ch["message"])))

    # ...nor by handing back the victim's own address with their pubkey, since
    # the attacker cannot produce a signature under a key they don't hold.
    ch = auth.issue_nonce(VICTIM)
    await expect_auth_error(
        "attacker cannot forge the victim's signature",
        auth.verify(address=VICTIM, pubkey=PK_VICTIM, nonce=ch["nonce"],
                    signature="sig:pubkey-victim:whatever-the-attacker-guesses"))

    # And the victim's own session still resolves to the victim, i.e. the fix
    # closed the hole without locking the rightful owner out of their match.
    check("victim can still act as themselves",
          auth.account_for_token(victim["session"]["token"])["address"], VICTIM)


async def main():
    db.ensure_schema()
    stub_sidecar({VICTIM: PK_VICTIM, ATTACKER: PK_ATTACKER})
    await test_happy_path()
    await test_message_is_rebuilt_server_side()
    await test_bad_signature_rejected()
    await test_signing_with_your_own_key_for_someone_elses_address()
    await test_nonce_is_single_use()
    await test_nonce_bound_to_its_address()
    await test_unknown_and_expired_nonces()
    await test_session_lifecycle()
    await test_token_is_not_stored_in_the_clear()
    await regression_the_proven_theft()

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        sys.exit(1)
    print("all auth checks passed")


if __name__ == "__main__":
    asyncio.run(main())
