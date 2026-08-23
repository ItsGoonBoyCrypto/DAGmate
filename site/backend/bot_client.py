"""DAGmate site backend — client for the alerts bot's internal webhook API
(docs/DAGMATE_SPEC.md §4). Every call is fire-and-forget/best-effort: a
missing link or an unreachable bot is never a reason to fail the actual
game/settlement action that triggered the notification."""
from __future__ import annotations

import logging

import httpx

import config

log = logging.getLogger(__name__)


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


async def notify_challenge(site_account_id: str, challenger_name: str, stake_kas: float, mode: str, url: str):
    await _post("/notify/challenge", {
        "site_account_id": site_account_id, "challenger_name": challenger_name,
        "stake": stake_kas, "mode": mode, "url": url,
    })


async def notify_your_move(site_account_id: str, match_id: str, url: str):
    await _post("/notify/your-move", {"site_account_id": site_account_id, "match_id": match_id, "url": url})


async def notify_clock_warning(site_account_id: str, match_id: str, remaining: str, url: str):
    await _post("/notify/clock-warning", {
        "site_account_id": site_account_id, "match_id": match_id, "remaining": remaining, "url": url,
    })


async def notify_draw_offer(site_account_id: str, match_id: str, url: str):
    await _post("/notify/draw-offer", {"site_account_id": site_account_id, "match_id": match_id, "url": url})


async def notify_settled(site_account_id: str, match_id: str, summary: str):
    await _post("/notify/settled", {"site_account_id": site_account_id, "match_id": match_id, "summary": summary})
