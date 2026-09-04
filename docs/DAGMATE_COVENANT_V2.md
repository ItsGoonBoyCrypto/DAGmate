# DAGmate — Covenant Escrow v2 (roadmap #2)

**Status:** DESIGN, source-grounded, pre-implementation. Nothing here is on mainnet.
Ships behind an `ESCROW_V2` flag after a testnet-10 prototype + on-chain adversarial
matrix; v1 (the 2-of-3 P2SH in `service/escrow.js`) keeps running until v2 is proven.

## Why

v1 escrow is a plain P2SH — `IF <2-of-3 CHECKMULTISIG> ELSE <CLTV reclaim>` — with no
output introspection. Settling a win needs DAGmate's **arbiter key to co-sign every
payout** (roadmap #1 shrinks this to a stall-breaker, but the arbiter still holds a
live signing key in the loop). Covenants (live on Kaspa mainnet since the Toccata hard
fork, 2026-06-30) let the **escrow script itself** enforce the payout, so DAGmate's role
shrinks to a **write-once oracle**: it signs the game result once and can then be offline
forever. Three wins:

1. **Non-interactive, self-claiming.** The winner (or anyone) releases the pot with the
   oracle-signed result — no live co-sign, no multi-visit signing dance.
2. **Amount + destination enforced on-chain.** Introspection rejects any settle tx that
   doesn't pay the winner the full pot. The "takes no cut" promise becomes consensus, not
   backend code + a disclaimer.
3. **Arbiter → oracle.** DAGmate can only *declare a winner-of-two*, cryptographically
   bound to this match; it can't skim, redirect, or be a liveness dependency at settle time.

**The residual trust (state it plainly), and how it's actually removable:** a covenant cannot
*compute* chess — it can't tell checkmate from a random position — so in v2 the oracle
*declares* the result and a compromised oracle could sign the wrong winner. But the oracle is
NOT fundamentally irreducible; it decomposes by case, and only the last case is genuinely hard:

- **Honest games (both players agree the result):** no oracle needed *already* — that's roadmap
  #1 (mutual settlement: winner + loser co-sign, no arbiter/oracle). BUILT + deployed.
- **Abandonment (a player just stops responding):** removable with NO oracle and NO chess
  validation, via a **DAA-based move-timeout forfeit**. Players sign each move into a channel
  (the move-anchor infra already exists); if the opponent posts no signed move within N DAA, the
  covenant awards the pot by forfeit — enforced on-chain with `OpTxInputDaaScore` +
  `OpCheckLockTimeVerify` (the SAME machinery the reclaim branch already uses). You do NOT
  "modify state with DAA" — DAA is a read-only input the spend condition is gated on. This is the
  feasible near-term trustless win and is where a "clock using DAA" belongs.
- **Contested result (a player claims checkmate, the other disputes):** the covenant must
  determine the winner on-chain, which needs chess-rule validation — impractical in raw Kaspa
  Script (no loops, mass limits; you'd be running a move-legality + checkmate checker per tx).
  The realistic route is **ZK-proven chess**: prove off-chain "this signed move sequence is legal
  and ends in checkmate for X", verify the proof on-chain via `OpZkPrecompile` (live post-Toccata,
  opcode 166). Feasible in principle, but a major R&D project (a chess ZK circuit + prover), so it
  is the endgame, not a quick change.

So "throw the game into a covenant and drop the oracle" is right in spirit: honest play needs no
oracle today, abandonment can be made oracle-free next, and only a *contested* result still needs
either the oracle or a ZK proof. See "Roadmap #3" below.

## Roadmap #3 — removing the oracle (DAA-timeout forfeit → ZK-proven result)

**#3a (feasible next, medium effort) — signed-move channel + DAA-timeout forfeit.** Each move is
signed by the mover (and countersigned/acked), forming an off-chain hash-chain; DAGmate anchors
them (existing `anchor()` / `ANCHOR_MOVES`). Settlement gains two oracle-free branches beside v2's:
(i) **mutual** (both sign the final result — #1), and (ii) **forfeit** — the last-to-move party
claims after the opponent misses the DAA deadline, the covenant checking `OpTxInputDaaScore`
against a baked/committed deadline (CLTV/CSV), plus a challenge window in which the opponent can
post the missing signed move to cancel the forfeit. This removes the oracle for the two common
non-agreement cases (abandonment / clock-flag) using only proven timelock opcodes. The oracle
remains ONLY as the fallback for a genuinely contested checkmate — a rare edge.

**#3b (endgame R&D) — ZK-proven result.** Replace even that fallback: a succinct proof that the
signed move list is a legal game ending in a specific result, verified on-chain by `OpZkPrecompile`.
Removes the oracle entirely. Large, standalone effort (circuit + prover + integration).

DAA-clock note: the *current* server-authoritative clock (clocks.py, wall-time) stays correct for
v1/v2 because the oracle arbitrates the result anyway. A DAA clock is only needed for #3a, where a
timeout must be *provable on-chain* rather than trusted to DAGmate's server time.

## Verified opcode semantics (rusty-kaspa master, txscript/src/opcodes/mod.rs — NOT the KIPs)

The KIPs are high-level; these were read from source (see also [[reference_kaspa_mass_limits]]
— on Kaspa, read the source, the docs are stale):

- `OpCheckSigFromStack` pops `[signature, msg_hash, pubkey]` (push in that order; pubkey on
  top). **`msg_hash` MUST be exactly 32 bytes** or it errors `"message hash must be 32 bytes"`.
  Pushes a bool like CHECKSIG. This verifies the ORACLE's signature over an arbitrary 32-byte
  message we build on-chain.
- `OpTxOutputAmount` (0xc2): pops an `i32` index, pushes that output's amount (sompi, i64).
- `OpTxOutputSpk` (0xc3): pops an `i32` index, pushes that output's scriptPublicKey bytes —
  **version AND script bytes**, so a comparison target must be built with the same version prefix.
- `OpTxInputAmount` (0xbe): pops an `i32` index, pushes that input's UTXO amount (i64).
- `OpTxInputIndex` (0xb9): pushes the index of the input currently being validated (no args) —
  lets a covenant know its own position without the witness telling it.
- `OpCat` (126): pops `b` then `a`, pushes `a‖b`. Enabled; result bounded by
  `MAX_SCRIPT_ELEMENT_SIZE` (probe S5a confirms the limit on-chain).
- `OpSHA256` (168): 32-byte digest — the right size for the CheckSigFromStack message.

## Design — oracle-blessed, self-claiming, amount-enforced

Keep the **two-escrow** structure (one per depositor) so "reclaim your own stake" and the
deposit watcher are unchanged. Only the IF (settle) branch changes; the ELSE (CLTV reclaim)
branch is byte-for-byte v1.

**Per-escrow redeem, baked constants** (all known at match creation):
`pkA`, `pkB` (players, x-only 32B), `pkOracle` (DAGmate oracle key, x-only 32B — derived
from the DAGmate seed like today's arbiter key, but now a signer-of-record not a co-signer),
`matchTag` (32B = `SHA256(matchId ‖ escrowSide)`, unique per escrow so an oracle sig can't
replay across matches or escrows), `MAX_FEE_PER_INPUT` (i64).

**Witness to settle:** `<oracleSig> <winnerSel>` where `winnerSel` ∈ {`OP_0`=A won, `OP_1`=B won}.

**Settle branch logic:**
1. `winnerPk = winnerSel ? pkB : pkA` (OP_IF on a DUP of the selector).
2. `msg = SHA256(matchTag ‖ selByte)` (OpCat + OpSHA256) — binds the oracle sig to THIS
   escrow AND the declared winner. Push `oracleSig`, `msg`, `pkOracle`; `OpCheckSigFromStack`;
   `OpVerify`. → the oracle can only authorize A-or-B for this specific escrow.
3. `i = OpTxInputIndex`. Enforce the SAME-index output pays the winner:
   - `OpTxOutputSpk(i) == P2PK(winnerPk)` (build the expected version-prefixed P2PK spk and
     `OpEqualVerify`),
   - `OpTxOutputAmount(i) ≥ OpTxInputAmount(i) − MAX_FEE_PER_INPUT` (`OpGreaterThanOrEqual`).
   The input↔output 1:1 binding means each escrow independently guarantees its own stake
   reaches the winner; no cross-input skim is expressible. The claim tx must be built with
   outputs count == inputs count, each paying the winner — the covenant enforces it.
4. `OP_TRUE`.

**Reclaim branch:** unchanged — `<reclaimDaa> OpCheckLockTimeVerify <pkDepositor> OpCheckSig`.

**Draws:** the oracle signs `winnerSel = DRAW`; a draw variant of the branch enforces each
input's same-index output pays its OWN depositor ≥ input − fee (introspection guarantees the
50/50-back split). (Detail deferred to the build; draws are low-trust — the arbiter can't
misdirect a depositor refund even in v1.)

## The unknowns to PROVE on dust before building the full escrow (probe campaign S5)

Covenant scripting is unforgiving; each of these is proven with a minimal throwaway script
submitted as a dust spend (same method as S1–S3), on **testnet-10 first**:

- **S5a — OpCat + OpSHA256:** witness `<a> <b>`; script `OpCat OpSHA256 <H(a‖b)> OpEqual`.
  Proves cat+sha execute with standard byte semantics and reveals `MAX_SCRIPT_ELEMENT_SIZE`.
- **S5b — OpCheckSigFromStack:** bake `pkOracle` + 32B `msg`; witness `<oracleSig>`; script
  `<msg> <pkOracle> OpCheckSigFromStack`. Proves oracle attestation + confirms the pop order
  and the raw-32-byte message format (vs any signMessage prefix wrapper).
- **S5c — output introspection + spk equality:** covenant requiring `OpTxOutputSpk(0)` equals
  a baked P2PK spk and `OpTxOutputAmount(0) ≥ N`. Proves the spk byte-encoding we must match
  (version prefix included) and the amount comparison.
- **S6 — full v2 settle branch** assembled from the above, then the **adversarial matrix**
  (reuse the DagLock red-team approach): wrong-winner sig rejected, no-oracle-sig rejected,
  skim/underpay rejected, wrong-destination rejected, cross-match replay rejected, reclaim
  still works after CLTV.

## Rollout (testnet-first, always)

1. Probe campaign S5a/b/c + S6 on **testnet-10** (throwaway keys, faucet dust, swept back).
2. Adversarial matrix green on testnet-10.
3. Tiny **mainnet-dust** S6 run (consensus parity check), same as S1–S3 were mainnet dust.
4. Implement `escrow_v2.js` + `ESCROW_V2` flag; new matches opt in, live v1 matches finish on v1.
5. Refresh `DAGMATE_SPEC.md` §2/§3 + README trust model when v2 goes live (user-facing change).

Reuse: `service/escrow.js` script-builder patterns, `service/spikes.mjs` dust harness
(fund→prove→sweep), and **DagLock** (audited covenant codebase, red-team clean) for the
introspection idioms and the adversarial matrix.
