# DAGMATE — Build Spec v2 (standalone, non-custodial)

**Product:** P2P wagered chess on Kaspa L1. Pot escrowed in a real on-chain
2-of-3 P2SH address, every move optionally anchored on-chain as a payload tx,
winner takes pot minus rake. **Standalone product** — own domain
(dagmate.org), own repo, own bot, own service, own database. Shares no code,
infrastructure, users, or funds with any other project.

**Trust model:** non-custodial. Players connect their own wallet (Kasware,
Kastle, or Kaspire) and sign every transaction themselves — DAGmate never
holds a player's private key or general wallet balance. The one piece of
centralization left is the **arbiter co-signing key**: the 2-of-3 escrow
script needs a neutral third signer to release funds on game end (mate,
resign, flag, draw), and that key is held server-side. This is real
trust-minimization, not full trustlessness — say so plainly in the UX. If the
service ever goes dark, every player can unilaterally reclaim their *own*
stake after the CLTV timeout with any wallet that can sign a custom script;
nobody can touch the other side's stake, arbiter included.

---

## 0. Scope

**IN (v1):**
- 1v1 challenge with KAS stake (or 0-stake friendly), `rapid` (live clock) and
  `daily` (24h/move correspondence) modes
- Real on-chain escrow: one 2-of-3 P2SH address per player per match (player
  A, player B, per-match arbiter key), unilateral CLTV reclaim branch after
  14 days
- Full rules via `python-chess`: legality, mate, stalemate, auto-draws,
  threefold/50-move claims, resign, draw offers, flag (timeout) wins
- Wallet-connect signing: **Kasware** and **Kastle** at launch (both have a
  documented custom-script-signing API — `signPskt` / `signTx(...,scripts[])`
  respectively); **Kaspire** once its SDK is verified against the other two
  (it advertises purpose-built covenant/multisig signing over WalletConnect
  v2, but is newer/less battle-tested); **Kaspium** supported for *funding
  only* (plain KAS send — any wallet can do that), not for signing the
  settlement leg, since it has no dApp/script-signing API
- Settlement: pot − rake → winner, rake → project fee address; draws split
  50/50 − rake
- Per-move on-chain anchor tx (payload = match/ply/move/board-hash), tiny fee
- Telegram alert bot: challenge received, your move, clock warning, match
  settled — notifications only, no wallet access, no game logic
- Kill switch, idempotency, stake caps, owner gate before any public launch

**OUT (later phases):**
- Tournaments (same match engine, bracket layer on top)
- Discord alerts (planned, phase 2 — many Kaspa users are there)
- Trustless covenant chess-clock (removes the arbiter entirely — needs
  Silverscript / KIP-16 vprogs, testnet-10 first)

---

## 1. Architecture

```
Player's own wallet (Kasware / Kastle / Kaspire ext, or Kaspium for funding)
   │  wallet-connect: fund escrow (plain send) + sign settlement (custom script)
   ▼
dagmate.org (site)               — game UI, matchmaking, board, clock
   │
   ├─ backend API                — match state, DB, orchestrates settlement
   │     builds the unsigned settle tx, collects the winner's wallet-connect
   │     signature + the arbiter's own signature, assembles + broadcasts
   │
   └─ service/ (Kaspa sidecar)   — WASM/RPC: escrow script + address builder,
         arbiter key derivation (own HD seed, DAGmate-only, never Dagger's),
         settle-tx assembly, anchor txs, DAA-score lookups

Telegram bot (dagmate/bot)       — alerts only: challenge received, your
   turn, clock warning, match settled. Links to a site account via a one-time
   code. Holds no wallet, no game state, no keys.

Chain: Kaspa mainnet.
```

---

## 2. Escrow design (the on-chain part)

### 2.1 Keys

- **Player keys:** the player's own wallet (Kasware/Kastle/Kaspire), never
  held or derived by DAGmate. The site only ever receives a *signature* over
  a specific tx via the wallet's connect API — never a private key.
- **Arbiter key:** per-match, derived from DAGmate's own HD seed (fresh,
  generated for this project — zero relation to any other project's keys):
  `new PrivateKeyGenerator(xprv, false, 1n).receiveKey(match_id)` — account
  **1n**, one key per match to limit blast radius and keep audit trails clean.

### 2.2 Script — one escrow address **per player** per match

```
OP_IF
    OP_2 <pk_playerA> <pk_playerB> <pk_arbiter> OP_3 OP_CHECKMULTISIG   # settle: any 2 of 3
OP_ELSE
    <reclaim_locktime> OP_CHECKLOCKTIMEVERIFY
    <pk_depositor> OP_CHECKSIG                                          # depositor reclaims after 14d
OP_ENDIF
```

P2SH address = standard Kaspa pay-to-script-hash over the redeem script.

Why per-player addresses (not one shared pot address): the CLTV branch names
**that player's own key**, so an abandoned match degrades to "everyone
reclaims their own stake, unilaterally, no arbiter needed."

- `reclaim_locktime` = current DAA score + ~14 days of DAA (Kaspa ≈ 10
  blocks/sec → `14*24*3600*10` ≈ 12,096,000).

### 2.3 Flows

**Deposit** — site shows each player their own escrow address; player sends
their stake from their own wallet (any wallet — this is a plain send).
Backend watches both addresses; both UTXOs ≥ stake → match goes LIVE.

**Settle (normal)** — one tx spending BOTH escrow UTXOs via the IF branch:
- unlock script per input: `<sig_arbiter> <sig_winner> OP_TRUE <redeem>`
- `sig_winner` comes from the winner's own wallet via a wallet-connect
  custom-script signing request (Kasware `signPskt` / Kastle `signTx` with
  `scripts[]`); `sig_arbiter` is produced server-side
- outputs: `pot − rake − txfee → winner`, `rake → project fee address`
- Draw: two outputs, `(pot − rake)/2` to each — both players sign their own
  escrow's release via their own wallet.

**Abort/refund** (opponent never funds, or mutual abort before move 2):
arbiter + depositor co-sign each escrow back to its own owner (again, the
depositor's own wallet signs). Rake **not** taken on aborts.

**Disaster path** (service gone): after 14 days each player sweeps their own
escrow via the ELSE branch with any wallet that can sign a custom script —
this is the actual trust-minimization claim, and it should be demonstrably
true (a small reference reclaim script belongs in this repo).

---

## 3. Service (`service/`)

Kaspa WASM/RPC sidecar, own HD seed (env-configured, `DAGMATE_MASTER_XPRV`
or equivalent — never shared with any other project's key material).

```
GET  /escrow/arbiter-pubkey?matchId=<n>   → { pubkey }
POST /escrow/build   { matchId, pkA, pkB, depositorIsA, reclaimDaa }
     → { address, redeemHex }             // pure function, no chain calls
POST /escrow/settle-unsigned  { matchId, escrows, winnerAddr|split, rakeSompi }
     → { txJson, sighashes }              // unsigned tx for the site to hand
                                           // to the winner's wallet for signing
POST /escrow/settle-broadcast  { matchId, txJson, sigWinner, sigArbiter }
     → { txid }
POST /escrow/anchor  { matchId, payloadHex, feeSompi }
     → { txid }
GET  /escrow/daa      → { daaScore }
```

Hard-won Kaspa script rules — confirmed on mainnet dust during the original
spikes, real consensus behavior, not SDK quirks. Bake these into the
escrow/settle builders or they will silently reject on mainnet:

1. **`sigOpCount` is a `createTransactions()`/`createTransaction()` option,
   not a post-hoc field.** Set it at creation time. Underfunding it →
   `script units exceeded the amount committed in the input`.
2. **CHECKMULTISIG is billed by pubkey count (n), not required-sig count
   (m).** A 2-of-3 script needs `sigOpCount: 3`, not `2`.
3. **Sig order in the witness must match pubkey order in the script**, not
   signer/role order. Pubkeys pushed `pkA, pkB, pkArb` → sigs must be pushed
   in ascending index order of their matching pubkey, or CHECKMULTISIG fails
   with `not all signatures empty on failed checkmultisig` (NULLFAIL).
4. **Kaspa's `OpCheckMultiSig` has NO Bitcoin-style off-by-one dummy
   element.** Do not push an extra `OP_0`/`OP_FALSE` before the sigs — it's
   never consumed and fails as `stack contains 1 unexpected items`.
5. **Kaspa's `OpCheckLockTimeVerify` POPS the locktime value off the stack**
   (unlike Bitcoin's peek-only CLTV). Do **not** add an `OP_DROP` after it —
   that's Bitcoin-only convention and here it drops the wrong stack item.
6. **`PendingTransaction.transaction` is a snapshot, not a live handle.**
   Mutating `pendingTx.transaction.lockTime` / `.inputs[i].sequence` does
   **not** persist into what `.submit()` sends. For a custom `lockTime` or
   `sequence` (the CLTV reclaim path), build with the low-level
   `createTransaction(entries, outputs, priorityFee, payload?, sigOpCount?)`,
   mutate directly, sign with `createInputSignature(txn, idx, key)`, submit
   via `rpc.submitTransaction({ transaction: txn, allowOrphan: false })`.
7. **CLTV enforcement is two separate checks:** the in-script
   `OpCheckLockTimeVerify` only verifies `stack_value <= tx.lock_time`; the
   actual "can't be mined before real time/DAA passes" gate is
   `check_tx_is_finalized` (consensus, compares `tx.lock_time` against the
   including block's real DAA score). Both require
   `input.sequence != MAX_TX_IN_SEQUENCE_NUM` to engage.
8. **Test-harness gotcha:** an "early reclaim" test needs a real time margin
   between "now" and the reclaim deadline — polling/setup alone can burn
   10–30s, so a tight margin makes an "expect REJECT" case false-pass. Not an
   issue for the real 14-day window; matters for spike/test scripts.

Reference spike code (mainnet-dust-tested, protocol rules only — needs a
fresh non-Dagger wallet-derivation core to actually run): `service/spikes.mjs`.

---

## 4. Bot (`bot/`) — alerts only

No wallet, no game state, no keys. Purpose: push notifications to a player's
Telegram, driven by webhooks from the backend API.

- `/start` — generates a one-time link code; player pastes it into the site
  to connect their Telegram for alerts.
- `/alerts on|off` — toggle notifications.
- `/unlink` — disconnect.
- Internal webhook endpoint (localhost-bound, shared-secret auth) the site
  backend calls: `notify_challenge`, `notify_your_move`, `notify_clock_warning`,
  `notify_settled`.

Discord alerts are the same shape, phase 2 — a lot of the Kaspa community is
there.

---

## 5. Game rules (unchanged from the original engine design)

| End | Result | Pot |
|---|---|---|
| Checkmate | winner | pot − rake → winner |
| Resign | opponent wins | same |
| Flag (incl. daily timeout) | opponent wins | same |
| Draw: agreement / stalemate / insufficient / 75-move / fivefold / claimed 3-fold / claimed 50-move | ½–½ | (pot − rake) / 2 each |
| Abort (pre-move-2 / unfunded) | none | full refund, no rake |

No on-chain mate detection: a mated/stalling player either resigns or flags.
Illegal moves can't exist (python-chess rejects them before they touch
state). First 2 plies are grace — a flag before either side has moved once is
an ABORT + refund, not a win (stops instant-flag griefs on accept).

---

## 6. Later phases (park, don't build yet)

- **Tournaments:** entry fees to one arbiter escrow, single-elim bracket of
  normal matches, payout 70/20/10 − rake.
- **Discord alerts** — mirror of the TG alert bot.
- **Trustless covenant chess-clock:** stateful covenant holds pot +
  board-hash + deadline; mutual-signed off-chain states, on-chain flag
  claims — removes the arbiter key entirely. Needs Silverscript / KIP-16
  vprogs.
- Compliance note before any public launch: wagering real money on skill
  games is jurisdiction-sensitive — sanity-check before opening publicly.
