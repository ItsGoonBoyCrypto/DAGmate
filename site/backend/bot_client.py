"""DAGmate site backend — client for the alerts bot's internal webhook API
(docs/DAGMATE_SPEC.md §4). Every call is fire-and-forget/best-effort: a
missing link or an unreachable bot is never a reason to fail the actual
game/settlement action that triggered the notification."""
from __future__ import annotations

import logging

import httpx

import config

log = logging.getLogger(__name__)


def _abs(url: str) -> str:
    """Make an in-app path absolute so Telegram renders it as a link. A value
    that's already absolute (http/https) is left alone."""
    if url and url.startswith("/"):
        return f"{config.PUBLIC_URL}{url}"
    return url


async def _post(path: str, body: dict):
    if not config.BOT_WEBHOOK_SECRET:
        return  # alerts bot not configured for this deployment — skip silently
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{config.BOT_WEBHOOK_URL}{path}", json=body,
                headers={"X-DAGmate-Secret": config.BOT_WEBHOOK_SECRET})
    except httpx.RequestError as e:
        log.warning(f"bot webhook {path} failed (non-fatal): {e}")


async def claim_link(code: str, site_account_id: str) -> dict:
    """Link a Telegram (identified only by the one-time code from /start) to a
    logged-in site account. Unlike the notify_* calls this is NOT
    fire-and-forget: the player is watching for the result of pasting their
    code, so we return the bot's verdict and keep the failure modes distinct —
    "no alerts bot in this deployment", "bot unreachable", and "bad/expired
    code" are three different messages. Never raises; the endpoint turns the
    returned dict into an HTTP status. The bot never learns the wallet or any
    key — it only stores which Telegram belongs to which site account."""
    if not config.BOT_WEBHOOK_SECRET:
        return {"linked": False, "reason": "not_configured"}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(
                f"{config.BOT_WEBHOOK_URL}/link/claim",
                json={"code": code, "site_account_id": site_account_id},
                headers={"X-DAGmate-Secret": config.BOT_WEBHOOK_SECRET})
        if r.status_code != 200:
            return {"linked": False, "reason": "bot_error"}
        return r.json()
    except (httpx.RequestError, ValueError) as e:
        log.warning(f"bot link claim failed (non-fatal): {e}")
        return {"linked": False, "reason": "unreachable"}


async def notify_challenge(site_account_id: str, challenger_name: str, stake_kas: float, mode: str, url: str):
    await _post("/notify/challenge", {
        "site_account_id": site_account_id, "challenger_name": challenger_name,
        "stake": stake_kas, "mode": mode, "url": _abs(url),
    })


async def notify_your_move(site_account_id: str, match_id: str, url: str):
    await _post("/notify/your-move", {"site_account_id": site_account_id, "match_id": match_id, "url": _abs(url)})


async def notify_clock_warning(site_account_id: str, match_id: str, remaining: str, url: str):
    await _post("/notify/clock-warning", {
        "site_account_id": site_account_id, "match_id": match_id, "remaining": remaining, "url": _abs(url),
    })


async def notify_draw_offer(site_account_id: str, match_id: str, url: str):
    await _post("/notify/draw-offer", {"site_account_id": site_account_id, "match_id": match_id, "url": _abs(url)})


async def notify_settled(site_account_id: str, match_id: str, summary: str):
    await _post("/notify/settled", {"site_account_id": site_account_id, "match_id": match_id, "summary": summary})
