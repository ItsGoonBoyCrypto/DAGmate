#!/usr/bin/env python3
"""DAGmate — read-only activity snapshot for the live site.

Opens the site DB READ-ONLY (never locks or writes it) and prints a one-screen
summary of who's playing: players, challenges, matches by status, results,
volume, tournaments, and a recent-activity feed. Safe to run anytime, on a
loop, or from cron — it only reads.

    python3 activity.py [--db /var/lib/dagmate/site/dagmate_site.db] [--recent 12]
"""
import argparse, sqlite3, time, os

SOMPI = 100_000_000

def short(a):
    if not a:
        return "—"
    return a if len(a) <= 16 else f"{a[:8]}…{a[-4:]}"

def ago(ts, now):
    if not ts:
        return "—"
    d = max(0, int(now - ts))
    if d < 60:   return f"{d}s ago"
    if d < 3600: return f"{d//60}m ago"
    if d < 86400: return f"{d//3600}h ago"
    return f"{d//86400}d ago"

def kas(sompi):
    return f"{(sompi or 0)/SOMPI:,.1f} KAS"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/var/lib/dagmate/site/dagmate_site.db")
    ap.add_argument("--recent", type=int, default=12)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"DB not found: {args.db}")
        return
    # Read-only URI connection — cannot lock or modify the live DB.
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    q = lambda s, *a: con.execute(s, a).fetchall()
    one = lambda s, *a: con.execute(s, a).fetchone()[0]
    now = time.time()

    def count(sql, *a):
        try:
            return one(sql, *a)
        except sqlite3.OperationalError:
            return 0

    players = count("SELECT COUNT(*) FROM accounts")
    p1h  = count("SELECT COUNT(*) FROM accounts WHERE created_ts > ?", now - 3600)
    p24h = count("SELECT COUNT(*) FROM accounts WHERE created_ts > ?", now - 86400)

    ch = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM challenges GROUP BY status")}
    ch_total = sum(ch.values())

    ms = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM matches GROUP BY status")}
    m_total = sum(ms.values())
    res = {r["result"]: r["n"] for r in q("SELECT result, COUNT(*) n FROM matches WHERE status='settled' AND result IS NOT NULL GROUP BY result")}

    deposited = count("SELECT COALESCE(SUM(COALESCE(funded_a_sompi,0)+COALESCE(funded_b_sompi,0)),0) FROM matches")
    settled_pots = count("SELECT COALESCE(SUM(stake_sompi*2),0) FROM matches WHERE status='settled' AND winner_account_id IS NOT NULL")

    print(f"\n════ DAGmate — activity ════   {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(now))}\n")
    print(f"  Players      : {players:<4} (+{p1h} in 1h · +{p24h} in 24h)")
    print(f"  Challenges   : {ch_total:<4} open {ch.get('open',0)} · accepted {ch.get('accepted',0)} · declined {ch.get('declined',0)}")
    print(f"  Matches      : {m_total:<4} awaiting {ms.get('awaiting_deposit',0)} · live {ms.get('live',0)} · settled {ms.get('settled',0)} · void {ms.get('void',0)} · expired {ms.get('expired',0)}")
    print(f"  Results      : checkmate {res.get('checkmate',0)} · resign {res.get('resign',0)} · draw {res.get('draw_agreed',0)+res.get('draw',0)} · timeout {res.get('timeout',0)+res.get('draw_timeout',0)}")
    print(f"  Volume       : deposited {kas(deposited)} · settled pots {kas(settled_pots)}")

    trows = q("""SELECT t.tier_kas, t.status, COUNT(e.account_id) n
                 FROM tournaments t LEFT JOIN tournament_entrants e ON e.tournament_id=t.id
                 WHERE t.status IN ('open','running') GROUP BY t.id ORDER BY t.tier_kas""")
    if trows:
        parts = [f"{r['tier_kas']} KAS {r['n']}/8 {r['status']}" for r in trows]
        print(f"  Tournaments  : " + " · ".join(parts))
    else:
        print(f"  Tournaments  : none active")

    # recent feed: newest activity by whichever timestamp is latest
    addr = {r["id"]: r["address"] for r in q("SELECT id, address FROM accounts")}
    feed = q("""SELECT id, status, result, stake_sompi, winner_account_id,
                       player_a_account_id, player_b_account_id, created_ts, settled_ts,
                       COALESCE(settled_ts, created_ts) AS ts
                FROM matches ORDER BY ts DESC LIMIT ?""", args.recent)
    print("\n  Recent:")
    if not feed:
        print("    (no matches yet — waiting on the first challenge)")
    for m in feed:
        a = short(addr.get(m["player_a_account_id"]))
        b = short(addr.get(m["player_b_account_id"]))
        tag = m["status"]
        if m["status"] == "settled":
            w = short(addr.get(m["winner_account_id"])) if m["winner_account_id"] else "draw"
            tag = f"settled ({m['result'] or '?'} → {w})"
        print(f"    {ago(m['ts'], now):<8} {tag:<28} {kas(m['stake_sompi']):>10}   {a} vs {b}")
    print()
    con.close()

if __name__ == "__main__":
    main()
