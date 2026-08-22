"""DAGmate site backend (docs/DAGMATE_SPEC.md) — FastAPI app: wallet/profile,
challenges, matches (python-chess rules), tournaments, learn page. Talks to
service/ (Node, Kaspa sidecar) for escrow addresses and to bot/ (Telegram
alerts) best-effort. Serves the static frontend too, so `uvicorn main:app`
is the one process to run locally.

Known, deliberate gaps in this pass (see docs/DAGMATE_SPEC.md and the
project memory for the full picture) — flagged here rather than silently
faked:
  - `/api/matches/{id}/dev-mark-funded` still exists and skips the on-chain
    deposit check entirely. Real deposits are now watched (deposits.py), so
    this route is only for clicking through locally without a reachable Kaspa
    node — it MUST be off before any public deployment.
  - Game-over settlement records a DB winner but does NOT yet call
    service/escrow's settle-unsigned/settle-broadcast (that needs a real
    wallet-connect signature round-trip from the winner's browser extension,
    which isn't available in a headless preview).
  - Learn-level gas payments are recorded optimistically, not yet verified
    against a real on-chain payment.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import bot_client
import chess_logic
import clocks
import config
import curriculum
import database as db
import deposits
import engine
import kns
import service_client
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
    asyncio.create_task(clocks.watch_loop())


def _short(addr: str) -> str:
    return addr if len(addr) <= 14 else f"{addr[:8]}…{addr[-6:]}"


def _account_public(a: dict) -> dict:
    return {
        "id": a["id"], "address": a["address"], "shortAddress": _short(a["address"]),
        "knsName": kns.primary_name(a["address"]),
        "acceptChallenges": bool(a["accept_challenges"]), "isDemoWallet": bool(a["is_demo_wallet"]),
    }


def _get_account_or_404(address: str) -> dict:
    a = db.get_account_by_address(address)
    if not a:
        raise HTTPException(404, "no account for this address — connect a wallet first")
    return a


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


# ── wallet / profile ────────────────────────────────────────────────────
class ConnectBody(BaseModel):
    address: str
    pubkey: str | None = None


@app.post("/api/wallet/connect")
def wallet_connect(body: ConnectBody):
    a = db.get_or_create_account(body.address, body.pubkey)
    return _account_public(a)


@app.get("/api/profile")
def get_profile(address: str):
    a = _get_account_or_404(address)
    unlocked = db.unlocked_levels(a["id"])
    levels = [{**lv, "unlocked": lv["gas_kas"] == 0 or lv["id"] in unlocked}
              for lv in config.LEARN_LEVELS]
    return {**_account_public(a), "learnTiers": config.LEARN_TIERS, "learnLevels": levels}


class AcceptTogglBody(BaseModel):
    address: str
    enabled: bool


@app.post("/api/profile/accept-challenges")
def set_accept_challenges(body: AcceptTogglBody):
    a = _get_account_or_404(body.address)
    db.set_accept_challenges(a["id"], body.enabled)
    return {"ok": True}


# ── challenges ───────────────────────────────────────────────────────────
class NewChallengeBody(BaseModel):
    fromAddress: str
    toAddress: str | None = None
    stakeKas: float = 0
    mode: str = "rapid"
    gasOnly: bool = False


@app.post("/api/challenges")
def new_challenge(body: NewChallengeBody):
    if body.mode not in ("rapid", "daily"):
        raise HTTPException(400, "mode must be 'rapid' or 'daily'")
    frm = _get_account_or_404(body.fromAddress)
    to_id = None
    if body.toAddress:
        to = db.get_account_by_address(body.toAddress)
        if to:
            if not to["accept_challenges"]:
                raise HTTPException(400, "that player isn't accepting challenges right now")
            to_id = to["id"]
    stake_sompi = config.GAS_ONLY_STAKE_SOMPI if body.gasOnly else round(body.stakeKas * config.SOMPI_PER_KAS)
    c = db.create_challenge(frm["id"], to_id, stake_sompi, body.mode, body.gasOnly)
    return _challenge_public(c)


@app.get("/api/challenges")
def list_challenges(address: str | None = None):
    account_id = None
    if address:
        a = db.get_account_by_address(address)
        account_id = a["id"] if a else None
    return [_challenge_public(c) for c in db.list_open_challenges(account_id)]


class AcceptChallengeBody(BaseModel):
    address: str
    pubkey: str | None = None


@app.post("/api/challenges/{challenge_id}/accept")
async def accept_challenge(challenge_id: str, body: AcceptChallengeBody):
    ch = db.get_challenge(challenge_id)
    if not ch or ch["status"] != "open":
        raise HTTPException(404, "challenge not found or no longer open")
    accepter = db.get_or_create_account(body.address, body.pubkey)
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
def decline_challenge(challenge_id: str):
    ch = db.get_challenge(challenge_id)
    if not ch:
        raise HTTPException(404, "not found")
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
def list_matches(address: str):
    a = _get_account_or_404(address)
    return [_match_public(m) for m in db.list_matches_for_account(a["id"])]


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
    """Dev/testing convenience only — see module docstring. Flips a match
    from awaiting_deposit to live without any real on-chain check. Goes
    through the same guarded transition as the deposit watcher so the clock
    still starts: a live match with no running clock is precisely the
    abandonment hole clocks exist to close."""
    m = db.get_match(match_id)
    if not m:
        raise HTTPException(404, "match not found")
    initial_ms, increment_ms = clocks.settings_for(m["mode"])
    db.mark_match_live(match_id, initial_ms=initial_ms, increment_ms=increment_ms,
                       now_ms=clocks.now_ms())
    return {"ok": True}


class MoveBody(BaseModel):
    address: str
    uci: str


@app.post("/api/matches/{match_id}/move")
async def make_move(match_id: str, body: MoveBody):
    m = db.get_match(match_id)
    if not m or m["status"] != "live":
        raise HTTPException(400, "match isn't live")
    a = _get_account_or_404(body.address)
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


class ResignBody(BaseModel):
    address: str


@app.post("/api/matches/{match_id}/resign")
async def resign(match_id: str, body: ResignBody):
    m = db.get_match(match_id)
    if not m or m["status"] != "live":
        raise HTTPException(400, "match isn't live")
    a = _get_account_or_404(body.address)
    is_a = a["id"] == m["player_a_account_id"]
    is_b = a["id"] == m["player_b_account_id"]
    if not (is_a or is_b):
        raise HTTPException(403, "you're not a player in this match")
    winner_color = "black" if is_a else "white"
    await _settle_game_over(match_id, "resign", winner_color)
    return _match_public(db.get_match(match_id))


async def _settle_game_over(match_id: str, result: str, winner_color: str | None):
    """Records the result. Does NOT yet move real funds — see module
    docstring's settlement gap.

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


class JoinTournamentBody(BaseModel):
    address: str


@app.post("/api/tournaments/{tier_kas}/join")
async def join_tournament(tier_kas: int, body: JoinTournamentBody):
    if tier_kas not in config.TOURNAMENT_TIERS_KAS:
        raise HTTPException(400, "unknown tier")
    a = _get_account_or_404(body.address)
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
class UnlockLevelBody(BaseModel):
    address: str


@app.post("/api/learn/levels/{level_id}/unlock")
def unlock_level(level_id: str, body: UnlockLevelBody):
    level = next((lv for lv in config.LEARN_LEVELS if lv["id"] == level_id), None)
    if not level:
        raise HTTPException(404, "unknown level")
    a = _get_account_or_404(body.address)
    # Real flow: verify a matching on-chain gas payment to the operating
    # address before unlocking (see module docstring — not yet wired).
    db.unlock_level(a["id"], level_id)
    return {"ok": True}


@app.get("/api/learn/levels")
def learn_levels():
    """Catalogue only — bodies never appear here (see curriculum.level_index)."""
    return {"tiers": config.LEARN_TIERS, "levels": config.LEARN_LEVELS}


@app.get("/api/learn/levels/{level_id}/content")
def learn_level_content(level_id: str, address: str):
    """The paywall. Level bodies live server-side and only leave through here,
    so a locked level is genuinely unreadable rather than just hidden in the UI."""
    a = _get_account_or_404(address)
    unlocked = curriculum.gas_for(level_id) == 0 or level_id in db.unlocked_levels(a["id"])
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
@app.post("/api/dev/demo-wallet")
async def demo_wallet():
    if not config.DEMO_WALLET_ENABLED:
        raise HTTPException(404, "demo wallet disabled")
    try:
        kp = await service_client.generate_demo_keypair()
    except ServiceError as e:
        raise HTTPException(502, f"couldn't reach the Kaspa service: {e}")
    db.get_or_create_account(kp["address"], kp["pubkey"], is_demo=True)
    return kp


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
