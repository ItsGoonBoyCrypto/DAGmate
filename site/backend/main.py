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
import os
import random

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


async def _reclaim_daa() -> tuple[int, str]:
    """Best-effort live DAA score + the standard 14-day CLTV window. Falls
    back to a clearly-fake placeholder if the sidecar/node is unreachable —
    good enough to demo the escrow-building UI locally, NOT good enough for
    a real deposit (see module docstring)."""
    try:
        current = await service_client.daa_score()
        return current + config.RECLAIM_DAA_WINDOW, "live"
    except ServiceError as e:
        log.warning(f"DAA lookup failed, using placeholder reclaim window: {e}")
        return config.RECLAIM_DAA_WINDOW, "fallback"


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
        "clock": clocks.public(m),
    }


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
    stake_sompi = config.GAS_ONLY_STAKE_SOMPI if body.gasOnly else round(body.stakeKas * config.SOMPI_PER_KAS)
    if stake_sompi <= 0:
        raise HTTPException(400, "stake must be above zero (or tick gas-only)")
    to_id = None
    if body.toAddress:
        to = db.get_account_by_address(body.toAddress)
        if to:
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

    match = await _create_match_from_pair(
        challenge_id=challenge_id, tournament_id=None, round_no=None,
        player_a_id=creator["id"], player_b_id=accepter["id"],
        pk_a=pk_a, pk_b=pk_b, stake_sompi=ch["stake_sompi"], mode=ch["mode"])
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
    db.set_challenge_status(challenge_id, "declined")
    return {"ok": True}


async def _create_match_from_pair(*, challenge_id, tournament_id, round_no, player_a_id, player_b_id,
                                   pk_a, pk_b, stake_sompi, mode) -> dict:
    reclaim_daa, daa_source = await _reclaim_daa()
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
        match = db.get_match(match["id"])
    except ServiceError as e:
        log.warning(f"escrow build failed for match {match['id']} (service unreachable?): {e}")
    match["_daa_source"] = daa_source
    return match


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

    try:
        status = chess_logic.apply_uci(m["fen"], body.uci)
    except ValueError as e:
        raise HTTPException(400, str(e))

    import json
    moves = json.loads(m["moves_json"]) + [body.uci]
    if not db.apply_move_with_clock(
            match_id, status["fen"], moves, status["turn"], mover_color=my_color,
            mover_remaining_ms=clocks.charge_move(m, my_color, at_ms), now_ms=at_ms):
        # The guarded UPDATE didn't match, so the position moved under us —
        # a duplicate submission, or the clock loop settled the match first.
        raise HTTPException(409, "the match moved on — reload the board")

    opponent_id = m["player_b_account_id"] if is_a else m["player_a_account_id"]
    await bot_client.notify_your_move(opponent_id, match_id, f"/play/{match_id}")

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
    random.shuffle(accounts)
    t = db.get_tournament(tournament_id)
    stake_sompi = t["tier_kas"] * config.SOMPI_PER_KAS
    db.set_tournament_status(tournament_id, "running")
    for i in range(0, len(accounts) - 1, 2):
        p_a, p_b = accounts[i], accounts[i + 1]
        try:
            await _create_match_from_pair(
                challenge_id=None, tournament_id=tournament_id, round_no=1,
                player_a_id=p_a["id"], player_b_id=p_b["id"],
                pk_a=p_a["pubkey"], pk_b=p_b["pubkey"], stake_sompi=stake_sompi, mode="rapid")
        except Exception as e:
            log.error(f"failed to build round-1 match for tournament {tournament_id}: {e}")
    return True


# ── learn ────────────────────────────────────────────────────────────────
@app.post("/api/learn/levels/{level_id}/unlock")
def unlock_level(level_id: str, account: dict = Depends(require_account)):
    level = next((lv for lv in config.LEARN_LEVELS if lv["id"] == level_id), None)
    if not level:
        raise HTTPException(404, "unknown level")
    # Real flow: verify a matching on-chain gas payment to the operating
    # address before unlocking (see module docstring — not yet wired).
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
    return {"legalMoves": chess_logic.legal_uci_moves(fen)}


@app.get("/api/practice/start-fen")
def practice_start_fen():
    return {"fen": chess_logic.STARTING_FEN}


class PracticeBotBody(BaseModel):
    fen: str
    level: str | None = None


@app.get("/api/practice/levels")
def practice_levels():
    return {"levels": engine.level_list(), "default": engine.DEFAULT_LEVEL}


@app.post("/api/practice/bot-move")
def practice_bot_move(body: PracticeBotBody):
    # Sync `def` on purpose: FastAPI runs these in a threadpool, so the engine's
    # wall-clock budget can't stall the event loop for every other request.
    uci = engine.best_move(body.fen, body.level)
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
