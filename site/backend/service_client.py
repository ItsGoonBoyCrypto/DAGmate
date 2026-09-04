"""DAGmate site backend — client for service/ (the Kaspa sidecar, see
docs/DAGMATE_SPEC.md §3). Every call is best-effort: if the sidecar or its
configured Kaspa node is unreachable, callers get a clear ServiceError
instead of a stack trace, and the site keeps working for everything that
doesn't need live chain data (challenges, tournaments lobby, learn page,
the board itself once a match exists)."""
from __future__ import annotations

import httpx

import config


class ServiceError(RuntimeError):
    pass


def _url(path: str) -> str:
    return f"{config.SERVICE_URL}{path}"


def _error(path: str, r: httpx.Response) -> ServiceError:
    """The sidecar's own error message, or something useful if it didn't send
    one. It usually replies `{"error": ...}`, but not always: an unknown route
    gets Express's HTML 404 page, which json() chokes on. That is exactly what
    a version-skewed deploy looks like — a backend calling a route the sidecar
    it's talking to doesn't have — so it has to produce a readable message
    rather than a JSONDecodeError surfacing as a bare 500."""
    try:
        msg = r.json().get("error")
    except ValueError:
        msg = None
    if msg:
        return ServiceError(str(msg))
    if r.status_code == 404:
        return ServiceError(f"Kaspa service has no route {path} — the sidecar is "
                            f"older than this backend, or a dev-only route is switched off")
    return ServiceError(f"Kaspa service returned HTTP {r.status_code} for {path}")


async def _get(path: str, params: dict | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(_url(path), params=params)
    except httpx.RequestError as e:
        raise ServiceError(f"service unreachable at {config.SERVICE_URL}: {e}") from e
    if r.status_code >= 400:
        raise _error(path, r)
    return r.json()


async def _post(path: str, body: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(_url(path), json=body)
    except httpx.RequestError as e:
        raise ServiceError(f"service unreachable at {config.SERVICE_URL}: {e}") from e
    if r.status_code >= 400:
        raise _error(path, r)
    return r.json()


async def arbiter_pubkey(match_id: int) -> str:
    return (await _get("/escrow/arbiter-pubkey", {"matchId": match_id}))["pubkey"]


async def build_escrow(*, match_id: str, pk_a: str, pk_b: str, depositor_is_a: bool, reclaim_daa: int) -> dict:
    return await _post("/escrow/build", {
        "matchId": match_id, "pkA": pk_a, "pkB": pk_b,
        "depositorIsA": depositor_is_a, "reclaimDaa": reclaim_daa,
    })


async def escrow_balances(addresses: list[str], confirm_daa: int) -> dict[str, dict]:
    """{address: {"sompi": int, "confirmedSompi": int, "utxos": int}} — one RPC
    round-trip for the whole batch. Amounts come back over the wire as decimal
    strings (see escrow.js) and are parsed to int here, never float: these get
    compared against a stake, so a rounding artefact would be a money bug."""
    r = await _post("/escrow/balances", {"addresses": addresses, "confirmDaa": confirm_daa})
    return {addr: {"sompi": int(v["sompi"]), "confirmedSompi": int(v["confirmedSompi"]),
                   "utxos": int(v["utxos"])}
            for addr, v in r["balances"].items()}


async def settle_unsigned(*, match_id: str, escrows: list[dict], winner_addr: str | None, split: bool, rake_sompi: int) -> dict:
    return await _post("/escrow/settle-unsigned", {
        "matchId": match_id, "escrows": escrows, "winnerAddr": winner_addr,
        "split": split, "rakeSompi": rake_sompi,
    })


async def settle_broadcast(*, tx_json: str, escrows: list[dict], sigs_player: list[str], sigs_arb: list[str]) -> dict:
    return await _post("/escrow/settle-broadcast", {
        "txJson": tx_json, "escrows": escrows, "sigsPlayer": sigs_player, "sigsArb": sigs_arb,
    })


async def settle_broadcast_mutual(*, tx_json: str, escrows: list[dict], sigs_a: list[str], sigs_b: list[str]) -> dict:
    """Broadcast a mutual (arbiter-free) settle — roadmap #1. Same route as the
    arbiter path, but the sidecar sees sigsA+sigsB (both players, no arbiter)
    and assembles the {pkA, pkB} 2-subset of the 2-of-3 instead of
    {player, arbiter}. sigs_a[i] / sigs_b[i] are role-ordered by the caller
    (settlement.py knows which wallet is A and which is B)."""
    return await _post("/escrow/settle-broadcast", {
        "txJson": tx_json, "escrows": escrows, "sigsA": sigs_a, "sigsB": sigs_b,
    })


async def build_escrow_v2(*, match_id: str, pk_a: str, pk_b: str, side: str, reclaim_daa: int) -> dict:
    """Build one player's v2 covenant escrow (roadmap #2). `side` is 'A' or 'B'
    (which escrow); the reclaim branch is byte-identical to v1, so a stranded v2
    escrow reclaims through the existing /escrow/reclaim-* routes."""
    return await _post("/escrow-v2/build", {
        "matchId": match_id, "pkA": pk_a, "pkB": pk_b, "side": side, "reclaimDaa": reclaim_daa,
    })


async def oracle_sign_result(*, match_id: str, outcome: str) -> dict:
    """The oracle's signed verdict for a decided v2 match — {outcome, sigA, sigB}.
    The ONE thing DAGmate produces to settle; publish it so the winner (or
    anyone) can relay the settle even if DAGmate never does. `outcome` is
    'A', 'B' or 'draw'."""
    return await _post("/escrow-v2/oracle-sign", {"matchId": match_id, "outcome": outcome})


async def settle_v2(*, match_id: str, escrows: list[dict], outcome: str, pk_a: str, pk_b: str,
                    sig_a: str, sig_b: str) -> dict:
    """Build AND submit a v2 settle — no player signature, the covenant pays the
    winner (or, on a draw, each depositor back). Returns {txid, potSompi,
    feeSompi, outcome}. Idempotent at the chain level (a re-submit of an
    already-spent escrow is a double-spend the node rejects — caller checks for
    a prior txid)."""
    return await _post("/escrow-v2/settle", {
        "matchId": match_id, "escrows": escrows, "outcome": outcome,
        "pkA": pk_a, "pkB": pk_b, "sigA": sig_a, "sigB": sig_b,
    })


async def extract_sigs(*, signed_tx_json: str, indexes: list[int]) -> dict:
    """Pull the raw player signatures back out of the given inputs of a
    wallet-signed tx. Returns {"sigs": {"<index>": "<hex>"}}."""
    return await _post("/escrow/extract-sigs", {
        "signedTxJson": signed_tx_json, "indexes": indexes,
    })


async def reclaim_unsigned(*, address: str, depositor_addr: str, reclaim_daa: int) -> dict:
    """Unsigned tx draining one escrow's CLTV branch back to its depositor.
    Sompi fields come back as decimal strings (see escrow.js) — parsed to int
    by the caller, never float."""
    return await _post("/escrow/reclaim-unsigned", {
        "address": address, "depositorAddr": depositor_addr, "reclaimDaa": reclaim_daa,
    })


async def reclaim_broadcast(*, tx_json: str, redeem_hex: str, sigs: list[str]) -> dict:
    return await _post("/escrow/reclaim-broadcast", {
        "txJson": tx_json, "redeemHex": redeem_hex, "sigs": sigs,
    })


async def anchor(*, match_id: str, ply: int, payload_hex: str, fee_sompi: int = 0) -> dict:
    return await _post("/escrow/anchor", {
        "matchId": match_id, "ply": ply, "payloadHex": payload_hex, "feeSompi": fee_sompi,
    })


async def daa_score() -> int:
    return int((await _get("/escrow/daa"))["daaScore"])


async def verify_message(*, address: str, pubkey: str, message: str, signature: str) -> dict:
    """{"ok": bool, "reason": str} — did the holder of `address` sign `message`?

    All the key maths lives on the sidecar (service/auth.js), including the
    check that `pubkey` actually derives to `address`; this side only ever
    sees a boolean, so there's no way for the backend to talk itself into
    accepting a signature it shouldn't."""
    return await _post("/auth/verify-message", {
        "address": address, "pubkey": pubkey, "message": message, "signature": signature,
    })


async def demo_sign_message(*, private_key_hex: str, message: str) -> str:
    """Local-testing-only: sign a login challenge with a demo-wallet key, so
    the demo path goes THROUGH the real auth handshake rather than around it."""
    return (await _post("/dev/sign-message", {
        "privateKeyHex": private_key_hex, "message": message,
    }))["signature"]


async def generate_demo_keypair() -> dict:
    """Local-testing-only helper: a fresh throwaway Kaspa keypair, used as a
    stand-in wallet when no browser extension (Kasware/Kastle) is available
    to click through the full flow. See config.DEMO_WALLET_ENABLED."""
    return await _post("/dev/demo-keypair", {})
