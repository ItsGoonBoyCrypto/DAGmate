"""DAGmate — Learn curriculum (docs/DAGMATE_SPEC.md §9).

Basics → grandmaster, six tiers. This module is the single source of truth for
both the level list AND the level content.

⚠️ Content lives server-side deliberately. It used to sit in a `LEARN_CONTENT`
object in `js/app.js`, which meant every paid level was readable via View Source
— the unlock gate was decorative. `content_for()` is the only way out, and it
refuses to return a body for a level the account hasn't unlocked.

All prose here is original. Where a concept has a standard name (Lucena,
Philidor, Zugzwang) the name is used as-is — those are terminology, not
copyrightable text — but no wording is lifted from any chess book or site.
"""
from __future__ import annotations

# Tiers, weakest → strongest. `gas_kas` is a one-way "gas" purchase per level:
# no escrow, no wager (spec §9). The first two tiers are free so a complete
# beginner can get to a playable understanding without paying anything.
TIERS = [
    {"key": "basics", "label": "Basics", "blurb": "Never played before? Start here.", "gas_kas": 0},
    {"key": "fundamentals", "label": "Fundamentals", "blurb": "The habits that stop you losing games.", "gas_kas": 0},
    {"key": "intermediate", "label": "Intermediate", "blurb": "Real tactics, real endgames.", "gas_kas": 1},
    {"key": "advanced", "label": "Advanced", "blurb": "Positional understanding and planning.", "gas_kas": 2},
    {"key": "expert", "label": "Expert", "blurb": "Calculation, dynamics and prophylaxis.", "gas_kas": 3},
    {"key": "master", "label": "Master", "blurb": "Deep theory and the classical canon.", "gas_kas": 5},
]

TIER_ORDER = [t["key"] for t in TIERS]


def _lv(id, tier, title, summary, body):
    return {"id": id, "tier": tier, "title": title, "summary": summary, "body": body}


LEVELS = [
    # ── BASICS ──────────────────────────────────────────────────────────
    _lv("basics-1", "basics", "The board and the pieces",
        "Set up correctly and learn what each piece is worth.",
        """<p>The board is 8×8. Set it up with a <b>light square in each player's
        bottom-right corner</b> — "white on the right". The queen starts on her
        own colour: white queen on a light square, black queen on a dark one.</p>
        <p>Rough values, in "pawns":</p>
        <ul>
          <li>Pawn — 1</li><li>Knight — 3</li><li>Bishop — 3</li>
          <li>Rook — 5</li><li>Queen — 9</li><li>King — priceless (the game ends when it's trapped)</li>
        </ul>
        <p>These are guidelines, not laws. A knight buried on the edge can be
        worth less than a pawn about to promote. But until you have a reason to
        think otherwise, don't trade a rook for a bishop.</p>"""),

    _lv("basics-2", "basics", "How the pieces move",
        "Movement rules for all six pieces.",
        """<ul>
          <li><b>Pawn</b> — one square forward, or two from its starting square.
          Captures <i>diagonally</i> forward, which is the part beginners forget.
          It can never move backwards.</li>
          <li><b>Knight</b> — an "L": two squares one way, one square
          perpendicular. It is the only piece that jumps over others.</li>
          <li><b>Bishop</b> — any distance diagonally. Each bishop is stuck on
          one colour for the entire game.</li>
          <li><b>Rook</b> — any distance in straight lines.</li>
          <li><b>Queen</b> — rook and bishop combined.</li>
          <li><b>King</b> — one square in any direction. It may never move onto
          a square attacked by an enemy piece.</li>
        </ul>
        <p>Sliding pieces (bishop, rook, queen) are blocked by anything in the
        way and cannot jump.</p>"""),

    _lv("basics-3", "basics", "Special moves: castling, en passant, promotion",
        "The three rules that surprise new players.",
        """<p><b>Castling</b> moves the king two squares toward a rook, and the
        rook hops over to the far side. It's legal only if: neither piece has
        moved, the squares between are empty, and the king is not in check, does
        not pass through an attacked square, and does not land on one. It's
        usually the fastest way to make your king safe.</p>
        <p><b>En passant</b>: if an enemy pawn uses its two-square move to land
        directly beside your pawn, you may capture it as though it had only
        moved one — but <i>only</i> on the very next move. The right expires
        immediately.</p>
        <p><b>Promotion</b>: a pawn reaching the far rank becomes any piece you
        choose except a king. Almost always take the queen. Occasionally a
        knight is better, because a knight can give check on squares a queen
        cannot reach.</p>"""),

    _lv("basics-4", "basics", "Check, checkmate and stalemate",
        "How games actually end.",
        """<p><b>Check</b> — your king is attacked. You must respond immediately,
        in one of exactly three ways: move the king, capture the attacker, or
        block the line. If none is possible, it's checkmate.</p>
        <p><b>Checkmate</b> — the king is in check and there is no legal escape.
        The game ends at once. The king is never actually captured.</p>
        <p><b>Stalemate</b> — the side to move has <i>no legal move at all</i>
        and is <i>not</i> in check. This is a <b>draw</b>, and it is the single
        most common way a winning player throws away a point. When you are far
        ahead, always check that your opponent still has a move.</p>
        <p>Other draws: insufficient material (e.g. king and bishop vs king),
        the 50-move rule, and threefold repetition.</p>"""),

    _lv("basics-5", "basics", "Reading notation",
        "Understand any chess book, video or game record.",
        """<p>Files are lettered <code>a</code>–<code>h</code> left to right from
        White's side; ranks are numbered <code>1</code>–<code>8</code> from
        White's side. Every square has a unique name like <code>e4</code>.</p>
        <p>Pieces: <code>K</code>ing, <code>Q</code>ueen, <code>R</code>ook,
        <code>B</code>ishop, <code>N</code>ight (knight). Pawns get no letter.</p>
        <ul>
          <li><code>e4</code> — a pawn to e4</li>
          <li><code>Nf3</code> — knight to f3</li>
          <li><code>Bxc6</code> — bishop captures on c6</li>
          <li><code>O-O</code> / <code>O-O-O</code> — castling short / long</li>
          <li><code>+</code> check, <code>#</code> checkmate</li>
          <li><code>e8=Q</code> — pawn promotes to a queen</li>
        </ul>
        <p>You'll also meet <b>FEN</b>, a one-line snapshot of an entire
        position. DAGmate stores every game as FEN plus a list of moves.</p>"""),

    # ── FUNDAMENTALS ────────────────────────────────────────────────────
    _lv("fund-1", "fundamentals", "Opening principles",
        "Three rules that cover almost every opening.",
        """<p>You do not need memorised opening lines to play a good opening. You
        need three ideas:</p>
        <ol>
          <li><b>Fight for the centre.</b> Pawns on e4/d4 (or e5/d5) take space
          and open lines for your pieces.</li>
          <li><b>Develop every piece, once.</b> Knights and bishops off the back
          rank before you move the same piece twice. Don't bring the queen out
          early — it just gets chased around while your opponent develops.</li>
          <li><b>Castle early.</b> A king in the centre when the position opens
          is how short games are lost.</li>
        </ol>
        <p>A useful self-check around move 10: how many pieces have I developed,
        and is my king safe? If the answer is "two" and "no", fix that before
        starting anything clever.</p>"""),

    _lv("fund-2", "fundamentals", "Basic tactics: forks and pins",
        "The two motifs that win the most material.",
        """<p>A <b>fork</b> is one piece attacking two targets at once. Knights
        are the classic forker because their movement is so hard to see — a
        knight forking king and queen wins the queen outright. Pawns fork
        brutally well too, and are the cheapest attacker on the board.</p>
        <p>A <b>pin</b> immobilises a piece because moving it would expose
        something more valuable behind it. An <i>absolute</i> pin is against the
        king and the pinned piece legally cannot move. A <i>relative</i> pin is
        against a queen or rook — moving is legal but expensive.</p>
        <p>The follow-up matters more than the pin: once a piece is pinned it
        cannot defend anything, so <b>pile more attackers onto it</b>. A pinned
        knight attacked twice is simply lost.</p>"""),

    _lv("fund-3", "fundamentals", "Basic tactics: skewers, discoveries, deflection",
        "Three more motifs, and the pattern behind all of them.",
        """<p><b>Skewer</b> — a pin in reverse. Attack a valuable piece; when it
        moves, take what was behind it.</p>
        <p><b>Discovered attack</b> — move one piece and reveal an attack from
        another behind it. Devastating because two threats appear at once, and
        the moving piece can make a threat of its own. <b>Discovered check</b>
        is the strongest version: the opponent must answer the check, so your
        moved piece can grab almost anything.</p>
        <p><b>Deflection</b> — force a defender away from what it is guarding,
        usually by attacking something it values more.</p>
        <p>The common thread: <b>every tactic is a double threat.</b> One threat
        gets parried; two at once cannot both be. When hunting for a tactic,
        look for undefended pieces, exposed kings, and pieces on the same line.</p>"""),

    _lv("fund-4", "fundamentals", "Checkmate patterns you must know",
        "Finish the game when you're winning.",
        """<p><b>Back-rank mate</b> — a rook or queen lands on the back rank
        where the king is trapped behind its own unmoved pawns. Prevent it by
        giving your king an escape square ("luft") <i>before</i> it's a problem.</p>
        <p><b>Smothered mate</b> — a knight mates a king completely boxed in by
        its own pieces. The classic sequence uses a queen sacrifice to force the
        king's own rook into the last free square.</p>
        <p><b>Queen + king vs lone king</b> — walk the enemy king to the edge
        using the queen a knight's-move away, then bring your king up and mate.
        Watch for stalemate.</p>
        <p><b>Two rooks</b> — a "staircase": each rook cuts off a rank, and they
        alternate driving the king backwards.</p>"""),

    _lv("fund-5", "fundamentals", "Why you're losing pieces",
        "A concrete method to stop hanging material.",
        """<p>At beginner and club level, the overwhelming majority of games are
        decided by someone leaving a piece <i>en prise</i> — not by deep strategy.
        Fixing this is worth more than any opening study.</p>
        <p>Before every single move, run this check:</p>
        <ol>
          <li><b>What did my opponent's last move attack?</b> Every move creates
          a threat; find it before you do anything else.</li>
          <li><b>What is undefended?</b> On both sides. Loose pieces are what
          tactics feed on.</li>
          <li><b>Am I hanging anything after my intended move?</b> Play it in
          your head, then look again — especially at the square you just left.</li>
          <li><b>Checks, captures, threats.</b> Scan all three, for both sides.</li>
        </ol>
        <p>It's slow at first and becomes automatic. Practise it against the
        Starter and Mid bots, where mistakes are affordable.</p>"""),

    # ── INTERMEDIATE ────────────────────────────────────────────────────
    _lv("inter-1", "intermediate", "Pawn structure",
        "Pawns are the skeleton — everything else hangs off them.",
        """<p>Pawns are the only pieces that cannot retreat, so pawn moves are
        permanent. Structure decides where your pieces belong.</p>
        <ul>
          <li><b>Isolated pawn</b> — no friendly pawn on either adjacent file.
          It can't be defended by a pawn, so it needs pieces. In return it
          controls key squares and opens lines — dynamic compensation.</li>
          <li><b>Doubled pawns</b> — two on one file. Usually a weakness, but
          they open a file for a rook.</li>
          <li><b>Backward pawn</b> — stuck behind its neighbours on a
          half-open file. A long-term target.</li>
          <li><b>Passed pawn</b> — no enemy pawn can stop it. In endgames this
          often decides the game outright.</li>
        </ul>
        <p>The general rule: <b>attack where your pawns point.</b> A pawn chain
        aimed at the kingside says your play is on the kingside.</p>"""),

    _lv("inter-2", "intermediate", "Good pieces and bad pieces",
        "Not all bishops and knights are worth three.",
        """<p><b>Bishop vs knight</b>: bishops are stronger in open positions
        with pawns on both wings, because they act at range. Knights are
        stronger in closed, blocked positions and where there are strong
        outposts. The <b>bishop pair</b> in an open position is a real,
        lasting advantage — usually reckoned at about half a pawn.</p>
        <p>A <b>bad bishop</b> is one hemmed in by its own pawns on its colour.
        Either trade it or move the pawns.</p>
        <p>An <b>outpost</b> is a square that cannot be attacked by an enemy
        pawn, ideally supported by one of yours. A knight on a central outpost
        can be worth more than a rook.</p>
        <p><b>Rooks</b> want open files and the 7th rank. Two rooks on the 7th
        are often winning by themselves.</p>"""),

    _lv("inter-3", "intermediate", "King and pawn endgames",
        "Opposition, key squares, and the square of the pawn.",
        """<p>King and pawn endgames are pure calculation — no compensation, no
        bluffing. They are also the foundation for every other endgame, because
        most endgames simplify into one.</p>
        <p><b>The square rule</b>: to know if a lone king catches a passed pawn,
        draw a square with the pawn's path as one side. If the king can step
        into that square, it catches the pawn. No counting needed.</p>
        <p><b>Opposition</b>: kings facing each other with one square between.
        Whoever is <i>not</i> to move holds the opposition and effectively
        controls the other king. Winning many pawn endgames is precisely a fight
        to seize the opposition.</p>
        <p><b>Key squares</b>: for a pawn on the 5th rank or below, the three
        squares two ranks ahead are key — get your king onto one and the pawn
        promotes regardless of whose move it is.</p>"""),

    _lv("inter-4", "intermediate", "Rook endgames: Lucena and Philidor",
        "The two positions every strong player knows cold.",
        """<p>Rook endgames are the most common endgame in chess. Two positions
        cover an enormous share of them.</p>
        <p><b>The Lucena position</b> (winning): you have a pawn on the 7th, your
        king is in front of it, and the enemy rook is checking from the side. The
        technique is <b>"building a bridge"</b> — put your rook on the 4th rank,
        walk your king out toward the checks, and when the checks come, interpose
        your rook. That interposition is the whole point.</p>
        <p><b>The Philidor position</b> (drawing): you're defending against a
        pawn that hasn't reached the 6th yet. Put your rook on your <b>third
        rank</b> and simply hold it there, denying the enemy king entry. The
        moment their pawn advances to the 6th, drop your rook to the <b>eighth</b>
        and check from behind forever.</p>
        <p>Also remember: <b>rooks belong behind passed pawns</b> — yours and
        theirs.</p>"""),

    _lv("inter-5", "intermediate", "Calculation technique",
        "How to look ahead without getting lost.",
        """<p>Strong players don't calculate everything — they calculate the
        right things, in order.</p>
        <ol>
          <li><b>List candidate moves</b> first. Two to four, no more. Most
          blunders come from never considering the right move at all, not from
          miscalculating the ones you saw.</li>
          <li><b>Checks, captures, threats</b> — forcing moves first, because
          they narrow the opponent's replies and are quickest to verify.</li>
          <li><b>One line at a time, to the end.</b> Jumping between lines is
          how you lose your place and double-count.</li>
          <li><b>Stop at a quiet position and evaluate</b> — material, king
          safety, structure, activity. A line is only as good as the position it
          arrives at.</li>
        </ol>
        <p><b>Blunder-check before you move.</b> You have already decided; spend
        five more seconds looking for their best reply. This one habit is worth
        several hundred rating points.</p>"""),

    # ── ADVANCED ────────────────────────────────────────────────────────
    _lv("adv-1", "advanced", "Building a repertoire",
        "Choose openings that fit how you want to play.",
        """<p>A repertoire is a set of openings you know well enough to reach
        positions you understand. Depth beats breadth: <b>one</b> reply to 1.e4
        that you know properly beats five you half-know.</p>
        <p>Decide what you want as White: a space-grabbing 1.d4 game, the sharp
        theory of 1.e4, or a flexible 1.Nf3/1.c4 setup. As Black you need one
        answer to 1.e4 and one to 1.d4 — that's the minimum viable repertoire.</p>
        <p>Study openings by <b>plans and pawn structures</b>, not move lists.
        Memorised moves evaporate the second your opponent deviates; knowing
        that a structure calls for a minority attack survives anything.</p>
        <p>Review your own losses to find where you left theory and what you
        didn't understand. That's your study list — not whatever's fashionable.</p>"""),

    _lv("adv-2", "advanced", "Planning and weak squares",
        "How to find a plan when nothing is forced.",
        """<p>When there's no tactic, you need a plan, and plans come from
        <b>imbalances</b>. Compare: material, pawn structure, piece activity,
        king safety, space, and the minor-piece mix. Whatever is unbalanced is
        where the plan lives.</p>
        <p><b>Weak squares</b> are squares that can never be defended by a pawn
        again. They are permanent. Identify them in both camps: theirs are
        outposts for your pieces, yours need covering by pieces.</p>
        <p>A workable planning method:</p>
        <ol>
          <li>Find your worst-placed piece and improve it. Often the whole plan.</li>
          <li>Identify the enemy's weakest point and aim at it.</li>
          <li>Ask which pawn break opens lines in your favour, and prepare it.</li>
        </ol>
        <p><b>Do not attack without a reason.</b> An attack launched against a
        sound position with no weaknesses just loses material.</p>"""),

    _lv("adv-3", "advanced", "Attacking the king",
        "When an attack is justified, and how to conduct one.",
        """<p>Prerequisites for a sound attack — you usually need at least two:
        a lead in development, more attackers than defenders near the king, open
        or half-open lines toward it, and a stable centre so your opponent can't
        counter there.</p>
        <p><b>Opposite-side castling</b> is the sharpest scenario: both sides
        storm with pawns and speed is everything. Don't waste a single move, and
        don't open lines toward your own king.</p>
        <p>Classic breakthroughs against a castled king include the bishop
        sacrifice on h7 (the "Greek gift"), the exchange sacrifice on the
        long diagonal, and the rook lift to the third rank.</p>
        <p><b>Count attackers and defenders before committing.</b> Three
        attackers against two defenders usually breaks through; two against
        three loses a piece and the game.</p>"""),

    _lv("adv-4", "advanced", "Prophylaxis",
        "Winning by preventing, not by threatening.",
        """<p>Prophylaxis means asking, every move, <b>"what does my opponent
        want to do?"</b> — and then making that impossible. It is the habit that
        most separates strong players from merely tactical ones.</p>
        <p>In practice: before choosing your move, work out their best plan and
        their best move. Then ask whether a quiet move that kills it is worth
        more than your own plan. Very often it is, because a player whose ideas
        keep evaporating drifts into passivity and eventually cracks.</p>
        <p>Typical prophylactic moves: a rook to a file <i>before</i> it opens;
        a pawn move denying a knight its outpost; giving your king luft in a
        calm moment; trading off their best-placed attacker.</p>
        <p>The cost is tempo, so it isn't free — but in closed positions where
        nothing is forcing, it's usually the strongest move on the board.</p>"""),

    _lv("adv-5", "advanced", "Converting an advantage",
        "The hardest skill: finishing won positions.",
        """<p>More points are dropped converting winning positions than reaching
        them. The mindset that won the material is the wrong one for cashing
        it in.</p>
        <ul>
          <li><b>Trade pieces, not pawns, when ahead in material.</b> Every
          trade makes your extra piece proportionally bigger. Ahead in pawns,
          trade pieces and head for the endgame. Keep pawns — you need one to
          promote.</li>
          <li><b>Eliminate counterplay first.</b> A safe king and no enemy
          activity is worth more than a second extra pawn. Spend moves on it.</li>
          <li><b>Don't chase brilliance.</b> The simple move that leaves you two
          pawns up in a dull endgame wins. The flashy sacrifice might not.</li>
          <li><b>Watch for stalemate</b> in simplified positions.</li>
        </ul>
        <p>Ask "what's their counterplay?" before every move once you're
        winning. If the answer is "none", you can take as long as you like.</p>"""),

    # ── EXPERT ──────────────────────────────────────────────────────────
    _lv("exp-1", "expert", "Dynamics vs statics",
        "The central trade-off in modern chess.",
        """<p><b>Static</b> advantages are permanent: pawn structure, weak
        squares, the bishop pair, material. They don't expire, so you can take
        your time and play slowly.</p>
        <p><b>Dynamic</b> advantages are temporary: a development lead, an
        exposed enemy king, a piece momentarily out of play, the initiative.
        They must be used immediately or they vanish.</p>
        <p>Nearly every hard decision is a trade between the two. Accepting
        doubled pawns for the bishop pair and open lines swaps static for
        dynamic. A sacrifice spends material for time.</p>
        <p>The practical rule: <b>if your advantage is dynamic, play fast and
        forcefully; if it's static, trade pieces and grind.</b> The classic
        error is treating a dynamic edge like a static one — spending three
        quiet moves "improving" while the opponent consolidates and your
        initiative evaporates.</p>"""),

    _lv("exp-2", "expert", "The initiative and sacrifice",
        "Spending material to buy time.",
        """<p>The initiative is the ability to keep making threats your opponent
        must answer. While they're reacting they can't execute their own plans,
        and defending accurately for many moves is far harder than attacking.</p>
        <p>Sacrifices convert material into time or structure:</p>
        <ul>
          <li><b>Gambits</b> — a pawn for development and open lines.</li>
          <li><b>Exchange sacrifice</b> (rook for minor piece) — for a dominant
          knight, a shattered structure, or the initiative. Often the most
          positionally sophisticated sacrifice.</li>
          <li><b>Positional piece sacrifice</b> — for lasting bind, not mate.
          Hardest to judge, because there's no forced line to verify.</li>
        </ul>
        <p>Before sacrificing, be honest about which you have: a <b>calculated</b>
        sacrifice ending in mate or material recovery, or an <b>intuitive</b> one
        you merely believe in. The first is a move; the second is a bet.</p>"""),

    _lv("exp-3", "expert", "Complex endgames",
        "Beyond rook endings.",
        """<p><b>Opposite-coloured bishops</b> — famously drawish in pure endings
        (even two pawns down can hold, since the defending bishop guards squares
        the attacker can never touch). But <i>with rooks or queens still on</i>
        they favour the attacker sharply, because the defending bishop can't
        cover the attacked colour. Know which situation you're in.</p>
        <p><b>Queen endings</b> — perpetual check is everywhere, so a passed pawn
        matters more than material. Centralise the queen; a queen on a central
        square both attacks and blocks checks.</p>
        <p><b>Knight endings</b> — behave much like pawn endings, because knights
        are poor at stopping passed pawns on the far wing. Opposition and
        tempo-play carry over.</p>
        <p><b>Fortresses</b> — some material deficits are simply unbreakable.
        Recognising a fortress saves half-points; failing to means grinding a
        drawn position for fifty moves.</p>"""),

    _lv("exp-4", "expert", "Practical decision-making",
        "Playing the opponent and the clock, not just the board.",
        """<p>Over the board you're not looking for the objectively best move —
        you're maximising your practical chances under time pressure with
        incomplete calculation.</p>
        <ul>
          <li><b>Manage the clock.</b> Spend time at critical moments —
          irreversible structural decisions, entering forcing lines — and move
          quickly in obvious positions. Losing on time in a won position is a
          skill failure, not bad luck.</li>
          <li><b>Play the position type your opponent dislikes.</b> Against a
          sharp tactician, close the position and grind. Against a positional
          grinder, complicate.</li>
          <li><b>When lost, complicate.</b> Objectively worse moves that create
          maximum practical difficulty are correct when the "best" move loses
          anyway.</li>
          <li><b>Don't play the last move again.</b> After a blunder, the
          position in front of you is the only one that matters. Tilt loses far
          more points than the original mistake.</li>
        </ul>"""),

    _lv("exp-5", "expert", "Studying and improving deliberately",
        "A training method that actually raises your level.",
        """<p>Most improvement plateaus come from practising what's comfortable.
        Deliberate practice means working at the edge of your ability.</p>
        <ol>
          <li><b>Analyse your own games first — without an engine.</b> Write down
          what you thought at each turning point. <i>Then</i> check with an
          engine. The gap between your reasoning and reality is your syllabus.</li>
          <li><b>Tactics daily, but slowly.</b> Solving hard puzzles properly
          beats blitzing easy ones. Calculate to the end before moving.</li>
          <li><b>Study whole annotated games</b>, not just openings — that's how
          you absorb plans and typical structures.</li>
          <li><b>Endgames pay compound interest.</b> They're finite, they never
          go out of fashion, and they decide close games.</li>
        </ol>
        <p>Track <i>why</i> you lose. If eight of your last ten losses were
        hanging pieces in equal positions, no amount of opening theory helps.</p>"""),

    # ── MASTER ──────────────────────────────────────────────────────────
    _lv("mas-1", "master", "Studying the classics",
        "Why old games still teach best.",
        """<p>Modern engine-checked games are objectively better, but classical
        games are clearer teachers: the ideas are executed against weaker
        resistance, so plans appear in undiluted form.</p>
        <p>The traditional progression: <b>Morphy</b> for development and open
        lines; <b>Steinitz</b> for the theory of accumulating small advantages;
        <b>Capablanca</b> for endgame technique and simplicity; <b>Alekhine</b>
        for dynamics and combination; <b>Botvinnik</b> for preparation and
        strategy; <b>Fischer</b> for clarity and precision; <b>Karpov</b> for
        prophylaxis and squeeze; <b>Kasparov</b> for dynamic aggression.</p>
        <p>Study method: play through a game and at each critical moment cover
        the next move and guess it. Note where you differ and why. Passive
        replaying teaches almost nothing — the guessing is the exercise.</p>"""),

    _lv("mas-2", "master", "Deep endgame theory",
        "Theoretical endings and what tablebases changed.",
        """<p>At master level, endgame knowledge is precise rather than general.
        Positions like rook and bishop vs rook, or queen vs rook, are known
        results with known techniques rather than things to work out.</p>
        <p><b>Tablebases</b> have solved every position with seven or fewer
        pieces. They proved that some endings assumed drawn are winning — and
        that some wins take over a hundred moves, exceeding the 50-move rule.
        The practical consequence: the theoretical result and the achievable
        result are not always the same thing.</p>
        <p>What's worth memorising: the <b>Vancura</b> defence for rook and pawn;
        the technique for bishop and knight mate (a genuinely hard skill under
        the 50-move rule); and the key drawing setups in queen vs pawn.</p>
        <p>Lichess exposes a free tablebase API, so any 7-piece ending can be
        checked exactly.</p>"""),

    _lv("mas-3", "master", "Preparation with engines",
        "Using computers without outsourcing your judgement.",
        """<p>Engines are enormously strong and completely unable to explain
        themselves. Used badly they produce players who know many moves and
        understand nothing.</p>
        <ul>
          <li><b>Always guess first.</b> Form your own assessment, then turn the
          engine on. Otherwise you're reading answers, not training.</li>
          <li><b>Understand the evaluation.</b> "+0.7" is meaningless until you
          can say <i>why</i>. Ask what changes in the resulting position.</li>
          <li><b>Beware engine-only lines.</b> A line requiring six exact moves
          is a great engine move and a terrible practical choice, unless you
          genuinely know all six.</li>
          <li><b>Prepare structures, not just moves.</b> Your opponent will
          deviate. Understanding the resulting middlegame survives that.</li>
        </ul>
        <p>Engine evaluations also shift with depth — an assessment at depth 20
        can reverse by depth 35. Don't trust a shallow number.</p>"""),

    _lv("mas-4", "master", "Long-term strategy",
        "Multi-move plans and manoeuvring.",
        """<p>The deepest strategic skill is executing plans that take ten or
        twenty moves and don't create a single threat along the way.</p>
        <p><b>The principle of two weaknesses</b>: a position with one weakness
        can usually be defended, because the defender concentrates everything on
        it. Wins come from creating a <i>second</i> weakness on the other wing
        and forcing the defence to stretch between them. This is the engine of
        most grandmaster grinds.</p>
        <p><b>Manoeuvring</b>: with no break available, improve piece placement
        while keeping the position closed. Repeating moves to gain time on the
        clock and provoke a concession is standard technique, not time-wasting.</p>
        <p><b>Restriction</b>: take squares away from enemy pieces even when it
        wins nothing immediately. A position where the opponent has no good moves
        eventually produces a bad one.</p>"""),

    _lv("mas-5", "master", "Building a complete game",
        "Putting the whole curriculum together.",
        """<p>A complete player moves between modes fluently, and the skill is
        recognising which the position demands.</p>
        <p>A workable framework for any position:</p>
        <ol>
          <li><b>Assess</b> — material, king safety, structure, activity, space.
          Who stands better, and why specifically?</li>
          <li><b>Classify</b> — is this sharp and forcing, or quiet and
          manoeuvring? That decides whether you calculate or plan.</li>
          <li><b>Candidates</b> — forcing moves first, then plans, then
          prophylaxis.</li>
          <li><b>Verify</b> — calculate the forcing lines fully; sanity-check
          quiet moves against the opponent's best plan.</li>
          <li><b>Blunder-check</b> — every move, no exceptions, forever.</li>
        </ol>
        <p>Beyond this there's no more syllabus — only your own games, honestly
        analysed, one weakness at a time. That never stops, at any level.</p>"""),
]

LEVELS_BY_ID = {lv["id"]: lv for lv in LEVELS}
_TIER_GAS = {t["key"]: t["gas_kas"] for t in TIERS}


def gas_for(level_id: str) -> int:
    lv = LEVELS_BY_ID.get(level_id)
    return _TIER_GAS.get(lv["tier"], 0) if lv else 0


def level_index() -> list[dict]:
    """The catalogue — safe to serve to anyone. Never includes `body`."""
    return [
        {
            "id": lv["id"],
            "tier": lv["tier"],
            "title": lv["title"],
            "summary": lv["summary"],
            "gas_kas": _TIER_GAS[lv["tier"]],
        }
        for lv in LEVELS
    ]


def content_for(level_id: str, unlocked: bool) -> str | None:
    """The level body — the ONLY way content leaves the server. Returns None if
    the level is still locked, so a client can't read ahead by guessing ids."""
    lv = LEVELS_BY_ID.get(level_id)
    if lv is None:
        return None
    if _TIER_GAS[lv["tier"]] > 0 and not unlocked:
        return None
    return lv["body"]
