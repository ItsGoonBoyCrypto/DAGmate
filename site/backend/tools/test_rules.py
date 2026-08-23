"""Tests for the rules authority (chess_logic) — run: python tools/test_rules.py

Pure rules, no DB. The point of these is the MONEY-relevant endings: which
positions end the game, and whether the pot goes to a winner or splits. A
rule-forced draw that DAGmate failed to detect would leave a dead-drawn game
running until someone flags on the clock — losing a pot that should have split.

Covered:
  1. Checkmate still ends the game with a winner (regression guard).
  2. Stalemate is a draw.
  3. Threefold repetition auto-draws (not left as an unclaimable "claim").
  4. The fifty-move rule auto-draws.
  5. A live position is not falsely reported as over.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chess_logic  # noqa: E402

_failures: list[str] = []


def check(name: str, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        _failures.append(name)


def status(fen: str) -> dict:
    return chess_logic.status_of(chess_logic.board_from(fen))


def replay(moves: list[str]) -> dict:
    """Apply a UCI sequence from the start and return the final status, passing
    the accumulating history exactly as the match move endpoint does (so
    repetition detection has the move stack it needs)."""
    st = {"fen": chess_logic.STARTING_FEN}
    history: list[str] = []
    for uci in moves:
        st = chess_logic.apply_uci(st["fen"], uci, history=history)
        history.append(uci)
    return st


def main_() -> int:
    print("checkmate ends the game with a winner")
    # 1.f3 e5 2.g4 Qh4# — white to move is mated.
    st = status("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3")
    check("game over", st["game_over"], True)
    check("result checkmate", st["result"], "checkmate")
    check("black wins", st["winner_color"], "black")

    print("stalemate is a draw")
    # Black king a8, white queen b6, white king h2 — black to move, not in
    # check, no legal move.
    st = status("k7/8/1Q6/8/8/8/7K/8 b - - 0 1")
    check("game over", st["game_over"], True)
    check("result draw", st["result"], "draw")
    check("no winner", st["winner_color"], None)

    print("threefold repetition auto-draws")
    # Shuffle the knights back and forth so the start position occurs 3 times.
    st = replay(["g1f3", "g8f6", "f3g1", "f6g8", "g1f3", "g8f6", "f3g1", "f6g8"])
    check("game over", st["game_over"], True)
    check("result draw", st["result"], "draw")
    check("no winner", st["winner_color"], None)

    print("one repetition short is still live")
    st = replay(["g1f3", "g8f6", "f3g1", "f6g8", "g1f3", "g8f6"])  # start seen only twice
    check("still playing", st["game_over"], False)

    print("fifty-move rule auto-draws")
    # Halfmove clock at 99; a non-pawn, non-capture move reaches 100.
    st = chess_logic.apply_uci("8/8/8/4k3/8/4K3/8/6R1 w - - 99 60", "g1g2")
    check("game over", st["game_over"], True)
    check("result draw", st["result"], "draw")

    print("one halfmove short is still live")
    st = chess_logic.apply_uci("8/8/8/4k3/8/4K3/8/6R1 w - - 98 60", "g1g2")  # clock -> 99
    check("still playing", st["game_over"], False)

    print("a normal opening move leaves the game live")
    st = chess_logic.apply_uci(chess_logic.STARTING_FEN, "e2e4")
    check("still playing", st["game_over"], False)
    check("no result", st["result"], None)

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("all rules checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_())
