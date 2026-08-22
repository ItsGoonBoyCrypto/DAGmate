"""DAGmate — chess rules engine wrapper (docs/DAGMATE_SPEC.md §5).

Thin wrapper over python-chess: it IS the rules authority (legality, mate,
stalemate, auto-draws, threefold/50-move claims) — nothing here re-derives
or second-guesses it. Board state round-trips as FEN; move history as a
plain list of UCI strings (both trivial to store/replay).
"""
from __future__ import annotations

import chess

STARTING_FEN = chess.STARTING_FEN


def board_from(fen: str) -> chess.Board:
    return chess.Board(fen)


def legal_uci_moves(fen: str) -> list[str]:
    board = board_from(fen)
    return [m.uci() for m in board.legal_moves]


def apply_uci(fen: str, uci_move: str) -> dict:
    """Apply one move. Raises ValueError on anything illegal — python-chess
    rejects it before it ever touches state, per spec §5."""
    board = board_from(fen)
    try:
        move = chess.Move.from_uci(uci_move)
    except ValueError:
        raise ValueError(f"malformed move: {uci_move}")
    if move not in board.legal_moves:
        raise ValueError(f"illegal move: {uci_move}")
    board.push(move)
    return status_of(board)


def status_of(board: chess.Board) -> dict:
    """Result table per spec §5. `game_over`/`result`/`winner_color` describe
    a natural end; `None` result means the game continues."""
    result = None
    winner_color = None  # 'white' | 'black' | None (draw)
    if board.is_checkmate():
        result = "checkmate"
        winner_color = "black" if board.turn == chess.WHITE else "white"  # side to move is mated
    elif (board.is_stalemate() or board.is_insufficient_material()
          or board.is_seventyfive_moves() or board.is_fivefold_repetition()):
        result = "draw"
    return {
        "fen": board.fen(),
        "turn": "white" if board.turn == chess.WHITE else "black",
        "game_over": result is not None,
        "result": result,
        "winner_color": winner_color,
        "can_claim_draw": board.can_claim_threefold_repetition() or board.can_claim_fifty_moves(),
        "in_check": board.is_check(),
    }


def timeout_result(fen: str, flagged_color: str) -> tuple[str, str | None]:
    """What running out of time actually means. Returns (result, winner_color).

    FIDE 6.9: flagging normally loses, but NOT if the opponent has no way to
    deliver mate — a lone king can't win on time, it's a draw. Worth getting
    right rather than defaulting to "flag = loss": in a wagered game that
    distinction is the difference between taking the pot and splitting it.
    """
    board = board_from(fen)
    opponent = chess.BLACK if flagged_color == "white" else chess.WHITE
    if board.has_insufficient_material(opponent):
        return "draw_timeout", None
    return "timeout", ("black" if flagged_color == "white" else "white")


# The practice opponent lives in engine.py — this module stays purely the rules
# authority so there is exactly one place that decides what is legal.
