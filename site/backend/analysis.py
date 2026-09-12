"""Engine-cheat analysis (integrity Phase 2 Part B).

Given a finished game, score how engine-like each player's moves were. The score is a FLAG that HOLDS a
staked pot for human review — never a verdict. A single game is weak evidence (a strong player has strong
games); the real signal accrues per wallet over many games, and a human always decides the consequence.
See docs/DAGMATE_INTEGRITY.md.

`analyze_game` is engine-agnostic: it takes an `evaluate(board, multipv) -> [(uci, cp_from_mover_pov), ...]`
callback (best move first), so it's fully unit-testable with a mock engine. `run_and_record` wires the real
Stockfish (via python-chess) and stores the result, holding the match if the WINNER's score crosses the
threshold. It is best-effort and FAILS OPEN: any error still stamps the match analysed so the payout is
never bricked by an analysis glitch.

The per-game score is a transparent, tunable heuristic (NOT a trained model like Lichess's Irwin — we have
no labelled data): it combines engine top-1 match rate, average win-probability loss, the consistency
(variance) of that loss, and — if move times are present — how often "hard" positions were answered
instantly. Forced/only-move and already-decided positions are discounted or skipped, because matching the
engine there is not evidence of anything.
"""
from __future__ import annotations
import json
import logging
import threading
import time

import chess

import config
import chess_logic
import database as db

log = logging.getLogger("dagmate.analysis")

_running: set[str] = set()          # match ids currently being analysed, so a poll can't start it twice
_running_lock = threading.Lock()

# Scoring constants — the knobs. Documented so a reviewer can reason about a score.
_LOSS_SCALE = 0.06   # avg win-prob loss that maps to "clearly human" (strong humans ~0.03–0.05, engines ~0)
_VAR_SCALE = 0.004   # win-prob-loss variance that maps to "clearly human" (engines are unnaturally steady)
_FORCED_GAP = 0.40   # a best-vs-2nd win-prob gap this large = a forced/only-move position → discounted
_DECIDED_CP = 700    # |eval| above this = the game is already decided → the ply carries little signal
_NONMATCH_LOSS = _LOSS_SCALE   # loss charged to a move outside the top-K (clearly not the engine's pick)


def _wp(cp: int) -> float:
    """Win probability for the side to move, from a centipawn eval (logistic, clamped for mate scores)."""
    cp = max(-1000, min(1000, cp))
    return 1.0 / (1.0 + 10 ** (-cp / 400.0))


def analyze_game(evaluate, moves_uci: list[str], move_times_ms: list[int] | None,
                 start_fen: str = None) -> dict:
    """Return {'white': side, 'black': side} where side = {score|None, ...features}. score is None when a
    colour has too few decision plies to judge (a short game is not evidence)."""
    board = chess.Board(start_fen or chess_logic.STARTING_FEN)
    times = move_times_ms or []
    # Per-colour accumulators of decision plies.
    acc = {chess.WHITE: [], chess.BLACK: []}

    for i, uci in enumerate(moves_uci):
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            break
        if move not in board.legal_moves:
            break
        mover = board.turn
        decision = (i >= config.ANALYSIS_SKIP_OPENING_PLIES
                    and board.legal_moves.count() > 1)
        if decision:
            pvs = evaluate(board, config.ANALYSIS_MULTIPV)   # [(uci, cp_mover_pov)], best first
            if pvs:
                best_uci, best_cp = pvs[0]
                best_wp = _wp(best_cp)
                if abs(best_cp) <= _DECIDED_CP and best_wp >= 0.05:   # skip decided / lost positions
                    gap = best_wp - _wp(pvs[1][1]) if len(pvs) > 1 else 1.0
                    weight = max(0.15, 1.0 - gap / _FORCED_GAP)       # forced position → low weight
                    played = dict(pvs).get(uci)
                    wploss = max(0.0, best_wp - _wp(played)) if played is not None else _NONMATCH_LOSS
                    t = times[i] if i < len(times) else None
                    acc[mover].append({
                        "top1": 1.0 if uci == best_uci else 0.0,
                        "wploss": wploss, "weight": weight, "gap": gap, "t": t,
                    })
        board.push(move)

    return {"white": _score_side(acc[chess.WHITE]), "black": _score_side(acc[chess.BLACK])}


def _score_side(plies: list[dict]) -> dict:
    n = len(plies)
    if n < config.ANALYSIS_MIN_DECISION_PLIES:
        return {"score": None, "decisionPlies": n}
    W = sum(p["weight"] for p in plies) or 1.0
    top1_rate = sum(p["top1"] * p["weight"] for p in plies) / W
    avg_loss = sum(p["wploss"] * p["weight"] for p in plies) / W
    var_loss = sum(p["weight"] * (p["wploss"] - avg_loss) ** 2 for p in plies) / W

    # Timing: among "hard" plies (many real candidates → low gap → high weight), how many were answered
    # near-instantly? Engine users play only-moves as fast as recaptures. Neutral (0.5) when no clock data.
    hard = [p for p in plies if p["weight"] >= 0.7 and p["t"] is not None and p["t"] > 0]
    if hard and config.ANALYSIS_FAST_MOVE_MS > 0:
        s_time = sum(1 for p in hard if p["t"] < config.ANALYSIS_FAST_MOVE_MS) / len(hard)
    else:
        s_time = 0.5

    s_match = top1_rate
    s_acc = 1.0 - min(1.0, avg_loss / _LOSS_SCALE)
    s_consist = 1.0 - min(1.0, var_loss / _VAR_SCALE)
    score = 0.4 * s_match + 0.3 * s_acc + 0.2 * s_consist + 0.1 * s_time
    return {
        "score": round(score, 4), "decisionPlies": n,
        "top1Rate": round(top1_rate, 4), "avgWpLoss": round(avg_loss, 4),
        "wpLossVar": round(var_loss, 6), "fastHard": round(s_time, 4),
    }


def _winner_color(m: dict) -> str | None:
    if not m["winner_account_id"]:
        return None
    return "white" if m["winner_account_id"] == m["player_a_account_id"] else "black"


def _stockfish_evaluate(engine):
    """Adapt python-chess's SimpleEngine into the evaluate(board, multipv) callback analyze_game wants."""
    def evaluate(board, multipv):
        info = engine.analyse(board, chess.engine.Limit(nodes=config.ANALYSIS_NODES), multipv=multipv)
        out = []
        for entry in info:
            pv = entry.get("pv")
            if not pv:
                continue
            cp = entry["score"].pov(board.turn).score(mate_score=100000)
            out.append((pv[0].uci(), int(cp)))
        out.sort(key=lambda x: x[1], reverse=True)   # best (mover POV) first
        return out
    return evaluate


def run_and_record(match_id: str) -> None:
    """Analyse a finished staked match with Stockfish and store the result, HOLDING the pot if the winner's
    score crosses the threshold. Best-effort + fail-open: on ANY error the match is still stamped analysed
    (hold=False) so its payout proceeds normally. Blocking — call in a background thread."""
    import chess.engine  # local import: only the runner needs the engine, not the pure scorer
    try:
        m = db.get_match(match_id)
        if not m or m["analyzed_ts"] or (m["stake_sompi"] or 0) <= 0:
            return
        moves = json.loads(m["moves_json"] or "[]")
        times = json.loads(m["move_times_json"] or "[]")
        engine = chess.engine.SimpleEngine.popen_uci(config.STOCKFISH_PATH)
        try:
            res = analyze_game(_stockfish_evaluate(engine), moves, times)
        finally:
            engine.quit()

        wc = _winner_color(m)
        if wc:                                  # a cheating winner is what takes the pot
            score = res[wc]["score"]
        else:                                   # a draw — either side crossing the bar is worth a look
            scores = [res[c]["score"] for c in ("white", "black") if res[c]["score"] is not None]
            score = max(scores) if scores else None
        hold = score is not None and score >= config.CHEAT_HOLD_THRESHOLD
        held = db.record_analysis(match_id, score if score is not None else -1.0, json.dumps(res), hold=hold)
        log.info("analysed %s: score=%s hold=%s(%s)", match_id, score, hold, held)
    except Exception as e:
        # Fail OPEN — never let an analysis failure brick a payout. Stamp it analysed so the gate releases.
        log.warning("analysis failed for %s, failing open: %s", match_id, e)
        try:
            db.record_analysis(match_id, -1.0, json.dumps({"error": str(e)}), hold=False)
        except Exception:
            pass


def is_awaiting_analysis(m: dict) -> bool:
    """True while a staked match's payout should WAIT for its analysis — analysis is on, the game just
    finished, and it hasn't been analysed or held yet. After the window it returns False (fail open)."""
    if not config.ANALYSIS_ENABLED or (m["stake_sompi"] or 0) <= 0:
        return False
    if m["status"] != "settled" or m["analyzed_ts"] or m["review_status"] or m["settle_txid"]:
        return False
    return (int(time.time()) - (m["settled_ts"] or 0)) < config.ANALYSIS_WINDOW_SECS


def maybe_kick(match_id: str, m: dict = None) -> None:
    """Start the analysis in the background if this match is awaiting it and isn't already running. Safe to
    call from anywhere (game-end AND every settle poll), so analysis starts regardless of which path ended
    the game. Deduped, so repeated polls never spawn a second run."""
    if not config.ANALYSIS_ENABLED:
        return
    m = m or db.get_match(match_id)
    if not m or not is_awaiting_analysis(m):
        return
    with _running_lock:
        if match_id in _running:
            return
        _running.add(match_id)

    def _work():
        try:
            run_and_record(match_id)
        finally:
            with _running_lock:
                _running.discard(match_id)
    threading.Thread(target=_work, daemon=True).start()
