# DAGmate — Roadmap #3a: DAA move-timeout forfeit (removing the oracle for abandonment)

**Status:** ON-CHAIN PRIMITIVES PROVEN (2026-09-05). All covenant legs are proven on Kaspa **mainnet**
dust — S8 (co-signed DAA-CLTV forfeit), S9 (bounded unilateral move), S10 (optimistic challenge
window), S11 (escrow→pending **link**), S11b (two-direction link). The full trustless forfeit path
runs end to end on chain: escrow → pending-forfeit → finalise / cancel. Remaining: the off-chain
signed-move channel, the v3 backend settlement branch, and the DAA clock — then testnet → mainnet.
Builds on the proven v2 covenant (docs/DAGMATE_COVENANT_V2.md) and the existing CLTV reclaim branch +
move-anchor infra. Prerequisite direction from Kaspa-dev feedback (2026-09-04): "oracle not needed if
thrown into a covenant" + "clock using DAA".

## Goal & threat model

v2 settles a decided game via a DAGmate **oracle** signature. The oracle can't skim/redirect (the
covenant enforces that), but it still *declares who won*, so a compromised oracle could sign the
wrong winner. #3a removes the oracle for the **abandonment / clock-flag** case — the most common
non-agreement ending — so that a player who stops responding **forfeits provably on-chain**, with
no trust in DAGmate's clock or verdict.

What #3a removes the oracle for: a game where one player vanishes or flags on time.
What it does **not** cover (stays on the oracle, or #3b ZK): a *contested checkmate* — a covenant
still can't compute chess, so it can't adjudicate "is this actually mate?".

Non-goal: on-chain per-move state. Anchoring every move as its own tx (recursive covenant) is
sound but far too slow/expensive for real-time chess (a move every few seconds vs Kaspa
confirmation latency + fees). Rejected. #3a keeps the game off-chain and only touches the chain to
*settle* — so the covenant never sees move times directly, which is the whole design tension below.

## The core difficulty (state it plainly)

A settle-time covenant can only introspect the **spending tx** (its inputs/outputs/locktime/DAA) and
whatever the spender **pushes in the witness**. It CANNOT read arbitrary chain history, so it cannot
know when an off-chain move happened. Therefore a "DAA clock" cannot be *computed* by the covenant
from move history — the deadline must be **carried into the covenant as authenticated data** and the
covenant enforces the timeout via `OpCheckLockTimeVerify` against the tx's own DAA-locktime.
(Confirmed opcodes: `OpTxInputDaaScore` 192, `OpCheckLockTimeVerify` 176, `OpCheckSequenceVerify`
177, `OpCheckSigFromStack` 215, 8-byte arithmetic `OpAdd`/… — all live post-Toccata.)
⚠️ You do **not** "modify state with DAA": the UTXO model has no mutable state. DAA is a *read-only
input a spend condition is gated on* (as the reclaim branch already does).

## Mechanism — co-signed checkpoint + bounded unilateral move + optimistic forfeit

A lightweight off-chain signed-move channel, plus a new **FORFEIT** leg in the escrow covenant.

### Off-chain channel
- **Checkpoint `C`** = `{matchId, ply, turn, deadlineDaa, budgetOppDaa}` signed by **BOTH** players
  (2-of-2). Both signing = both agree: at this ply it's `turn`'s move, due by `deadlineDaa`, and the
  opponent then has `budgetOppDaa` DAA of clock. Exchanged as the natural ack of each move (the
  opponent counter-signs the position before replying). Cheap: off-chain message signing, sub-second.
- **Unilateral move `M`** on top of a co-signed `C` = `{prevHash=H(C), ply+1, nextDeadlineDaa}` signed
  by the **mover only**. It extends an agreed state by exactly one move, so it can't be forged onto a
  fabricated history (H(C) pins the agreed prior state, which carries the opponent's signature).

The "latest state" a player holds is either a co-signed `C`, or a `C` + one unilateral `M`.

### Bounding the unilateral deadline (why the mover can't cheat the clock)
`M.nextDeadlineDaa` is set by the mover, who could try to make it too short to steal a forfeit. The
covenant BOUNDS it against the co-signed fields it can trust — a LOWER bound (proven direction, S9):

    M.nextDeadlineDaa  ≥  C.deadlineDaa + C.budgetOppDaa + INCREMENT_DAA

i.e. the opponent's deadline must be at least "mover used their whole remaining time, then the
opponent gets their full budget + the Fischer increment". `C.deadlineDaa`/`C.budgetOppDaa` are
co-signed (agreed), `INCREMENT_DAA` is baked in the escrow, and the covenant checks the inequality
with 8-byte `OpAdd`/`OpGreaterThanOrEqual`. Result: the bound is *generous to the opponent* (never
too short — the mover is forced to grant at least the maximum fair deadline), so the mover cannot
shorten the opponent's clock; the worst they can do is give the opponent slightly MORE time than
strictly owed. ⚠️ It MUST be `≥` — an `≤` (upper) bound would let the mover set a *short* deadline
and steal, which is exactly the S9 "short-deadline steal" adversarial case (rejected on dust).
Oracle-free and sound.

### The FORFEIT covenant leg (new, beside win-A / win-B / draw / reclaim)
Witness to claim a forfeit for `claimant` (the player still active), presenting `C` (+ optional `M`):

    <sigA_on_C> <sigB_on_C> <C-fields> [ <sigMover_on_M> <M-fields> ] <FORFEIT selector> <settle>

The leg verifies (all with proven idioms):
1. **`C` is genuine:** `OpCheckSigFromStack` over `H(C)` against `pkA` AND against `pkB` (both baked).
2. **`M` (if present) extends `C`:** `M.prevHash == H(C)` (`OpEqualVerify`) and `OpCheckSigFromStack`
   over `H(M)` against the mover's pk (the player whose turn `C` says it is).
3. **It's the OPPONENT's turn now** (parity of the presented ply) → the forfeiter is the opponent,
   the claimant is the other player.
4. **The deadline bound holds** (if `M` present): the inequality above.
5. **The deadline has passed:** tx locktime ≥ (M or C).effectiveDeadlineDaa, via
   `OpCheckLockTimeVerify` (the tx's DAA-locktime is the authenticated "now").
6. **The output pays the claimant** their side's stake (win-take-all) — same `OpTxOutputSpk` +
   amount-introspection as v2's win leg.

### Optimistic challenge window (prevents claiming with a STALE state)
A forfeiter who actually moved later must be able to cancel a bogus claim. So a forfeit does not pay
out immediately: it spends the escrow into a **pending-forfeit** UTXO carrying
`challengeDeadlineDaa = now + W`. During `W`:
- the accused can **cancel** by presenting a **newer** co-signed `C'` (higher ply) — proving the game
  advanced past the claimed state — which voids the forfeit; or
- if `W` passes unchallenged, the claimant **finalizes** and takes the pot (CLTV on `challengeDeadlineDaa`).

"Latest state wins", the standard fraud-proof dispute. Residual assumption (also standard): a player
must **watch the chain during `W`** and post their newer state if wrongly claimed against. `W` is
sized generously (e.g. hours of DAA) so this is easy; DAGmate can also watch-and-defend on a
player's behalf as a *convenience* (not a trust dependency — the player can always do it themselves).

## What stays trusted (be honest)
- **Contested checkmate** — not covered; a covenant can't compute mate. Stays on the oracle (or #3b).
- **Both players collude to stall forever** — they only hurt themselves; funds are always reclaimable
  via the existing 14-day CLTV branch.
- The **increment/clock config** is baked at escrow creation, so it's fixed & agreed up front.

## Build plan (spikes first, same discipline as v2)
1. ✅ **S8 — DAA-CLTV forfeit leg (mainnet):** pays a claimant only after tx-locktime ≥ a co-signed
   deadline DAA AND a co-signed `C` verifies (2× `OpCheckSigFromStack`) + turn selection. Proven:
   accept-after / reject-before, forged co-sig rejected, wrong-claimant rejected.
2. ✅ **S9 — bounded unilateral move (mainnet):** `M` (prevHash pin + mover sig + the `≥` deadline
   bound); the too-short `M.nextDeadlineDaa` "short-deadline steal" is rejected on-chain.
3. ✅ **S10 — optimistic challenge (mainnet):** pending-forfeit UTXO; cancel-by-newer-state and
   finalize-after-W both work; stale / forged / wrong-payee / early all rejected.
4. ✅ **S11 — escrow→pending LINK (mainnet):** the forfeit spend OUTPUTS into the S10 pending covenant
   instead of paying directly. The escrow reconstructs the pending P2SH scriptPubKey on-chain
   (`pendingRedeem = PREFIX ‖ ply2 ‖ SUFFIX`, `OpBlake2b`, spk `0000 aa20 ‖ h ‖ 87`,
   `OpTxOutputSpk == it`) — first on-chain use of `OpBlake2b` (opcode 170); the pot verifiably lands
   at the reconstructed address. Escrow-leg redeem 387B — well under the compute-mass ceiling.
   ✅ **S11b — two-direction link (mainnet):** a co-signed `claimant` byte selects PA vs PB; both
   directions land + finalise; cross-direction, flipped-claimant, early, forged all rejected. (This
   subsumed the originally-planned "S11 adversarial matrix" — the adversarial cases are proven across
   S8–S11b, and the real open problem turned out to be the *linkage*, now resolved.)
5. **▶ NEXT — the off-chain channel** (sign/verify/exchange `C` & `M` in the game loop), the backend
   settlement branch (a v3 escrow variant behind an `ESCROW_V3`/`DAA_FORFEIT` flag), a DAA clock in
   `clocks.py` used for the on-chain deadlines (kept alongside the wall-clock UX display), frontend,
   tests, then testnet full-flow, then mainnet — exactly the v2 rollout shape.

### What the on-chain proofs settled (byte-exact facts for the builders)
- **Checkpoint hash** = `SHA256(matchTag ‖ deadlineDaa ‖ ply2 ‖ claimant)`; deadline is the minimal
  script-number encoding so it doubles as the CLTV operand with no `OpBin2Num`. Signed by both players
  with `signScriptHash` → the bare 64-byte Schnorr (`subarray(1,65)` of the 66-byte return) for
  `OpCheckSigFromStack`.
- **`claimedPly` is a FIXED 2-byte little-endian field** (constant `0x02` push framing → baked into the
  pending-covenant PREFIX). This is what makes the escrow's `PREFIX ‖ ply2 ‖ SUFFIX` reconstruction a
  simple two-`OpCat` with no length arithmetic. The pending covenant reads it with `OpBin2Num` before
  `OpGreaterThan`. The split reproduces the real `ScriptBuilder` redeem byte-for-byte across ply
  0..32767 (`dev_s11_split.mjs`).
- **P2SH spk framing** (confirmed against real `ScriptBuilder`): `0x0000` (version, 2-byte BE) ‖ `aa 20`
  (OpBlake2b-push of a 32-byte hash) ‖ `<blake2b256(redeem)>` ‖ `0x87` (OP_EQUAL) = 37 bytes.
- **Compute mass:** the single-direction escrow leg is 387B, the two-direction 641B; both accepted, so
  the earlier ~500k-mass worry is cleared for this construction. No slimming needed.
- **Offline-first workflow:** develop the stack choreography in `scriptsim.mjs` (a pure Kaspa-script
  stack simulator; `OpBlake2b`/`OpBlake3` are domain-separated stand-ins) via `dev_s8..s11b.mjs`, then
  prove the real crypto with ONE on-chain run via `spikes_forfeit.mjs [S8|S9|S10|S11|S11b]`.
- **txid note:** Kaspa's txid EXCLUDES the signature script, so an honest spend and a forged-witness
  attempt on the same UTXO share a txid — the forged one fails script verification, the honest one is
  accepted. Not a bug; don't key any logic on txid uniqueness across witnesses.

## Open problems to resolve during S8–S9 (don't pretend these are settled)
- **Stonewalling refinement:** the `C + M` construction lets a mover advance one step unilaterally,
  but confirm on-chain that a player who never gets a *co-signed* reply can still always reach a
  "opponent-to-move" claimable state from the last co-signed `C` they hold. Map every stall ordering.
- ✅ **Ply/turn encoding & hashing — RESOLVED (S11/S11b):** `H(C) = SHA256(matchTag ‖ deadlineDaa ‖
  ply2(fixed 2B LE) ‖ claimant(1B))`; the covenant recomputes it from witness copies and it matches
  the signed hash on-chain. See "What the on-chain proofs settled" above for the full byte layout.
- **DAA↔seconds mapping for the clock:** ~1 DAA/s but not exact; size budgets/`W` with margin so
  clock drift never flips a legitimately-in-time move into a forfeit.
- **Griefing the challenge window:** ensure a spurious forfeit-claim costs the claimant (fees) and
  can't be spammed to lock a pot; `W` bounds the delay, but quantify worst-case stall.

## Reuse
CLTV/DAA branch idioms from escrow.js/escrow_v2.js; `OpCheckSigFromStack`/hashing/introspection from
the v2 covenant (all proven S5–S7); the move-anchor path (`anchor()`); the v2 backend settlement
shape (a v3 branch beside `_settle_v2`); and DagLock's adversarial-matrix method.
