"""DAGmate site backend (docs/DAGMATE_SPEC.md) — FastAPI app: wallet/profile,
challenges, matches (python-chess rules), tournaments, learn page. Talks to
service/ (Node, Kaspa sidecar) for escrow addresses and to bot/ (Telegram
alerts) best-effort. Serves the static frontend too, so `uvicorn main:app`
is the one process to run locally.

WHO THE CALLER IS: every mutating endpoint resolves its account from a
session token (auth.py), never from an address in the request body. An
address is public — it's printed on the match view — so trusting one meant
anyone could POST /resign as anyone and walk off with the pot. If you add an
endpoint that changes state or reveals a player's own data, take
`account: dict = Depends(require_account)` and use that; there is no
supported way to name a different player.

Known, deliberate gaps in this pass (see docs/DAGMATE_SPEC.md and the
project memory for the full picture) — flagged here rather than silently
faked:
  - `/api/matches/{id}/dev-mark-funded` skips the on-chain deposit check
    entirely — it conjures a pot. It now 404s unless DAGMATE_DEV_ROUTES=1,
    which is off by default and refused outright on mainnet, but the route
    still exists in the file. Don't "tidy" it into something gated on being a
    player in the match; making it look safe is how it survives to production.
  - Settlement is wired (settlement.py) but its signature round-trip has never
    run against a real wallet extension — `signPskt()` needs Kasware/Kastle,
    which a headless preview doesn't have. The orchestration is unit-tested;
    the signatures themselves are not yet proven end-to-end. The same is true
    of the login signature: `signMessage()` is verified against Kaspa's own
    WASM implementation, but not yet against a real extension's output.
  - Learn-level gas payments are recorded optimistically, not yet verified
    against a real on-chain payment.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import threading

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import auth
import bot_client
import chess_logic
import clocks
import config
import curriculum
import database as db
import deposits
import engine
import kns
import reclaim
import service_client
import settlement
from service_client import ServiceError

log = logging.getLogger("dagmate.site")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="DAGmate")


# Security headers on EVERY response, including the static HTML page. In
# production nginx also sets a CSP, but a local/dev `uvicorn main:app` run has
# no nginx in front — and DAGmate is meant to be run and poked at by hostile
# eyes on testnet — so the app must carry its own baseline rather than rely on
# a reverse proxy that isn't always there.
#
# The policy is deliberately tight: scripts and XHR/fetch only from our own
# origin (so a stored-XSS payload in, say, a hostile KNS name can't beacon a
# stolen session token to an attacker's host), the page can't be framed
# (clickjacking on the one-click Resign button), and only Google Fonts is
# allowed off-origin. 'unsafe-inline' is granted for STYLE only — the app uses a
# handful of inline style attributes and the Google Fonts stylesheet — never for
# script; there are no inline <script> blocks or on* handlers to need it.
_CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "object-src 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)


@app.middleware("http")
async def _security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=(), payment=()")
    return resp


@app.on_event("startup")
async def _startup():
    db.ensure_schema()
    if config.DEPOSIT_WATCH_ENABLED:
        asyncio.create_task(deposits.watch_loop())
    else:
        log.warning("deposit watcher DISABLED — matches will never go live on their own")
    if config.DEV_ROUTES:
        log.warning("⚠️  DEV ROUTES ENABLED (%s) — /api/dev/* and dev-mark-funded are live. "
                    "dev-mark-funded starts a match nobody paid for.", config.NETWORK_ID)
    asyncio.create_task(clocks.watch_loop())


def _short(addr: str) -> str:
    return addr if len(addr) <= 14 else f"{addr[:8]}…{addr[-6:]}"


def _account_public(a: dict) -> dict:
    return {
        "id": a["id"], "address": a["address"], "shortAddress": _short(a["address"]),
        "knsName": kns.primary_name(a["address"]),
        "acceptChallenges": bool(a["accept_challenges"]), "isDemoWallet": bool(a["is_demo_wallet"]),
        # Whether an escrow can be built for this player at all. The key
        # itself never leaves the server — the UI only needs to know if it's
        # there, and a pubkey is one lookup away from a balance.
        "hasPubkey": bool(a["pubkey"]),
    }


def _require_dev_routes() -> None:
    """Gate for every testing affordance on this process (config.DEV_ROUTES).

    404 rather than 403 on purpose: a disabled dev route should be
    indistinguishable from a route that was never built, so probing a
    deployment tells an attacker nothing about what's switched off."""
    if not config.DEV_ROUTES:
        raise HTTPException(404, "not found")


# ── who's calling ───────────────────────────────────────────────────────
def _token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def require_account(authorization: str | None = Header(default=None)) -> dict:
    """The caller's account, proven by a wallet signature (see auth.py).

    Use this on ANYTHING that changes state or returns a player's own data.
    It is the only supported way to learn who is calling — no endpoint should
    ever take an address from the request and treat it as identity."""
    a = auth.account_for_token(_token(authorization))
    if not a:
        raise HTTPException(401, "connect and sign in with your wallet")
    return a


def optional_account(authorization: str | None = Header(default=None)) -> dict | None:
    """For reads that are richer when signed in but fine when not (the open
    challenge board). Never gates a write."""
    return auth.account_for_token(_token(authorization))


async def _reclaim_daa() -> int:
    """Live DAA score + the standard 14-day CLTV window, as the ABSOLUTE DAA at
    which a stranded stake becomes reclaimable.

    There is deliberately NO fallback. A placeholder window (just
    RECLAIM_DAA_WINDOW with no live baseline) is millions of DAA below the real
    chain height, so an escrow built with it has an ALREADY-OPEN timelock — a
    player could drain their own stake mid-game. If the node can't be reached we
    let this raise (ServiceError) and refuse to create the match at all, rather
    than build an unsafe escrow. Failing a match creation is recoverable; a live
    escrow whose reclaim branch is open from block one is not."""
    current = await service_client.daa_score()
    return current + config.RECLAIM_DAA_WINDOW


def _challenge_public(ch: dict) -> dict:
    frm = db.get_account(ch["from_account_id"])
    to = db.get_account(ch["to_account_id"]) if ch["to_account_id"] else None
    return {
        "id": ch["id"], "status": ch["status"], "mode": ch["mode"],
        "stakeKas": ch["stake_sompi"] / config.SOMPI_PER_KAS, "gasOnly": bool(ch["gas_only"]),
        "fromAccountId": ch["from_account_id"], "toAccountId": ch["to_account_id"],
        "fromAddress": frm["address"] if frm else None, "fromShort": _short(frm["address"]) if frm else None,
        "fromKns": kns.cached_name(frm["address"]) if frm else None,
        "toAddress": to["address"] if to else None, "toShort": _short(to["address"]) if to else None,
        "toKns": kns.cached_name(to["address"]) if to else None,
        "createdTs": ch["created_ts"],
    }


def _match_public(m: dict) -> dict:
    a = db.get_account(m["player_a_account_id"])
    b = db.get_account(m["player_b_account_id"])
    return {
        "id": m["id"], "status": m["status"], "mode": m["mode"],
        "stakeKas": m["stake_sompi"] / config.SOMPI_PER_KAS,
        "fen": m["fen"], "turn": m["turn"], "result": m["result"],
        "playerA": {"address": a["address"], "shortAddress": _short(a["address"]),
                    "knsName": kns.cached_name(a["address"])} if a else None,
        "playerB": {"address": b["address"], "shortAddress": _short(b["address"]),
                    "knsName": kns.cached_name(b["address"])} if b else None,
        "winnerAccountId": m["winner_account_id"],
        "escrowA": m["escrow_a_address"], "escrowB": m["escrow_b_address"],
        # Published on purpose: with the redeem script and the timelock, a
        # player can spend their own escrow's reclaim branch without DAGmate
        # existing at all. That's what makes the non-custodial claim checkable
        # rather than a promise. They're public data either way — a P2SH script
        # is revealed the first time it's spent.
        "escrowARedeemHex": m["escrow_a_redeem_hex"], "escrowBRedeemHex": m["escrow_b_redeem_hex"],
        "reclaim": reclaim.summary(m),
        "tournamentId": m["tournament_id"], "round": m["round"],
        # Deposit progress, so the player can see whose stake is outstanding
        # rather than staring at "awaiting_deposit" with no explanation.
        "funding": {
            "stakeKas": m["stake_sompi"] / config.SOMPI_PER_KAS,
            "aKas": (m["funded_a_sompi"] or 0) / config.SOMPI_PER_KAS,
            "bKas": (m["funded_b_sompi"] or 0) / config.SOMPI_PER_KAS,
            "aFunded": m["funded_a_ts"] is not None,
            "bFunded": m["funded_b_ts"] is not None,
            "checkedTs": m["deposit_checked_ts"],
            "deadlineTs": m["created_ts"] + config.DEPOSIT_DEADLINE_SECS,
            "windowMins": config.DEPOSIT_DEADLINE_SECS // 60,
        },
        # A standing draw offer, by colour rather than account id — the client
        # already knows which colour it is (it has to, to move) and never sees
        # account ids, so colour is the whole answer without leaking one.
        "drawOffer": {"byColor": _color_of(m, m["draw_offer_by"])} if m["draw_offer_by"] else None,
        "clock": clocks.public(m),
    }


def _color_of(m: dict, account_id: str | None) -> str | None:
    """Player A is always white, player B always black."""
    if account_id and account_id == m["player_a_account_id"]:
        return "white"
    if account_id and account_id == m["player_b_account_id"]:
        return "black"
    return None


# ── auth ─────────────────────────────────────────────────────────────────
# There is deliberately no "connect" endpoint that takes an address and
# creates an account from it. Accounts are created by /api/auth/verify and
# nowhere else, because the stored pubkey is what escrows are built from: an
# unproven one would let a stranger install their own key on someone else's
# account before that player ever showed up.
class NonceBody(BaseModel):
    address: str


@app.post("/api/auth/nonce")
def auth_nonce(body: NonceBody):
    """Hand out a single-use login challenge and the exact text to sign."""
    try:
        return auth.issue_nonce(body.address)
    except auth.AuthError as e:
        raise HTTPException(400, str(e))


class VerifyBody(BaseModel):
    address: str
    pubkey: str
    nonce: str
    signature: str


@app.post("/api/auth/verify")
async def auth_verify(body: VerifyBody):
    try:
        r = await auth.verify(address=body.address, pubkey=body.pubkey,
                              nonce=body.nonce, signature=body.signature)
    except auth.AuthError as e:
        raise HTTPException(401, str(e))
    except ServiceError as e:
        raise HTTPException(503, f"Kaspa service unavailable: {e}")
    return {**r["session"], "account": _account_public(r["account"])}


@app.post("/api/auth/logout")
def auth_logout(authorization: str | None = Header(default=None)):
    return {"ok": auth.logout(_token(authorization))}


# ── profile ──────────────────────────────────────────────────────────────
@app.get("/api/profile")
def get_profile(account: dict = Depends(require_account)):
    unlocked = db.unlocked_levels(account["id"])
    levels = [{**lv, "unlocked": lv["gas_kas"] == 0 or lv["id"] in unlocked}
              for lv in config.LEARN_LEVELS]
    return {**_account_public(account), "learnTiers": config.LEARN_TIERS, "learnLevels": levels}


class AcceptTogglBody(BaseModel):
    enabled: bool


@app.post("/api/profile/accept-challenges")
def set_accept_challenges(body: AcceptTogglBody, account: dict = Depends(require_account)):
    db.set_accept_challenges(account["id"], body.enabled)
    return {"ok": True}


class TelegramLinkBody(BaseModel):
    code: str


@app.post("/api/telegram/link")
async def telegram_link(body: TelegramLinkBody, account: dict = Depends(require_account)):
    """Finish the link the bot's /start began: the player pastes the one-time
    code here (authenticated as themselves — the account comes from the session,
    never the body), and we hand it to the bot to bind that Telegram to this
    account. The site never sees the Telegram id; the bot never sees a wallet.

    Bounded to a plausible code so a flood of junk can't be relayed to the bot,
    and every non-success reason from the bot is surfaced as a distinct message
    rather than a generic failure — a player whose bot simply isn't running in
    this deployment should be told that, not "invalid code"."""
    code = body.code.strip().upper()
    if not (4 <= len(code) <= 32):
        raise HTTPException(400, "that doesn't look like a link code — send /start to the bot to get one")
    result = await bot_client.claim_link(code, account["id"])
    if result.get("linked"):
        return {"ok": True}
    reason = result.get("reason", "invalid_or_expired")
    msg = {
        "not_configured": "Telegram alerts aren't set up on this server.",
        "unreachable": "Couldn't reach the alerts bot — try again in a moment.",
        "bot_error": "The alerts bot rejected the request — try again in a moment.",
        "invalid_or_expired": "That code is invalid or has expired — send /start to the bot for a new one.",
    }.get(reason, "That code is invalid or has expired — send /start to the bot for a new one.")
    # 502 for a bot-side/transport problem the player can't fix by retyping;
    # 400 for a bad code, which they can.
    status = 502 if reason in ("unreachable", "bot_error") else 400
    raise HTTPException(status, msg)


# ── challenges ───────────────────────────────────────────────────────────
class NewChallengeBody(BaseModel):
    toAddress: str | None = None
    stakeKas: float = 0
    mode: str = "rapid"
    gasOnly: bool = False


@app.post("/api/challenges")
def new_challenge(body: NewChallengeBody, account: dict = Depends(require_account)):
    if body.mode not in ("rapid", "daily"):
        raise HTTPException(400, "mode must be 'rapid' or 'daily'")
    if body.gasOnly:
        stake_sompi = config.GAS_ONLY_STAKE_SOMPI
    else:
        # stakeKas is a float off the wire: NaN/inf survive JSON and would sail
        # past a `<= 0` check (NaN comparisons are all false), minting an escrow
        # for a garbage amount. Reject anything that isn't a finite number first,
        # then clamp to the configured band so a fat-fingered stake bounces at
        # the form rather than on-chain.
        if not math.isfinite(body.stakeKas):
            raise HTTPException(400, "stake must be a real number")
        stake_sompi = round(body.stakeKas * config.SOMPI_PER_KAS)
        if stake_sompi < config.MIN_STAKE_SOMPI:
            raise HTTPException(400, f"minimum stake is {config.MIN_STAKE_SOMPI / config.SOMPI_PER_KAS:g} KAS "
                                      "(or tick gas-only)")
        if stake_sompi > config.MAX_STAKE_SOMPI:
            raise HTTPException(400, f"maximum stake is {config.MAX_STAKE_SOMPI / config.SOMPI_PER_KAS:g} KAS")
    to_id = None
    if body.toAddress:
        to = db.get_account_by_address(body.toAddress)
        # A named challenge to an address that has never played must NOT quietly
        # become an open challenge to the whole board — the creator picked a
        # specific opponent and would never see it went public. Bounce it.
        if not to:
            raise HTTPException(400, "that address hasn't played DAGmate yet — "
                                      "leave the opponent blank for an open challenge")
        if to["id"] == account["id"]:
            raise HTTPException(400, "you can't challenge yourself")
        if not to["accept_challenges"]:
            raise HTTPException(400, "that player isn't accepting challenges right now")
        to_id = to["id"]
    c = db.create_challenge(account["id"], to_id, stake_sompi, body.mode, body.gasOnly)
    return _challenge_public(c)


@app.get("/api/challenges")
def list_challenges(account: dict | None = Depends(optional_account)):
    return [_challenge_public(c) for c in db.list_open_challenges(account["id"] if account else None)]


@app.post("/api/challenges/{challenge_id}/accept")
async def accept_challenge(challenge_id: str, accepter: dict = Depends(require_account)):
    ch = db.get_challenge(challenge_id)
    if not ch or ch["status"] != "open":
        raise HTTPException(404, "challenge not found or no longer open")
    if ch["to_account_id"] and ch["to_account_id"] != accepter["id"]:
        raise HTTPException(403, "this challenge isn't addressed to you")
    if accepter["id"] == ch["from_account_id"]:
        raise HTTPException(400, "you can't accept your own challenge")

    creator = db.get_account(ch["from_account_id"])
    pk_a, pk_b = creator["pubkey"], accepter["pubkey"]
    if not pk_a or not pk_b:
        raise HTTPException(400, "both players need a connected wallet pubkey to build the escrow "
                                  "(use a real wallet, or a demo wallet for local testing)")

    # Claim the challenge before building anything: only the caller that wins
    # this atomic open->accepting transition proceeds, so a double-submit (or two
    # racing acceptors) can't turn one stake into two escrows / two matches.
    if not db.claim_challenge_for_accept(challenge_id):
        raise HTTPException(409, "challenge was just accepted or withdrawn")

    try:
        match = await _create_match_from_pair(
            challenge_id=challenge_id, tournament_id=None, round_no=None,
            player_a_id=creator["id"], player_b_id=accepter["id"],
            pk_a=pk_a, pk_b=pk_b, stake_sompi=ch["stake_sompi"], mode=ch["mode"])
    except ServiceError as e:
        # No escrow was built and no match survives (see _create_match_from_pair)
        # — hand the challenge back to 'open' so it can be accepted again once the
        # node is back, rather than being stranded 'accepting' with no match.
        db.release_challenge_to_open(challenge_id)
        raise HTTPException(503, f"Kaspa service unavailable, try again: {e}")
    except Exception:
        # Any other failure between the claim and the status flip would otherwise
        # leave the challenge stuck in the transient 'accepting' state —
        # invisible to the board, un-acceptable, un-declinable. Release it and
        # re-raise; no escrow was funded, so nothing is at risk.
        db.release_challenge_to_open(challenge_id)
        raise
    db.set_challenge_status(challenge_id, "accepted")

    stake_kas = ch["stake_sompi"] / config.SOMPI_PER_KAS
    await bot_client.notify_challenge(creator["id"], _short(accepter["address"]), stake_kas, ch["mode"],
                                       f"/play/{match['id']}")
    return _match_public(match)


@app.post("/api/challenges/{challenge_id}/decline")
def decline_challenge(challenge_id: str, account: dict = Depends(require_account)):
    """Only the two people it concerns can kill a challenge — the player it
    was sent to (declining) or the one who sent it (withdrawing). Without that
    check any passer-by could cancel every open challenge on the board."""
    ch = db.get_challenge(challenge_id)
    if not ch:
        raise HTTPException(404, "not found")
    if account["id"] not in (ch["from_account_id"], ch["to_account_id"]):
        raise HTTPException(403, "that challenge isn't yours to decline")
    if not db.decline_challenge_if_open(challenge_id):
        raise HTTPException(409, "challenge is already being accepted or is no longer open")
    return {"ok": True}


async def _create_match_from_pair(*, challenge_id, tournament_id, round_no, player_a_id, player_b_id,
                                   pk_a, pk_b, stake_sompi, mode) -> dict:
    # Raises ServiceError if the node is unreachable — no match row is created,
    # so we never build an escrow with an unsafe (already-open) timelock.
    reclaim_daa = await _reclaim_daa()
    match = db.create_match(
        challenge_id=challenge_id, tournament_id=tournament_id, round_no=round_no,
        player_a_account_id=player_a_id, player_b_account_id=player_b_id,
        stake_sompi=stake_sompi, mode=mode, fen=chess_logic.STARTING_FEN,
        escrow_a=None, escrow_b=None, reclaim_daa=reclaim_daa)
    try:
        escrow_a = await service_client.build_escrow(
            match_id=match["hd_index"], pk_a=pk_a, pk_b=pk_b, depositor_is_a=True, reclaim_daa=reclaim_daa)
        escrow_b = await service_client.build_escrow(
            match_id=match["hd_index"], pk_a=pk_a, pk_b=pk_b, depositor_is_a=False, reclaim_daa=reclaim_daa)
        db.set_match_escrows(match["id"], escrow_a, escrow_b)
        return db.get_match(match["id"])
    except ServiceError:
        # Roll the just-created match back rather than leave a zombie with NULL
        # escrows that a challenge already points at as "accepted" and that no
        # one can ever fund. The hd_index had to exist first (the arbiter key is
        # derived from it), so the row could only be created before this build.
        db.delete_match(match["id"])
        raise


# ── matches ──────────────────────────────────────────────────────────────
@app.get("/api/matches")
def list_matches(account: dict = Depends(require_account)):
    return [_match_public(m) for m in db.list_matches_for_account(account["id"])]


@app.get("/api/matches/{match_id}")
def get_match(match_id: str):
    m = db.get_match(match_id)
    if not m:
        raise HTTPException(404, "match not found")
    out = _match_public(m)
    out["legalMoves"] = chess_logic.legal_uci_moves(m["fen"]) if m["status"] == "live" else []
    return out


@app.post("/api/matches/{match_id}/dev-mark-funded")
def dev_mark_funded(match_id: str):
    """Flips a match from awaiting_deposit to live with no on-chain check —
    i.e. it conjures a pot out of nothing. The single most dangerous route in
    the project, and it exists only so the site can be clicked through without
    a funded node.

    ⚠️ It takes no account and never should get one: gating it on "are you a
    player in this match" would make it look safe enough to leave on. It is
    only ever acceptable when DEV_ROUTES is on, which is off by default and
    impossible on mainnet.

    Goes through the same guarded transition as the deposit watcher so the
    clock still starts — a live match with no running clock is precisely the
    abandonment hole clocks exist to close."""
    _require_dev_routes()
    m = db.get_match(match_id)
    if not m:
        raise HTTPException(404, "match not found")
    initial_ms, increment_ms = clocks.settings_for(m["mode"])
    db.mark_match_live(match_id, initial_ms=initial_ms, increment_ms=increment_ms,
                       now_ms=clocks.now_ms())
    return {"ok": True}


class MoveBody(BaseModel):
    uci: str


async def _anchor_move(match_id: str, ply: int, uci: str):
    """Write one ply to L1 via the sidecar, swallowing every failure. The
    payload is a compact, self-describing record — `DGMT` magic, the match id,
    the ply number, and the move in UCI — so the tx is legible on-chain as a
    DAGmate move and not just opaque bytes. feeSompi is left at 0: the anchor is
    a dust carrier, not a payment, and the sidecar adds only the network mass
    fee. Never raises: called after the move is already committed."""
    payload = f"DGMT|{match_id}|{ply}|{uci}".encode("utf-8").hex()
    try:
        await service_client.anchor(match_id=match_id, ply=ply, payload_hex=payload)
    except ServiceError as e:
        log.warning(f"move anchor failed (non-fatal) match={match_id} ply={ply}: {e}")


@app.post("/api/matches/{match_id}/move")
async def make_move(match_id: str, body: MoveBody, a: dict = Depends(require_account)):
    m = db.get_match(match_id)
    if not m or m["status"] != "live":
        raise HTTPException(400, "match isn't live")
    is_a = a["id"] == m["player_a_account_id"]
    is_b = a["id"] == m["player_b_account_id"]
    if not (is_a or is_b):
        raise HTTPException(403, "you're not a player in this match")
    my_color = "white" if is_a else "black"  # player A is always white, player B always black
    if m["turn"] != my_color:
        raise HTTPException(400, "not your move")

    # One timestamp for the whole request: the flag check and the time charged
    # must agree, or a move made right on the boundary could be both accepted
    # and forfeited.
    at_ms = clocks.now_ms()
    if await clocks.forfeit_if_flagged(m, at_ms):
        raise HTTPException(400, "your clock ran out")

    import json
    prior_moves = json.loads(m["moves_json"])
    try:
        # Pass the full move history so repetition draws (threefold/fivefold) are
        # actually detected — rebuilding from the FEN alone can't see them.
        status = chess_logic.apply_uci(m["fen"], body.uci, history=prior_moves)
    except ValueError as e:
        raise HTTPException(400, str(e))

    moves = prior_moves + [body.uci]
    if not db.apply_move_with_clock(
            match_id, status["fen"], moves, status["turn"], mover_color=my_color,
            mover_remaining_ms=clocks.charge_move(m, my_color, at_ms), now_ms=at_ms):
        # The guarded UPDATE didn't match, so the position moved under us —
        # a duplicate submission, or the clock loop settled the match first.
        raise HTTPException(409, "the match moved on — reload the board")

    opponent_id = m["player_b_account_id"] if is_a else m["player_a_account_id"]
    await bot_client.notify_your_move(opponent_id, match_id, f"/play/{match_id}")

    # Anchor the ply on L1 if the operator has turned it on. Best-effort by
    # design: the move is already committed and the clock already charged, so an
    # anchor failure (node down, operating address unfunded) must never undo a
    # legal move or hand the player a 500 — it's the same "nobody to tell right
    # now" posture as the alerts. The ply number is len(moves) after appending.
    if config.ANCHOR_MOVES:
        await _anchor_move(match_id, len(moves), body.uci)

    if status["game_over"]:
        await _settle_game_over(match_id, status["result"], status["winner_color"])

    m = db.get_match(match_id)
    out = _match_public(m)
    out["legalMoves"] = chess_logic.legal_uci_moves(m["fen"]) if m["status"] == "live" else []
    out["inCheck"] = status["in_check"]
    return out


@app.post("/api/matches/{match_id}/resign")
async def resign(match_id: str, a: dict = Depends(require_account)):
    """The single most abusable endpoint on the site — resigning hands the
    pot to the other player, so it MUST be the resigning player who called it.
    That's why identity comes from a signed session and not from the body."""
    m = db.get_match(match_id)
    if not m or m["status"] != "live":
        raise HTTPException(400, "match isn't live")
    is_a = a["id"] == m["player_a_account_id"]
    is_b = a["id"] == m["player_b_account_id"]
    if not (is_a or is_b):
        raise HTTPException(403, "you're not a player in this match")
    winner_color = "black" if is_a else "white"
    await _settle_game_over(match_id, "resign", winner_color)
    return _match_public(db.get_match(match_id))


# ── draw offers ──────────────────────────────────────────────────────────
# Agreeing a draw splits the pot, so it is a money decision — and it is the
# only ending that needs BOTH players to say yes. Every route here takes its
# player from the session for the same reason /resign does: an address is
# printed on the match view, so a body-supplied one would let a stranger
# accept a draw in a game someone else was winning.
#
# The rules that make it safe live in database.py, not here, because they must
# be enforced by the same UPDATE that acts on them:
#   - you cannot accept your own offer      (draw_offer_by <> accepter)
#   - accepting settles in one statement    (no read-then-write window)
#   - playing on withdraws your offer       (cleared by apply_move_with_clock)
#   - one offer per position                (draw_offer_ply survives a decline)
def _my_live_match(match_id: str, account: dict) -> dict:
    m = db.get_match(match_id)
    if not m or m["status"] != "live":
        raise HTTPException(400, "match isn't live")
    if account["id"] not in (m["player_a_account_id"], m["player_b_account_id"]):
        raise HTTPException(403, "you're not a player in this match")
    return m


def _opponent_of(m: dict, account: dict) -> str:
    return (m["player_b_account_id"] if account["id"] == m["player_a_account_id"]
            else m["player_a_account_id"])


@app.post("/api/matches/{match_id}/draw/offer")
async def draw_offer(match_id: str, account: dict = Depends(require_account)):
    import json
    m = _my_live_match(match_id, account)
    ply = len(json.loads(m["moves_json"]))
    if not db.offer_draw(match_id, account["id"], ply):
        if m["draw_offer_by"] and m["draw_offer_by"] != account["id"]:
            raise HTTPException(400, "your opponent has already offered a draw — accept or decline it")
        if m["draw_offer_by"] == account["id"]:
            raise HTTPException(400, "your draw offer is already on the board")
        raise HTTPException(400, "you've already offered a draw in this position — play a move first")
    await bot_client.notify_draw_offer(_opponent_of(m, account), match_id, f"/play/{match_id}")
    return _match_public(db.get_match(match_id))


@app.post("/api/matches/{match_id}/draw/accept")
async def draw_accept(match_id: str, account: dict = Depends(require_account)):
    """Take the standing offer. Ends the match as a draw and splits the pot —
    which is why the offer's existence, and the fact that it wasn't this
    player's, are WHERE clauses on the settlement rather than checks up here."""
    m = _my_live_match(match_id, account)
    if not db.accept_draw_if_offered(match_id, account["id"]):
        if m["draw_offer_by"] == account["id"]:
            raise HTTPException(400, "that's your own offer — your opponent has to accept it")
        raise HTTPException(400, "no draw offer to accept")
    for pid in (m["player_a_account_id"], m["player_b_account_id"]):
        await bot_client.notify_settled(pid, match_id, "draw agreed — each stake goes back to its owner")
    return _match_public(db.get_match(match_id))


@app.post("/api/matches/{match_id}/draw/decline")
def draw_decline(match_id: str, account: dict = Depends(require_account)):
    """Decline the opponent's offer, or withdraw your own — the same clear
    either way. Both players may call it: refusing a draw and thinking better
    of having offered one are the same state change."""
    _my_live_match(match_id, account)
    db.clear_draw_offer(match_id)
    return _match_public(db.get_match(match_id))


async def _settle_game_over(match_id: str, result: str, winner_color: str | None):
    """Records the result. Money moves separately, through the settle
    endpoints below: releasing the pot needs a player signature, so it can't
    happen inside the request that ended the game — the winner might not even
    be the one who sent it (a resignation ends the game from the loser's
    browser).

    Guarded, because the clock loop can be deciding the same match at the same
    moment: whoever gets there first ends it, and the loser of that race stays
    quiet rather than overwriting the result or sending a second DM."""
    m = db.get_match(match_id)
    winner_id = None
    if winner_color == "white":
        winner_id = m["player_a_account_id"]
    elif winner_color == "black":
        winner_id = m["player_b_account_id"]
    if not db.settle_match_if_live(match_id, result=result, winner_account_id=winner_id):
        return
    summary = f"{result}" + (" — you won" if winner_id else " — draw")
    for pid in (m["player_a_account_id"], m["player_b_account_id"]):
        await bot_client.notify_settled(pid, match_id, summary)


# ── settlement ───────────────────────────────────────────────────────────
@app.post("/api/matches/{match_id}/settle/prepare")
async def settle_prepare(match_id: str, account: dict = Depends(require_account)):
    """Build (once) the tx that releases the pot, and say what this player
    still has to sign. Both players may call this — on a draw they both must."""
    try:
        return await settlement.prepare(match_id, account["address"])
    except settlement.SettlementError as e:
        raise HTTPException(400, str(e))
    except ServiceError as e:
        raise HTTPException(503, f"Kaspa service unavailable: {e}")


class SettleSubmitBody(BaseModel):
    # {input index: signature}. JSON object keys are strings, so this is typed
    # as such and converted below rather than silently dropping entries.
    sigs: dict[str, str]


@app.post("/api/matches/{match_id}/settle/submit")
async def settle_submit(match_id: str, body: SettleSubmitBody,
                        account: dict = Depends(require_account)):
    try:
        sigs = {int(k): v for k, v in body.sigs.items()}
    except ValueError:
        raise HTTPException(400, "signature keys must be input indexes")
    if not sigs:
        raise HTTPException(400, "no signatures supplied")
    try:
        return await settlement.submit(match_id, account["address"], sigs)
    except settlement.SettlementError as e:
        raise HTTPException(400, str(e))
    except ServiceError as e:
        raise HTTPException(503, f"Kaspa service unavailable: {e}")


# ── reclaim ──────────────────────────────────────────────────────────────
# The way out when a match dies with money in it. Both endpoints take the
# escrow from the session, never the body — see reclaim.py, property 3.
@app.post("/api/matches/{match_id}/reclaim/prepare")
async def reclaim_prepare(match_id: str, account: dict = Depends(require_account)):
    try:
        return await reclaim.prepare(match_id, account["address"])
    except reclaim.ReclaimError as e:
        raise HTTPException(400, str(e))
    except ServiceError as e:
        raise HTTPException(503, f"Kaspa service unavailable: {e}")


class ReclaimSubmitBody(BaseModel):
    # The tx the wallet signed, handed straight back. Nothing was stored
    # server-side to compare it against, and nothing needs to be: the
    # signatures only validate against the exact tx they were made over.
    txJson: str
    sigs: list[str]


@app.post("/api/matches/{match_id}/reclaim/submit")
async def reclaim_submit(match_id: str, body: ReclaimSubmitBody,
                         account: dict = Depends(require_account)):
    try:
        return await reclaim.submit(match_id, account["address"], body.txJson, body.sigs)
    except reclaim.ReclaimError as e:
        raise HTTPException(400, str(e))
    except ServiceError as e:
        raise HTTPException(503, f"Kaspa service unavailable: {e}")


# ── tournaments ──────────────────────────────────────────────────────────
@app.get("/api/tournaments")
def list_tournaments():
    out = []
    for tier in config.TOURNAMENT_TIERS_KAS:
        t = db.get_or_create_open_tournament_readonly(tier)
        count = db.count_entrants(t["id"]) if t else 0
        out.append({
            "tierKas": tier, "tournamentId": t["id"] if t else None,
            "entrants": count, "minEntrants": config.TOURNAMENT_MIN_ENTRANTS,
            "status": t["status"] if t else "open",
        })
    return out


@app.post("/api/tournaments/{tier_kas}/join")
async def join_tournament(tier_kas: int, a: dict = Depends(require_account)):
    if tier_kas not in config.TOURNAMENT_TIERS_KAS:
        raise HTTPException(400, "unknown tier")
    if not a["pubkey"]:
        raise HTTPException(400, "connect a wallet with a pubkey before joining a tournament")
    t = db.get_or_create_open_tournament(tier_kas)
    joined = db.join_tournament(t["id"], a["id"])
    count = db.count_entrants(t["id"])
    started = False
    if joined and count >= config.TOURNAMENT_MIN_ENTRANTS:
        started = await _start_tournament(t["id"])
    return {"ok": True, "alreadyJoined": not joined, "entrants": count,
            "minEntrants": config.TOURNAMENT_MIN_ENTRANTS, "started": started}


async def _start_tournament(tournament_id: str) -> bool:
    entrants = db.list_entrants(tournament_id)
    accounts = [db.get_account(e["account_id"]) for e in entrants]
    accounts = [a for a in accounts if a and a["pubkey"]]
    if len(accounts) < config.TOURNAMENT_MIN_ENTRANTS:
        return False
    # Win the open->running transition before building any brackets. Two
    # entrants filling the last seats at once could both reach here; only the
    # caller that claims the start proceeds, so the bracket (and its escrows) is
    # built exactly once.
    if not db.claim_tournament_start(tournament_id):
        return False
    random.shuffle(accounts)
    t = db.get_tournament(tournament_id)
    stake_sompi = t["tier_kas"] * config.SOMPI_PER_KAS
    for i in range(0, len(accounts) - 1, 2):
        p_a, p_b = accounts[i], accounts[i + 1]
        try:
            await _create_match_from_pair(
                challenge_id=None, tournament_id=tournament_id, round_no=1,
                player_a_id=p_a["id"], player_b_id=p_b["id"],
                pk_a=p_a["pubkey"], pk_b=p_b["pubkey"], stake_sompi=stake_sompi, mode="rapid")
        except Exception as e:
            log.error(f"failed to build round-1 match for tournament {tournament_id}: {e}")
    # An odd entrant count leaves one player unpaired. They never funded
    # anything (no escrow is built for a player with no pairing), so no money is
    # at risk — but they'd otherwise sit in a started tournament with no match
    # and no explanation. Tell them so it isn't a silent dead end. Best-effort,
    # like every other alert. (min entrants defaults to an even 8, so this is an
    # edge case tied to an odd config, not the normal path.)
    if len(accounts) % 2 == 1:
        odd = accounts[-1]
        log.info(f"tournament {tournament_id}: odd entrant {odd['id']} left unpaired this run")
        await bot_client.notify_settled(
            odd["id"], tournament_id,
            "You weren't paired this round — the lobby had an odd number of players. "
            "Your stake was never taken; join the next lobby to play.")
    return True


# ── learn ────────────────────────────────────────────────────────────────
@app.post("/api/learn/levels/{level_id}/unlock")
def unlock_level(level_id: str, account: dict = Depends(require_account)):
    level = next((lv for lv in config.LEARN_LEVELS if lv["id"] == level_id), None)
    if not level:
        raise HTTPException(404, "unknown level")
    # A paid level cannot be unlocked while payment enforcement is on, because
    # the on-chain gas-payment check isn't wired yet — better to refuse than to
    # hand a "paid" level out for free under a price tag. With enforcement off
    # (the default), levels unlock free and the UI is told to present them as
    # free (see /api/meta learnRequiresPayment + config.LEARN_REQUIRE_PAYMENT).
    if config.LEARN_REQUIRE_PAYMENT and curriculum.gas_for(level_id) > 0:
        raise HTTPException(402, "paid levels aren't available yet — check back soon")
    db.unlock_level(account["id"], level_id)
    return {"ok": True}


@app.get("/api/learn/levels")
def learn_levels():
    """Catalogue only — bodies never appear here (see curriculum.level_index)."""
    return {"tiers": config.LEARN_TIERS, "levels": config.LEARN_LEVELS}


@app.get("/api/learn/levels/{level_id}/content")
def learn_level_content(level_id: str, account: dict = Depends(require_account)):
    """The paywall. Level bodies live server-side and only leave through here,
    so a locked level is genuinely unreadable rather than just hidden in the UI.

    Session-scoped, not address-scoped: when this took an `address` query
    param, reading a paid level was a matter of naming anyone who'd bought it."""
    unlocked = curriculum.gas_for(level_id) == 0 or level_id in db.unlocked_levels(account["id"])
    body = curriculum.content_for(level_id, unlocked)
    if body is None:
        if level_id not in curriculum.LEVELS_BY_ID:
            raise HTTPException(404, "unknown level")
        raise HTTPException(403, "level locked")
    lv = curriculum.LEVELS_BY_ID[level_id]
    return {"id": level_id, "title": lv["title"], "body": body}


class PracticeMoveBody(BaseModel):
    fen: str
    uci: str


@app.post("/api/practice/apply-move")
def practice_apply_move(body: PracticeMoveBody):
    try:
        return chess_logic.apply_uci(body.fen, body.uci)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/practice/legal-moves")
def practice_legal_moves(fen: str):
    # A hand-supplied FEN can be malformed; python-chess raises ValueError.
    # Answer 400 rather than letting it become an unauthenticated 500 (matches
    # how /api/practice/apply-move already handles it).
    try:
        return {"legalMoves": chess_logic.legal_uci_moves(fen)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/practice/start-fen")
def practice_start_fen():
    return {"fen": chess_logic.STARTING_FEN}


class PracticeBotBody(BaseModel):
    fen: str
    level: str | None = None


@app.get("/api/practice/levels")
def practice_levels():
    return {"levels": engine.level_list(), "default": engine.DEFAULT_LEVEL}


# Ceiling on concurrent engine searches (see config.PRACTICE_MAX_CONCURRENCY).
# A plain counting semaphore acquired non-blocking: the CPU-bound search runs in
# the threadpool, so blocking on the semaphore would just tie up a pool thread —
# instead an over-cap request returns 429 immediately and frees the thread.
_practice_engine_slots = threading.Semaphore(config.PRACTICE_MAX_CONCURRENCY)


@app.post("/api/practice/bot-move")
def practice_bot_move(body: PracticeBotBody, account: dict = Depends(require_account)):
    # Sync `def` on purpose: FastAPI runs these in a threadpool, so the engine's
    # wall-clock budget can't stall the event loop for every other request.
    # Requires a session (the practice board is behind a wallet connect) and is
    # concurrency-capped so a burst can't starve the deposit/clock watchers that
    # share this process — a stalled deposit watcher is a money problem, not a
    # slow page.
    if not _practice_engine_slots.acquire(blocking=False):
        raise HTTPException(429, "practice engine busy — try again in a moment")
    try:
        # A malformed FEN is a 400, not a 500 — the search and the status/move
        # helpers all raise ValueError on a bad position.
        try:
            uci = engine.best_move(body.fen, body.level)
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        _practice_engine_slots.release()
    if not uci:
        return {"uci": None, "status": chess_logic.status_of(chess_logic.board_from(body.fen))}
    status = chess_logic.apply_uci(body.fen, uci)
    return {"uci": uci, "level": engine.resolve_level(body.level).key, "status": status}


# ── dev-only demo wallet ────────────────────────────────────────────────
# The demo wallet deliberately signs its way in through the SAME nonce →
# signature → verify handshake a real wallet uses, rather than being handed a
# session directly. A login path that only ever runs in production is a login
# path nobody has tested, and this is the one that guards the pot.
@app.post("/api/dev/demo-wallet")
async def demo_wallet():
    _require_dev_routes()
    try:
        kp = await service_client.generate_demo_keypair()
    except ServiceError as e:
        raise HTTPException(502, f"couldn't reach the Kaspa service: {e}")
    return kp


class DemoSignBody(BaseModel):
    privateKeyHex: str
    message: str


@app.post("/api/dev/demo-sign")
async def demo_sign(body: DemoSignBody):
    """Sign a login challenge with a demo-wallet key. Harmless by
    construction — it can only sign with a key the caller already holds — but
    it still disappears with the rest of the dev surface."""
    _require_dev_routes()
    try:
        sig = await service_client.demo_sign_message(
            private_key_hex=body.privateKeyHex, message=body.message)
    except ServiceError as e:
        raise HTTPException(502, f"couldn't reach the Kaspa service: {e}")
    return {"signature": sig}


# ── site meta ────────────────────────────────────────────────────────────
@app.get("/api/meta")
def meta():
    """Facts the frontend states out loud — chiefly the fee policy, which is a
    promise to players and so is DERIVED from the same config the settle path
    charges from. If the rake is ever switched on, the disclaimer changes by
    itself instead of becoming a lie nobody remembered to update."""
    return {
        "fees": {
            "platformFeeBps": config.RAKE_BPS,
            "takesCut": config.RAKE_BPS > 0,
            "networkFeeKasPerInput": config.SETTLE_FEE_SOMPI_PER_INPUT / config.SOMPI_PER_KAS,
        },
        "gasOnlyKas": config.GAS_ONLY_STAKE_SOMPI / config.SOMPI_PER_KAS,
        # Stake bounds so the form can cap the input and confirm large stakes,
        # matching what the backend enforces (a fat-fingered amount should bounce
        # at the field, not mint an escrow). The backend is still the authority.
        "minStakeKas": config.MIN_STAKE_SOMPI / config.SOMPI_PER_KAS,
        "maxStakeKas": config.MAX_STAKE_SOMPI / config.SOMPI_PER_KAS,
        # Whether moves are actually being written to L1 right now. The frontend
        # copy about on-chain anchoring is driven by this so it can never claim a
        # feature the operator hasn't switched on (see config.ANCHOR_MOVES).
        "anchorsMoves": config.ANCHOR_MOVES,
        # The bot's public handle, so the (optional) alerts settings can show a
        # one-tap link to it. Empty when no bot is configured — the site never
        # requires Telegram to play.
        "botUsername": config.BOT_USERNAME,
        # Whether paid learn levels actually charge. Off (default) means levels
        # unlock free and the UI shows "free" instead of a price the server
        # doesn't collect (see config.LEARN_REQUIRE_PAYMENT).
        "learnRequiresPayment": config.LEARN_REQUIRE_PAYMENT,
        "reclaimDays": config.RECLAIM_DAA_WINDOW // (24 * 3600 * 10),
        # Same reasoning as the fee copy: the UI asks what's available rather
        # than keeping its own guess about it. The demo-wallet fallback and the
        # "mark funded" button vanish on their own in a real deployment.
        "devRoutes": config.DEV_ROUTES,
        "network": config.NETWORK_ID,
    }


# ── health + static frontend ────────────────────────────────────────────
@app.get("/api/health")
async def health():
    service_ok = True
    try:
        await service_client.daa_score()
    except ServiceError:
        service_ok = False
    return {"ok": True, "service_ok": service_ok}


FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")
