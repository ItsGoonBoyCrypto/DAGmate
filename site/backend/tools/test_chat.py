"""Tests for in-match chat (private, off-chain relay). Run: python tools/test_chat.py

Real schema + accessors + the real HTTP handlers (main.post_chat / get_chat); DMs stubbed. Proves:
both players can post + read in order, paging by `after` returns only new lines, a spectator/stranger
is refused (chat is private to the two players), empty/over-long messages bounce, and the per-player
rate limit trips. No sidecar involved — chat never touches the chain.
"""
from __future__ import annotations
import asyncio, os, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DAGMATE_SITE_DB"] = os.path.join(tempfile.mkdtemp(prefix="dagmate-chat-"), "t.db")

import config  # noqa: E402
config.DB_PATH = os.environ["DAGMATE_SITE_DB"]
assert config.DB_PATH.startswith(tempfile.gettempdir())

import bot_client  # noqa: E402
async def _noop(*a, **k): return None
for _n in dir(bot_client):
    if _n.startswith("notify_"): setattr(bot_client, _n, _noop)

import database as db  # noqa: E402
import chess_logic  # noqa: E402
import main  # noqa: E402
from main import ChatBody  # noqa: E402
from fastapi import HTTPException  # noqa: E402

_fail = []
def ck(name, cond, extra=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond: _fail.append(name)

def status_of(fn):
    try: fn(); return 200
    except HTTPException as e: return e.status_code

def a_match():
    a = db.get_or_create_account(f"kaspa:pA{time.time_ns()}", "pubA")
    b = db.get_or_create_account(f"kaspa:pB{time.time_ns()}", "pubB")
    m = db.create_match(challenge_id=None, tournament_id=None, round_no=None,
                        player_a_account_id=a["id"], player_b_account_id=b["id"],
                        stake_sompi=0, mode="rapid", fen=chess_logic.STARTING_FEN,
                        escrow_a=None, escrow_b=None, reclaim_daa=None)
    return m["id"], a, b

def main_():
    db.ensure_schema()
    config.MATCH_CHAT_ENABLED = True

    print("both players post; each sees the thread in order, tagged by colour + mine/theirs")
    mid, a, b = a_match()
    m1 = main.post_chat(mid, ChatBody(text="gl hf"), account=a)
    m2 = main.post_chat(mid, ChatBody(text="you too"), account=b)
    ck("A message tagged white + mine (to A)", (m1["byColor"], m1["mine"]), ("white", True))
    ck("B message tagged black", m2["byColor"], "black")
    ck("sequence increments", m2["seq"] > m1["seq"])
    a_view = main.get_chat(mid, after=0, account=a)
    ck("A sees both, in order", [x["text"] for x in a_view["messages"]], ["gl hf", "you too"])
    ck("A: own line is mine, opponent's is not", [x["mine"] for x in a_view["messages"]], [True, False])
    b_view = main.get_chat(mid, after=0, account=b)
    ck("B: mine/theirs is from B's view", [x["mine"] for x in b_view["messages"]], [False, True])

    print("paging: after=lastSeq returns only new messages")
    last = a_view["lastSeq"]
    main.post_chat(mid, ChatBody(text="nice move"), account=b)
    delta = main.get_chat(mid, after=last, account=a)
    ck("only the new line", [x["text"] for x in delta["messages"]], ["nice move"])
    ck("lastSeq advanced", delta["lastSeq"] > last)

    print("chat is private to the two players")
    outsider = db.get_or_create_account(f"kaspa:evil{time.time_ns()}", "pubX")
    ck("stranger can't read", status_of(lambda: main.get_chat(mid, after=0, account=outsider)), 403)
    ck("stranger can't post", status_of(lambda: main.post_chat(mid, ChatBody(text="hi"), account=outsider)), 403)
    ck("chat on a missing match is 404", status_of(lambda: main.get_chat("nope", after=0, account=a)), 404)

    print("empty + over-long messages bounce")
    ck("empty rejected", status_of(lambda: main.post_chat(mid, ChatBody(text="   "), account=a)), 400)
    ck("over-long rejected", status_of(lambda: main.post_chat(mid, ChatBody(text="x" * (config.CHAT_MAX_LEN + 1)), account=a)), 400)
    ck("at the cap is fine", status_of(lambda: main.post_chat(mid, ChatBody(text="x" * config.CHAT_MAX_LEN), account=a)), 200)

    print("per-player rate limit trips, and is per-match/per-player")
    mid2, a2, b2 = a_match()
    codes = [status_of(lambda: main.post_chat(mid2, ChatBody(text="spam"), account=a2)) for _ in range(config.CHAT_RATE_MAX + 3)]
    ck("first CHAT_RATE_MAX allowed", all(c == 200 for c in codes[:config.CHAT_RATE_MAX]), codes[:config.CHAT_RATE_MAX])
    ck("then 429", 429 in codes[config.CHAT_RATE_MAX:], codes)
    ck("the OTHER player isn't rate-limited by A's spam", status_of(lambda: main.post_chat(mid2, ChatBody(text="hi"), account=b2)), 200)

    print("the switch turns it off")
    config.MATCH_CHAT_ENABLED = False
    ck("post refused when off", status_of(lambda: main.post_chat(mid, ChatBody(text="hi"), account=a)), 403)
    ck("get returns empty when off", main.get_chat(mid, after=0, account=a)["messages"] == [])
    config.MATCH_CHAT_ENABLED = True

    print()
    print(f"{len(_fail)} FAILED: {_fail}" if _fail else "all chat checks passed")
    return 1 if _fail else 0

if __name__ == "__main__":
    raise SystemExit(main_())
