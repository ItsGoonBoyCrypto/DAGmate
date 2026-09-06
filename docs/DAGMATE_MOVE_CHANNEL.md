# DAGmate — off-chain signed-move channel (roadmap #3a, part 2)

**Status:** CORE PROVEN (2026-09-05). The channel primitives are implemented in `service/move_channel.mjs`
and proven three ways: (1) the checkpoint/move hashes are byte-identical to the on-chain-proven covenant
layout (`dev_channel.mjs`), (2) sign/verify/tamper are correct off-chain (noble BIP340), and (3) a
session-key checkpoint from this module **settles the forfeit covenant on Kaspa mainnet end to end**
(`spikes_forfeit.mjs S11n`: accepted by `OpCheckSigFromStack`, landed in the pending covenant, finalised).
Remaining for #3a part 2: the exchange transport + client state, and the backend relay/watch-tower.

## What the channel is
The forfeit legs (S8–S11b) are *fed* by two small messages the two clients exchange as they play — no
chain writes during the game, only two Schnorr signatures per move at most:

- **Checkpoint `C`** — co-signed by BOTH players (2-of-2). "At this ply, the clock is due at
  `deadlineDaa`; if it lapses, `claimant` may forfeit-claim." Exchanged as the natural ack of a move:
  the opponent counter-signs the resulting position before it's their turn.
- **Move `M`** — signed by the MOVER only. Extends an agreed `C` by exactly one move (pins `hC`), so it
  can't be forged onto a fabricated history. The "latest state" a client holds is either a co-signed `C`,
  or a `C` + one unilateral `M`.

## Keys: per-match SESSION keys (not the wallet)
`OpCheckSigFromStack` verifies a raw BIP340 Schnorr signature over a 32-byte message. Browser wallets
(Kasware / Kastle / Kaspire) only expose `signMessage`, which hashes with its own personal-message prefix
— **incompatible** with a raw digest sign. So:

- At match start each client mints a **session keypair** (`move_channel.newSessionKey()` → BIP340). The
  x-only pubkeys `pkA`/`pkB` are baked into the escrow + pending covenants as the checkpoint signers.
- The **main wallet** signs only the on-chain txs: the deposit into escrow, and the settlement/claim
  spend. It authorises the session pubkey once, at deposit time (the deposit references `pkA`/`pkB`, so a
  wrong session key can't be substituted after funding).
- The session private key lives only in the client for the match's duration. Compromising it lets an
  attacker co-sign checkpoints *as that player* — which is exactly that player's own authority — so it is
  no worse than the player acting against themselves; it can never move funds (only the wallet-signed
  claim tx can, and the covenant fixes the payee).

**Proven consistent:** noble's BIP340 `verify` accepts the SDK's `signScriptHash` output (the scheme the
covenant enforced on-chain in S8–S11b) and vice-versa, and a noble-signed session checkpoint was accepted
on mainnet (S11n). So a signature made in the browser settles the covenant unchanged.

## Exact byte layout (DO NOT change without a new on-chain proof)
Little-endian, minimal script numbers (rusty-kaspa `numToBytes`); ply is FIXED 2-byte LE.

    C preimage = matchTag(32) ‖ deadlineDaa(minimal LE) ‖ ply(2B LE) ‖ claimant(1B: 0x01=A, 0x02=B)
    hC         = SHA256(C preimage)
    M preimage = hC(32) ‖ nextDeadlineDaa(minimal LE)
    hM         = SHA256(M preimage)

`deadlineDaa` uses the minimal encoding so the same bytes double as the covenant's `OpCheckLockTimeVerify`
operand (no `OpBin2Num`). `ply` is fixed-width so the escrow can reconstruct the pending covenant's redeem
by concatenation (`PREFIX ‖ ply2 ‖ SUFFIX`, proven byte-exact ply 0..32767). Both players sign `hC`;
the mover signs `hM`.

## Module API (`service/move_channel.mjs`, portable ESM — Node now, browser bundle later)
- `newSessionKey()` → `{ privHex, xonlyHex }`
- `hashCheckpoint({matchTag, deadlineDaa, ply, claimant})` / `signCheckpoint(cp, privHex)` /
  `verifyCheckpoint(cp, sig, xonlyPub)`
- `hashMove({hC, nextDeadlineDaa})` / `signMove(m, moverPrivHex)` / `verifyMove(m, sig, moverXonly)`
- `plyField(ply)`, `numToBytes(n)`, `CLAIMANT` — the shared encoders.
Crypto is `@noble/curves` (BIP340) + `@noble/hashes` (sha256); no SDK, so the frontend can bundle it.

## Exchange protocol (to build — part 2)
Transport reuses the existing match websocket/relay (the same channel that already ships moves for the
board). The relay is a **dumb router + store**, never an authority:
1. Mover plays move → client builds `C'` for the new ply (its own deadline math from the DAA clock) →
   signs with its session key → sends `{move, C', sigMover}` to the opponent via the relay.
2. Opponent validates the move (chess-legal) and the deadline math, counter-signs `C'` → sends `sigOpp`
   back. Now both hold a fully co-signed `C'`. This is the ack.
3. If the opponent stalls (never counter-signs / never moves), the mover falls back to the last co-signed
   `C` it holds and, when `deadlineDaa` lapses, may present `C` (+ its own `M`) to the forfeit leg.
The relay stores the latest `{C, sigA, sigB [, M, sigM]}` so a reconnecting client recovers state; because
every field is co-signed, a malicious relay can withhold or replay but cannot forge a state.

## Watch-and-defend (convenience, not trust)
During the challenge window `W`, a wrongly-forfeited player must post their newer co-signed `C'` to the
pending covenant's CANCEL branch. The player can always do this themselves; DAGmate MAY run an optional
watch-tower (the sidecar, using `verifyCheckpoint`) that auto-cancels a bogus claim on the player's behalf.
This is a convenience — the covenant never trusts it, and a player who watches the chain needs nothing
from DAGmate. `W` is sized in hours of DAA so self-defence is easy.

## The DAA clock (to build — part 2, `clocks.py`)
Deadlines are DAA scores, not wall-clock. `clocks.py` maps the game's time control to DAA using ~1 DAA/s
with margin (never tight enough that clock drift flips an in-time move into a forfeit), and produces the
`deadlineDaa`/`nextDeadlineDaa` the clients put into `C`/`M`. The wall-clock UX display stays as-is; the
DAA figure is the authoritative one the covenant reads.

## v3 integration checklist (the frontend-coupled build — TESTNET first)
The covenant, the DAA clock, `escrow_v3.js`, and the sidecar `/escrow-v3/*` routes + `service_client`
wrappers are DONE and proven. What remains is one coupled unit (backend + frontend), because the escrow
can't be built until the clients have minted their session keys:

1. **Frontend — session key.** At challenge create/accept, mint `move_channel.newSessionKey()`; keep the
   private key in the match's client state; send the x-only `sessPk` alongside the wallet pubkey.
2. **Backend — escrow creation.** Thread `sess_pk_a`/`sess_pk_b` + `w_daa` (=`daa_clock.challenge_window_daa()`)
   into `_create_match_from_pair`; when `ESCROW_V3_ENABLED`, call `service_client.build_escrow_v3(... side A/B ...)`
   and `db.set_match_escrows(..., version="v3")` (mirror the v2 branch at `main.py:496`). Store the returned
   `checkpointTag` on the match so both clients build `C` against the same domain.
3. **Backend — settle dispatch.** In `settlement.py`, add a v3 arm beside `_settle_v2` (oracle settle is
   identical: `oracle_sign_result_v3` + `settle_v3`); add a `settle_v3_verdict_json` column.
4. **Backend — trustless forfeit.** In `clocks.py`, when a v3 match flags, drive the forfeit instead of the
   oracle path: `forfeit_claim_v3` (deadline = the co-signed `C.deadlineDaa`) → persist the returned
   `pendingAddress`/`pendingRedeem` → after `w_daa` DAA, `forfeit_finalise_v3`. A `forfeit_cancel_v3` is
   posted if a newer co-signed `C'` arrives during the window (self, or the watch-tower).
5. **Relay.** Carry `C`/`M` over the existing match websocket (§ Exchange protocol). Optional sidecar
   watch-tower verifies opponents' checkpoints and auto-cancels a bogus claim.
6. **Frontend — play + collect.** Co-sign `C` on each move (`move_channel.signCheckpoint`); a claim/cancel
   panel for the forfeit + challenge window.
7. **Tests → testnet.** Backend tests per the house rules (non-empty lists, real row types, DB isolation),
   then the full flow on **testnet** — including the forfeit e2e (`e2e_v3.mjs`), which the core.js 60s
   `withRpc` fn-timeout blocks in one mainnet process but which runs fine as separate short requests in
   production (claim now, finalise hours later).

## Open items for part 2 (be honest)
- **Deadline math ownership:** clients compute `deadlineDaa`; the opponent must re-derive and refuse to
  counter-sign a `C` whose deadline is wrong. Spec the exact formula (base + increment per move) so both
  sides agree deterministically.
- **Reconnect / abandonment orderings:** map every stall ordering to a claimable state from the last
  co-signed `C` (carried over from the S9 stonewalling analysis — the `≥` deadline bound is already proven).
- **Relay auth:** the relay must authenticate which session key belongs to which seat so it can't cross
  messages between matches; the co-signatures make forgery impossible but mis-routing is still a DoS.
