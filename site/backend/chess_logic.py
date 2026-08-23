"""DAGmate — chess rules engine wrapper (docs/DAGMATE_SPEC.md §5).

Thin wrapper over python-chess: it IS the rules authority (legality, mate,
stalemate, and all the rule-forced draws — stalemate, insufficient material,
threefold/fivefold repetition, fifty/seventy-five-move) — nothing here
re-derives or second-guesses it. Rule-forced draws end the game automatically;
a VOLUNTARY draw (a truce in a live position) is a separate, two-sided
offer/accept flow handled in the app, not here. Board state round-trips as FEN;
move history as a plain list of UCI strings (both trivial to store/replay).
"""
from __future__ import annotations

import chess

STARTING_FEN = chess.STARTING_FEN


def board_from(fen: str) -> chess.Board:
    return chess.Board(fen)


def board_from_history(history: list[str]) -> chess.Board:
    """Rebuild a board by replaying the whole game from the standard start.

    This is the ONLY way repetition draws can be detected: a board created from
    a FEN has no move stack, so `is_repetition`/`is_fivefold_repetition` see a
    single position and can never fire. Matches always begin at the standard
    position and store their full UCI move list, so the list is the source of
    truth for anything history-dependent (repetition), while the FEN carries the
    history-free state (piece placement, castling, the halfmove clock the fifty
    /seventy-five-move rules use)."""
    board = chess.Board()
    for uci in history:
        board.push_uci(uci)
    return board


def legal_uci_moves(fen: str) -> list[str]:
    board = board_from(fen)
    return [m.uci() for m in board.legal_moves]


def apply_uci(fen: str, uci_move: str, history: list[str] | None = None) -> dict:
    """Apply one move. Raises ValueError on anything illegal — python-chess
    rejects it before it ever touches state, per spec §5.

    `history` is the full UCI move list BEFORE this move (from the standard
    start). Pass it for a real match so repetition draws are detectable; omit it
    on a throwaway board (the practice trainer) where a fresh board from `fen`
    is enough. When given, the replayed position must match `fen` or the move
    list and the stored state have diverged — a bug worth failing on, not
    papering over."""
    if history is not None:
        board = board_from_history(history)
        if board.fen() != fen:
            raise ValueError("move history doesn't match the stored position")
    else:
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
          or board.is_seventyfive_moves() or board.is_fivefold_repetition()
          # Threefold repetition and the fifty-move rule are treated as
          # AUTOMATIC draws here, not "claims". In FIDE they're claimable (a
          # player asserts them); we auto-end instead, and deliberately so. This
          # is a wagered game: a claim a player never gets to make is a player
          # flagged on the clock in a position that is already dead-drawn by
          # rule, losing a pot that should have split. A player who would rather
          # play on simply doesn't make the move that repeats the position a
          # third time (or resets the fifty-move clock with a pawn move/capture)
          # — the choice stays in their hands, but nobody can be cheated out of
          # a rule-mandated draw. Voluntary draws (a truce in a live position)
          # still go through the two-sided offer/accept flow. We use the
          # condition-is-met primitives (is_repetition(3), halfmove_clock>=100),
          # NOT can_claim_* which look a move ahead.
          or board.is_repetition(3) or board.halfmove_clock >= 100):
        result = "draw"
    return {
        "fen": board.fen(),
        "turn": "white" if board.turn == chess.WHITE else "black",
        "game_over": result is not None,
        "result": result,
        "winner_color": winner_color,
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
