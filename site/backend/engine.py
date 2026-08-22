"""DAGmate — practice bot (docs/DAGMATE_SPEC.md §9).

A self-contained negamax searcher: alpha-beta, MVV-LVA move ordering,
quiescence search, a Zobrist transposition table and iterative deepening under
a hard wall-clock budget. No engine binary, no download, no subprocess — the
practice board is a free feature, so its cost has to be boundable on the server.

⚠️ The tier names are DIFFICULTY TIERS, not FIDE titles. A pure-Python searcher
inside a request handler tops out around club-player strength; "Grandmaster"
here means "the strongest this engine plays", not 2500 Elo. Genuinely titled
strength needs Stockfish — the intended path is the WASM build running in the
player's own browser, which also moves the CPU cost off the server entirely.

Weak tiers are deliberately NOT "random legal move". Random play is unpleasant
to practise against because it's erratic rather than weak — it hangs its queen
and then finds a brilliancy. Instead every tier searches properly and then
degrades in two controlled ways: `slack` widens the set of moves considered
acceptable (so it picks a reasonable-but-not-best move), and `blunder` is the
chance of an outright mistake. That produces an opponent that feels like a
weaker player rather than a broken one.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass

import chess
import chess.polyglot

MATE = 30_000
_INF = 1 << 30

PIECE_VALUE = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,  # kings are never captured; mate is scored separately
}

# Piece-square tables — Tomasz Michniewski's "Simplified Evaluation Function"
# (Chess Programming Wiki). Written in board-reading order (a8..h8 first), so
# they index with chess.square_mirror(sq) for White and sq directly for Black.
_PST_PAWN = [
      0,  0,  0,  0,  0,  0,  0,  0,
     50, 50, 50, 50, 50, 50, 50, 50,
     10, 10, 20, 30, 30, 20, 10, 10,
      5,  5, 10, 25, 25, 10,  5,  5,
      0,  0,  0, 20, 20,  0,  0,  0,
      5, -5,-10,  0,  0,-10, -5,  5,
      5, 10, 10,-20,-20, 10, 10,  5,
      0,  0,  0,  0,  0,  0,  0,  0,
]
_PST_KNIGHT = [
    -50,-40,-30,-30,-30,-30,-40,-50,
    -40,-20,  0,  0,  0,  0,-20,-40,
    -30,  0, 10, 15, 15, 10,  0,-30,
    -30,  5, 15, 20, 20, 15,  5,-30,
    -30,  0, 15, 20, 20, 15,  0,-30,
    -30,  5, 10, 15, 15, 10,  5,-30,
    -40,-20,  0,  5,  5,  0,-20,-40,
    -50,-40,-30,-30,-30,-30,-40,-50,
]
_PST_BISHOP = [
    -20,-10,-10,-10,-10,-10,-10,-20,
    -10,  0,  0,  0,  0,  0,  0,-10,
    -10,  0,  5, 10, 10,  5,  0,-10,
    -10,  5,  5, 10, 10,  5,  5,-10,
    -10,  0, 10, 10, 10, 10,  0,-10,
    -10, 10, 10, 10, 10, 10, 10,-10,
    -10,  5,  0,  0,  0,  0,  5,-10,
    -20,-10,-10,-10,-10,-10,-10,-20,
]
_PST_ROOK = [
      0,  0,  0,  0,  0,  0,  0,  0,
      5, 10, 10, 10, 10, 10, 10,  5,
     -5,  0,  0,  0,  0,  0,  0, -5,
     -5,  0,  0,  0,  0,  0,  0, -5,
     -5,  0,  0,  0,  0,  0,  0, -5,
     -5,  0,  0,  0,  0,  0,  0, -5,
     -5,  0,  0,  0,  0,  0,  0, -5,
      0,  0,  0,  5,  5,  0,  0,  0,
]
_PST_QUEEN = [
    -20,-10,-10, -5, -5,-10,-10,-20,
    -10,  0,  0,  0,  0,  0,  0,-10,
    -10,  0,  5,  5,  5,  5,  0,-10,
     -5,  0,  5,  5,  5,  5,  0, -5,
      0,  0,  5,  5,  5,  5,  0, -5,
    -10,  5,  5,  5,  5,  5,  0,-10,
    -10,  0,  5,  0,  0,  0,  0,-10,
    -20,-10,-10, -5, -5,-10,-10,-20,
]
_PST_KING_MID = [
    -30,-40,-40,-50,-50,-40,-40,-30,
    -30,-40,-40,-50,-50,-40,-40,-30,
    -30,-40,-40,-50,-50,-40,-40,-30,
    -30,-40,-40,-50,-50,-40,-40,-30,
    -20,-30,-30,-40,-40,-30,-30,-20,
    -10,-20,-20,-20,-20,-20,-20,-10,
     20, 20,  0,  0,  0,  0, 20, 20,
     20, 30, 10,  0,  0, 10, 30, 20,
]
_PST_KING_END = [
    -50,-40,-30,-20,-20,-30,-40,-50,
    -30,-20,-10,  0,  0,-10,-20,-30,
    -30,-10, 20, 30, 30, 20,-10,-30,
    -30,-10, 30, 40, 40, 30,-10,-30,
    -30,-10, 30, 40, 40, 30,-10,-30,
    -30,-10, 20, 30, 30, 20,-10,-30,
    -30,-30,  0,  0,  0,  0,-30,-30,
    -50,-30,-30,-30,-30,-30,-30,-50,
]
_PST = {
    chess.PAWN: _PST_PAWN,
    chess.KNIGHT: _PST_KNIGHT,
    chess.BISHOP: _PST_BISHOP,
    chess.ROOK: _PST_ROOK,
    chess.QUEEN: _PST_QUEEN,
}


@dataclass(frozen=True)
class Level:
    key: str
    label: str
    blurb: str
    max_depth: int
    budget_s: float
    quiescence: bool
    blunder: float  # chance of an outright mistake (uniform random legal move)
    slack: int      # centipawns below best still considered "acceptable"


# Ordered weakest → strongest; the frontend renders them in this order.
LEVELS: dict[str, Level] = {
    "starter": Level(
        key="starter", label="Starter",
        blurb="Sees one move ahead. Will miss tactics and hang pieces.",
        max_depth=1, budget_s=0.10, quiescence=False, blunder=0.40, slack=250,
    ),
    "mid": Level(
        key="mid", label="Mid",
        blurb="Spots simple captures and one-move threats.",
        max_depth=2, budget_s=0.30, quiescence=False, blunder=0.20, slack=140,
    ),
    "main": Level(
        key="main", label="Main",
        blurb="Searches three ply with quiescence. Punishes loose pieces.",
        max_depth=3, budget_s=0.80, quiescence=True, blunder=0.08, slack=70,
    ),
    "pro": Level(
        key="pro", label="Pro",
        blurb="Deeper search, rarely blunders, plays real tactics.",
        max_depth=5, budget_s=1.80, quiescence=True, blunder=0.02, slack=30,
    ),
    "grandmaster": Level(
        key="grandmaster", label="Grandmaster",
        blurb="Full strength — every move searched as deep as time allows.",
        max_depth=7, budget_s=3.50, quiescence=True, blunder=0.0, slack=0,
    ),
}

DEFAULT_LEVEL = "mid"


def level_list() -> list[dict]:
    """Levels as plain dicts for the API/UI, weakest first."""
    return [
        {"key": lv.key, "label": lv.label, "blurb": lv.blurb}
        for lv in LEVELS.values()
    ]


def resolve_level(key: str | None) -> Level:
    return LEVELS.get((key or "").strip().lower(), LEVELS[DEFAULT_LEVEL])


class _Timeout(Exception):
    """Raised to unwind the search once the wall-clock budget is spent."""


def _is_endgame(board: chess.Board) -> bool:
    """Cheap endgame test: no queens, or very little material besides them."""
    if not board.pieces(chess.QUEEN, chess.WHITE) and not board.pieces(chess.QUEEN, chess.BLACK):
        return True
    heavy = 0
    for pt in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN):
        heavy += len(board.pieces(pt, chess.WHITE)) + len(board.pieces(pt, chess.BLACK))
    return heavy <= 4


def evaluate(board: chess.Board) -> int:
    """Static evaluation in centipawns, from the side-to-move's point of view
    (negamax convention). Material + piece-square tables + a small mobility
    nudge — enough to play sensibly, deliberately not more."""
    score = 0
    endgame = _is_endgame(board)
    for square, piece in board.piece_map().items():
        val = PIECE_VALUE[piece.piece_type]
        if piece.piece_type == chess.KING:
            table = _PST_KING_END if endgame else _PST_KING_MID
        else:
            table = _PST[piece.piece_type]
        idx = chess.square_mirror(square) if piece.color == chess.WHITE else square
        val += table[idx]
        score += val if piece.color == chess.WHITE else -val

    # Bishop pair is worth a little more than the sum of its parts.
    if len(board.pieces(chess.BISHOP, chess.WHITE)) >= 2:
        score += 30
    if len(board.pieces(chess.BISHOP, chess.BLACK)) >= 2:
        score -= 30

    return score if board.turn == chess.WHITE else -score


def _mvv_lva(board: chess.Board, move: chess.Move) -> int:
    """Most-valuable-victim / least-valuable-attacker ordering key."""
    if board.is_en_passant(move):
        return 100 * PIECE_VALUE[chess.PAWN] - PIECE_VALUE[chess.PAWN]
    victim = board.piece_type_at(move.to_square)
    if victim is None:
        return 0
    attacker = board.piece_type_at(move.from_square) or chess.PAWN
    return 100 * PIECE_VALUE[victim] - PIECE_VALUE[attacker]


def _order(board: chess.Board, moves: list[chess.Move], tt_move: chess.Move | None) -> list[chess.Move]:
    def key(m: chess.Move) -> int:
        if tt_move is not None and m == tt_move:
            return 1 << 20
        k = _mvv_lva(board, m)
        if m.promotion:
            k += 10 * PIECE_VALUE.get(m.promotion, 0)
        return k
    return sorted(moves, key=key, reverse=True)


class _Search:
    def __init__(self, level: Level, deadline: float):
        self.level = level
        self.deadline = deadline
        self.tt: dict[int, tuple[int, int, int, chess.Move | None]] = {}
        self.nodes = 0

    def _tick(self) -> None:
        self.nodes += 1
        # Checking the clock every node is itself measurable; every 1024 is plenty.
        if self.nodes & 1023 == 0 and time.monotonic() > self.deadline:
            raise _Timeout

    def quiesce(self, board: chess.Board, alpha: int, beta: int, ply: int) -> int:
        """Search only forcing captures past the horizon, so the engine doesn't
        stop counting halfway through a trade and think it's winning."""
        self._tick()
        stand_pat = evaluate(board)
        if stand_pat >= beta:
            return beta
        alpha = max(alpha, stand_pat)

        captures = [m for m in board.legal_moves if board.is_capture(m) or m.promotion]
        for move in _order(board, captures, None):
            board.push(move)
            try:
                score = -self.quiesce(board, -beta, -alpha, ply + 1)
            finally:
                board.pop()
            if score >= beta:
                return beta
            alpha = max(alpha, score)
        return alpha

    def negamax(self, board: chess.Board, depth: int, alpha: int, beta: int, ply: int) -> int:
        self._tick()

        if board.is_checkmate():
            return -MATE + ply          # prefer mating sooner / being mated later
        if board.is_stalemate() or board.is_insufficient_material() or board.can_claim_threefold_repetition():
            return 0

        alpha_orig = alpha
        key = chess.polyglot.zobrist_hash(board)
        hit = self.tt.get(key)
        tt_move = None
        if hit is not None:
            h_depth, h_score, h_flag, h_move = hit
            tt_move = h_move
            if h_depth >= depth:
                if h_flag == 0:
                    return h_score
                if h_flag < 0:
                    alpha = max(alpha, h_score)
                else:
                    beta = min(beta, h_score)
                if alpha >= beta:
                    return h_score

        if depth <= 0:
            return self.quiesce(board, alpha, beta, ply) if self.level.quiescence else evaluate(board)

        best = -_INF
        best_move = None
        for move in _order(board, list(board.legal_moves), tt_move):
            board.push(move)
            try:
                score = -self.negamax(board, depth - 1, -beta, -alpha, ply + 1)
            finally:
                board.pop()
            if score > best:
                best, best_move = score, move
            alpha = max(alpha, score)
            if alpha >= beta:
                break

        flag = 0 if alpha_orig < best < beta else (-1 if best >= beta else 1)
        self.tt[key] = (depth, best, flag, best_move)
        return best

    def root_scores(self, board: chess.Board, depth: int) -> list[tuple[chess.Move, int]]:
        """Full-width root search — every legal move gets a score, which is what
        the slack/blunder move-choice below needs."""
        out: list[tuple[chess.Move, int]] = []
        alpha = -_INF
        for move in _order(board, list(board.legal_moves), None):
            board.push(move)
            try:
                score = -self.negamax(board, depth - 1, -_INF, _INF, 1)
            finally:
                board.pop()
            out.append((move, score))
            alpha = max(alpha, score)
        out.sort(key=lambda ms: ms[1], reverse=True)
        return out


def best_move(fen: str, level_key: str | None = None) -> str | None:
    """Pick the bot's move. Returns UCI, or None if the game is already over."""
    board = chess.Board(fen)
    moves = list(board.legal_moves)
    if not moves:
        return None
    if len(moves) == 1:
        return moves[0].uci()

    level = resolve_level(level_key)

    # An outright mistake, at the configured rate. Kept genuinely uniform so the
    # weak tiers are actually beatable by a beginner.
    if level.blunder and random.random() < level.blunder:
        return random.choice(moves).uci()

    search = _Search(level, deadline=time.monotonic() + level.budget_s)
    scored: list[tuple[chess.Move, int]] = []
    try:
        # Iterative deepening: each pass seeds the transposition table for the
        # next, and the budget can expire mid-pass without losing a usable result.
        for depth in range(1, level.max_depth + 1):
            scored = search.root_scores(board, depth)
    except _Timeout:
        pass

    if not scored:
        return random.choice(moves).uci()

    best_score = scored[0][1]
    pool = [m for m, s in scored if s >= best_score - level.slack] or [scored[0][0]]
    return random.choice(pool).uci()
