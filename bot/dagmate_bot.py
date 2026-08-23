"""dagmate_bot.py — DAGmate alerts bot (docs/DAGMATE_SPEC.md §4).

Zero Dagger code, zero shared infrastructure, zero custody. This bot never
touches a wallet, a game board, or a key of any kind — the actual chess
(escrow, moves, clocks, settlement) all lives on dagmate.org and its
service/ sidecar. This process's entire job is Telegram notifications:

  /start          — mint a one-time link code to paste into dagmate.org
  /alerts on|off  — toggle notifications
  /unlink         — disconnect this Telegram from the site account

...plus an internal HTTP API (localhost-bound, shared-secret auth) that the
site backend calls to push alerts and to claim a link code:

  POST /link/claim           {code, site_account_id}
  POST /notify/challenge     {site_account_id, challenger_name, stake, mode, url}
  POST /notify/your-move     {site_account_id, match_id, url}
  POST /notify/clock-warning {site_account_id, match_id, remaining, url}
  POST /notify/draw-offer    {site_account_id, match_id, url}
  POST /notify/settled       {site_account_id, match_id, summary}

Run: `python dagmate_bot.py` — needs DAGMATE_BOT_TOKEN and
DAGMATE_WEBHOOK_SECRET in the environment (see config.py).
"""
from __future__ import annotations

import asyncio
import hmac
import html
import logging

from aiohttp import web
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import Application, CommandHandler, ContextTypes, filters

import config
import database as db

log = logging.getLogger(__name__)


def _esc(v) -> str:
    return html.escape(str(v))


# ── commands ──────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = await asyncio.to_thread(db.get_link, user.id)
    if row and row["site_account_id"]:
        return await update.effective_message.reply_text(
            "\u2659 DAGmate alerts are already linked to your account.\n"
            "Use /alerts on|off to toggle, or /unlink to disconnect.")
    code = await asyncio.to_thread(db.new_link_code, user.id)
    ttl_min = max(1, config.LINK_CODE_TTL_S // 60)
    await update.effective_message.reply_text(
        f"\u2659 <b>DAGmate alerts</b>\n\n"
        f"Paste this code into dagmate.org to connect this Telegram for match "
        f"alerts (challenges, your move, clock warnings, results):\n\n"
        f"<code>{code}</code>\n\n"
        f"Expires in {ttl_min} min. This bot never asks for your wallet or "
        f"any key \u2014 it only sends notifications.",
        parse_mode=ParseMode.HTML)


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = await asyncio.to_thread(db.get_link, user.id)
    if not row or not row["site_account_id"]:
        return await update.effective_message.reply_text(
            "Not linked yet \u2014 send /start to get a link code.")
    args = context.args or []
    if not args or args[0].lower() not in ("on", "off"):
        state = "on" if row["alerts_enabled"] else "off"
        return await update.effective_message.reply_text(
            f"Alerts are currently {state}. Use /alerts on or /alerts off.")
    enabled = args[0].lower() == "on"
    await asyncio.to_thread(db.set_alerts, user.id, enabled)
    await update.effective_message.reply_text(f"Alerts turned {'on' if enabled else 'off'}.")


async def cmd_unlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = await asyncio.to_thread(db.get_link, user.id)
    if not row or not row["site_account_id"]:
        return await update.effective_message.reply_text("Not linked.")
    await asyncio.to_thread(db.unlink, user.id)
    await update.effective_message.reply_text("Unlinked. Send /start any time to reconnect.")


# ── internal HTTP API (localhost-bound, shared-secret auth) ────────────────
def _authed(request: web.Request) -> bool:
    # Constant-time compare so the check doesn't leak the secret byte-by-byte
    # through timing the moment WEBHOOK_HOST is ever widened past loopback. The
    # secret is guaranteed non-empty and >=32 chars by config, so a missing or
    # empty header can never match.
    provided = request.headers.get("X-DAGmate-Secret", "")
    return hmac.compare_digest(provided, config.WEBHOOK_SHARED_SECRET)


async def _notify(request: web.Request, render) -> web.Response:
    """Shared body for every notify_* route: auth, look up the Telegram user
    linked to the given site_account_id, skip silently if unlinked/muted,
    render + send. A missing link or a delivery failure is never an error
    the site backend needs to retry on — it's just "nobody to tell right
    now" (see docs/DAGMATE_SPEC.md §4: alerts are best-effort, not part of
    the settlement path)."""
    if not _authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
        site_account_id = str(body["site_account_id"])
    except Exception:
        return web.json_response({"error": "bad_request"}, status=400)

    row = await asyncio.to_thread(db.get_by_site_account, site_account_id)
    if not row or not row["alerts_enabled"]:
        return web.json_response({"sent": False, "reason": "not_linked_or_muted"})

    bot = request.app["bot"]
    text = render(body)
    try:
        await bot.send_message(row["telegram_user_id"], text, parse_mode=ParseMode.HTML)
        return web.json_response({"sent": True})
    except (Forbidden, BadRequest) as e:
        log.warning(f"notify send failed telegram_user_id={row['telegram_user_id']}: {e}")
        return web.json_response({"sent": False, "reason": "send_failed"})


async def route_notify_challenge(request: web.Request) -> web.Response:
    def render(b):
        return (f"\u2659 <b>{_esc(b.get('challenger_name', 'Someone'))}</b> challenged you "
                f"to {_esc(b.get('stake', '?'))} KAS chess ({_esc(b.get('mode', ''))}).\n"
                f"{_esc(b.get('url', ''))}")
    return await _notify(request, render)


async def route_notify_your_move(request: web.Request) -> web.Response:
    def render(b):
        return f"\u2659 Your move \u2014 match #{_esc(b.get('match_id', '?'))}.\n{_esc(b.get('url', ''))}"
    return await _notify(request, render)


async def route_notify_clock_warning(request: web.Request) -> web.Response:
    def render(b):
        return (f"\u23f1 Match #{_esc(b.get('match_id', '?'))}: "
                f"{_esc(b.get('remaining', 'low time'))} left to move.\n{_esc(b.get('url', ''))}")
    return await _notify(request, render)


async def route_notify_draw_offer(request: web.Request) -> web.Response:
    """Worth its own alert rather than folding into your-move: in daily mode a
    player has three days per move, so an offer sitting unseen on the board is
    an offer that expires the moment the offerer plays on."""
    def render(b):
        return (f"\u00bd Draw offered in match #{_esc(b.get('match_id', '?'))} \u2014 "
                f"accept and the pot splits back to you both.\n{_esc(b.get('url', ''))}")
    return await _notify(request, render)


async def route_notify_settled(request: web.Request) -> web.Response:
    def render(b):
        return f"\u2659 Match #{_esc(b.get('match_id', '?'))} settled: {_esc(b.get('summary', ''))}"
    return await _notify(request, render)


async def route_claim_link(request: web.Request) -> web.Response:
    """Site backend calls this once a logged-in player submits the code they
    got from /start. Pure DB linking \u2014 no Telegram send here."""
    if not _authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
        code = str(body["code"]).strip().upper()
        site_account_id = str(body["site_account_id"])
    except Exception:
        return web.json_response({"error": "bad_request"}, status=400)
    ok = await asyncio.to_thread(db.claim_link_code, code, site_account_id)
    return web.json_response({"linked": ok} if ok else {"linked": False, "reason": "invalid_or_expired"})


def _build_webhook_app(bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.add_routes([
        web.post("/link/claim", route_claim_link),
        web.post("/notify/challenge", route_notify_challenge),
        web.post("/notify/your-move", route_notify_your_move),
        web.post("/notify/clock-warning", route_notify_clock_warning),
        web.post("/notify/draw-offer", route_notify_draw_offer),
        web.post("/notify/settled", route_notify_settled),
    ])
    return app


async def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.ensure_schema()

    application = Application.builder().token(config.BOT_TOKEN).build()
    # Private chats only. /start mints a one-time link code, and in a group that
    # code would be posted for everyone to see — the first person to paste it
    # into the site binds the victim's account to their own Telegram. The other
    # two are personal commands with no reason to run in a group either.
    private = filters.ChatType.PRIVATE
    application.add_handler(CommandHandler("start", cmd_start, filters=private))
    application.add_handler(CommandHandler("alerts", cmd_alerts, filters=private))
    application.add_handler(CommandHandler("unlink", cmd_unlink, filters=private))

    runner = web.AppRunner(_build_webhook_app(application.bot))
    await runner.setup()
    site = web.TCPSite(runner, config.WEBHOOK_HOST, config.WEBHOOK_PORT)
    await site.start()
    log.info(f"Internal webhook API listening on {config.WEBHOOK_HOST}:{config.WEBHOOK_PORT}")

    async with application:
        await application.start()
        await application.updater.start_polling()
        log.info("DAGmate alerts bot: polling started")
        try:
            await asyncio.Event().wait()
        finally:
            await application.updater.stop()
            await application.stop()
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
