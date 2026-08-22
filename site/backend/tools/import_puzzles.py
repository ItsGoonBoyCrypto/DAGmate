"""Import the Lichess open puzzle database into a local SQLite file.

Source: https://database.lichess.org/lichess_db_puzzle.csv.zst  (CC0 1.0 —
public domain, no attribution or share-alike obligations; safe for a closed
commercial product. See memory/dagmate_licensing.md.)

The download is ~290 MB compressed and ~1.5 GB as CSV, so this streams straight
from HTTP through the zstd decompressor into SQLite — the full CSV never
touches disk.

Output goes to its own DB (state/puzzles.db), not the app DB: it's a
rebuildable content artefact, and keeping it separate means re-importing can
never put user accounts or match state at risk.

Usage:
    python tools/import_puzzles.py                # download + import
    python tools/import_puzzles.py --limit 50000  # quick smoke run
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sqlite3
import sys
import time
import urllib.request

import zstandard

URL = "https://database.lichess.org/lichess_db_puzzle.csv.zst"
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "state", "puzzles.db")

# Lichess rates puzzles with Glicko-2, so RatingDeviation says how much to trust
# the number. A puzzle nobody has solved carries a huge RD and its rating is
# close to meaningless — useless for a difficulty ladder. Popularity is
# 100*(up-down)/(up+down); low scores flag ambiguous or unpleasant puzzles.
MIN_POPULARITY = 90
MIN_PLAYS = 100
MAX_RATING_DEVIATION = 80

SCHEMA = """
CREATE TABLE puzzles (
    id          TEXT PRIMARY KEY,
    fen         TEXT NOT NULL,
    moves       TEXT NOT NULL,
    rating      INTEGER NOT NULL,
    popularity  INTEGER NOT NULL,
    nb_plays    INTEGER NOT NULL,
    themes      TEXT NOT NULL,
    game_url    TEXT,
    opening     TEXT
);
CREATE TABLE puzzle_themes (
    puzzle_id   TEXT NOT NULL,
    theme       TEXT NOT NULL
);
"""

INDEXES = """
CREATE INDEX idx_puzzles_rating ON puzzles(rating);
CREATE INDEX idx_themes_theme_puzzle ON puzzle_themes(theme, puzzle_id);
CREATE INDEX idx_themes_puzzle ON puzzle_themes(puzzle_id);
"""


def open_stream(url: str):
    """Yield decoded CSV lines straight from the network, decompressing as we go."""
    resp = urllib.request.urlopen(url)
    reader = zstandard.ZstdDecompressor().stream_reader(resp)
    return io.TextIOWrapper(reader, encoding="utf-8", newline="")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="stop after N kept puzzles (smoke test)")
    ap.add_argument("--url", default=URL)
    args = ap.parse_args()

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    # Bulk-load settings. Safe to be reckless: this DB is disposable and is
    # rebuilt from scratch by re-running the script.
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")

    started = time.monotonic()
    seen = kept = 0
    rows: list[tuple] = []
    theme_rows: list[tuple] = []

    with open_stream(args.url) as text:
        for rec in csv.DictReader(text):
            seen += 1
            try:
                rating = int(rec["Rating"])
                rd = int(rec["RatingDeviation"])
                pop = int(rec["Popularity"])
                plays = int(rec["NbPlays"])
            except (ValueError, KeyError, TypeError):
                continue
            if pop < MIN_POPULARITY or plays < MIN_PLAYS or rd > MAX_RATING_DEVIATION:
                continue

            pid = rec["PuzzleId"]
            themes = (rec.get("Themes") or "").split()
            rows.append((pid, rec["FEN"], rec["Moves"], rating, pop, plays,
                         " ".join(themes), rec.get("GameUrl"), rec.get("OpeningTags") or None))
            theme_rows.extend((pid, t) for t in themes)
            kept += 1

            if len(rows) >= 20000:
                _flush(con, rows, theme_rows)
                rows, theme_rows = [], []
                print(f"  {seen:>9,} read  {kept:>8,} kept  "
                      f"{time.monotonic() - started:6.0f}s", flush=True)
            if args.limit and kept >= args.limit:
                break

    _flush(con, rows, theme_rows)
    print("building indexes…", flush=True)
    con.executescript(INDEXES)
    con.commit()

    lo, hi = con.execute("SELECT MIN(rating), MAX(rating) FROM puzzles").fetchone()
    themes = con.execute("SELECT COUNT(DISTINCT theme) FROM puzzle_themes").fetchone()[0]
    con.close()

    size_mb = os.path.getsize(DB_PATH) / 1e6
    print(f"\ndone — {kept:,} puzzles kept of {seen:,} read ({kept / max(seen, 1):.1%})")
    print(f"rating {lo}–{hi} · {themes} themes · {size_mb:.0f} MB · "
          f"{time.monotonic() - started:.0f}s")
    print(DB_PATH)
    return 0


def _flush(con, rows, theme_rows):
    if rows:
        con.executemany("INSERT OR REPLACE INTO puzzles VALUES (?,?,?,?,?,?,?,?,?)", rows)
    if theme_rows:
        con.executemany("INSERT INTO puzzle_themes VALUES (?,?)", theme_rows)
    con.commit()


if __name__ == "__main__":
    sys.exit(main())
