#!/usr/bin/env python3
"""DAGmate — Telegram activity monitor (server-side, cron-driven).

Runs the read-only activity checker, diffs against the last snapshot, and DMs
the admin via the DAGmate bot ONLY when something changed. First run sends a
one-time "online" confirmation and records a baseline. Reads the bot token and
admin chat id from /etc/dagmate/bot.env — no secrets or personal ids live in
this file or the repo.

Cron (every 15 min), as root:
    */15 * * * * root /usr/bin/python3 /root/dagmate-tg-monitor.py >> /var/log/dagmate-monitor.log 2>&1
"""
import json, os, subprocess, urllib.request, urllib.parse

ENV = "/etc/dagmate/bot.env"
SNAP = "/root/dagmate-activity-tg-last.json"
CHECKER = ["python3", "/root/dagmate-activity.py", "--json"]

def env(key):
    try:
        with open(ENV) as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None

def send(token, chat, text):
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "disable_web_page_preview": "true",
    }).encode()
    urllib.request.urlopen(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20).read()

def main():
    token = env("DAGMATE_BOT_TOKEN")
    chat = env("DAGMATE_ADMIN_CHAT_ID")
    if not token or not chat:
        print("missing DAGMATE_BOT_TOKEN or DAGMATE_ADMIN_CHAT_ID in", ENV)
        return

    cur = json.loads(subprocess.check_output(CHECKER).decode())
    last = None
    if os.path.exists(SNAP):
        try:
            with open(SNAP) as f:
                last = json.load(f)
        except (OSError, ValueError):
            last = None
    with open(SNAP, "w") as f:
        json.dump(cur, f)

    if last is None:
        send(token, chat,
             "✅ DAGmate activity monitor is online.\n\nI'll message you here whenever "
             "something changes on-chain — new players, challenges, funded matches, or "
             "results. Silence means nothing new. (Checks every 15 min.)")
        return
    if cur == last:
        return  # nothing changed — stay quiet

    labels = [("players", "players"), ("challenges", "challenges"), ("matches", "matches"),
              ("live", "live now"), ("settled", "settled"), ("void", "void"),
              ("deposited_kas", "deposited KAS"), ("settled_pots_kas", "settled pots KAS")]
    lines = []
    for key, label in labels:
        c = cur.get(key, 0)
        l = last.get(key, 0)
        if c != l:
            d = round(c - l, 2)
            sign = f"+{d}" if d > 0 else str(d)
            lines.append(f"• {label}: {l} → {c} ({sign})")
    if not lines:
        return

    msg = ("\U0001F514 DAGmate activity\n" + "\n".join(lines) +
           f"\n\nNow: {cur['players']} players · {cur['matches']} matches "
           f"({cur['live']} live, {cur['settled']} settled) · "
           f"{cur['deposited_kas']} KAS deposited")
    send(token, chat, msg)

if __name__ == "__main__":
    main()
