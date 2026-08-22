"""DAGmate site backend — wallet-signature login (docs/DAGMATE_SPEC.md).

Before this existed, every endpoint identified its caller by an `address`
string in the request body, and addresses are public — they're printed on the
match view. That made a match takeable with no cryptography at all: challenge
someone, wait for both stakes to land, then POST /resign with THEIR address.
Settlement would then quite legitimately pay the attacker. The escrow
signatures were never the weak part; the game RESULT was.

The fix is the standard sign-in-with-a-wallet handshake:

    1. POST /api/auth/nonce {address}  → a single-use nonce and the exact
       text to sign.
    2. The wallet signs that text (Kasware/Kastle `signMessage`).
    3. POST /api/auth/verify {address, pubkey, nonce, signature} → the
       sidecar checks it against Kaspa's own message-verification, and we
       hand back a session token.
    4. Every mutating endpoint takes the account from the session and ignores
       any address in the body.

Three properties do the actual work:

  * **The message is rebuilt here, never accepted from the client.** We store
    the nonce with its address and reconstruct the signed text from that row.
    If we took the client's word for what was signed, any signature that
    player had ever produced anywhere could be presented as a login.
  * **The nonce is single-use**, via a guarded UPDATE. A signature is valid
    forever, so without this a captured login replays indefinitely.
  * **The pubkey must derive to the claimed address** — enforced in the
    sidecar (service/auth.js), where the key maths lives. A signature alone
    only proves *somebody* signed; it's the address derivation that makes it
    proof of *who*.

Tokens are random and stored only as a SHA-256 hash, so read access to the
DB doesn't hand out live sessions.
"""
from __future__ import annotations

import hashlib
import secrets
import time

import config
import database as db
import service_client

# Failure modes are deliberately not distinguished to the caller — "no such
# nonce", "already used", "expired" and "bad signature" all surface the same
# way, so a rejected login says nothing about which part was wrong.
class AuthError(RuntimeError):
    pass


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def login_message(address: str, nonce: str, issued_ts: int) -> str:
    """The exact text the wallet is asked to sign.

    Written for a human staring at a wallet popup, because that popup is the
    only thing standing between a player and signing whatever a malicious site
    put in front of them. It names the site, states plainly that nothing is
    being spent, and carries the nonce that makes it good exactly once.

    ⚠️ Byte-for-byte reproducible: this is regenerated at verify time and the
    signature is checked against it, so any change here invalidates every
    login challenge already in flight (harmless — they expire in minutes).
    """
    return (
        f"DAGmate login\n"
        f"Site: {config.AUTH_DOMAIN}\n"
        f"Address: {address}\n"
        f"Nonce: {nonce}\n"
        f"Issued: {issued_ts}\n"
        f"\n"
        f"Signing this proves you control this wallet. "
        f"It authorises no transaction and moves no funds."
    )


def issue_nonce(address: str) -> dict:
    if not address or not address.startswith("kaspa"):
        raise AuthError("a Kaspa address is required")
    now = int(time.time())
    db.purge_nonces(now - config.AUTH_NONCE_TTL_SECS * 10)
    nonce = secrets.token_hex(16)
    db.create_nonce(nonce, address, now)
    return {
        "nonce": nonce,
        "message": login_message(address, nonce, now),
        "expiresTs": now + config.AUTH_NONCE_TTL_SECS,
    }


async def verify(*, address: str, pubkey: str, nonce: str, signature: str) -> dict:
    """Turn a signed nonce into a session. Raises AuthError on any failure."""
    now = int(time.time())
    row = db.consume_nonce(nonce, now)
    if not row:
        raise AuthError("that login request is no longer valid — try connecting again")
    # Bound to the address it was issued for: a nonce handed out for one
    # wallet must not be usable to log in as another.
    if row["address"] != address:
        raise AuthError("that login request is no longer valid — try connecting again")
    if now - row["issued_ts"] > config.AUTH_NONCE_TTL_SECS:
        raise AuthError("that login request expired — try connecting again")

    message = login_message(row["address"], nonce, row["issued_ts"])
    result = await service_client.verify_message(
        address=address, pubkey=pubkey, message=message, signature=signature)
    if not result.get("ok"):
        raise AuthError("signature didn't check out — make sure you're signing with the connected wallet")

    account = db.get_or_create_account(address, pubkey)
    return {"session": _start_session(account["id"], now), "account": account}


def _start_session(account_id: str, now: int) -> dict:
    token = secrets.token_urlsafe(32)
    expires = now + config.AUTH_SESSION_TTL_SECS
    db.create_session(_hash(token), account_id, now, expires)
    return {"token": token, "expiresTs": expires}


def account_for_token(token: str | None) -> dict | None:
    if not token:
        return None
    return db.account_for_session(_hash(token), int(time.time()))


def logout(token: str | None) -> bool:
    if not token:
        return False
    return db.revoke_session(_hash(token), int(time.time()))
