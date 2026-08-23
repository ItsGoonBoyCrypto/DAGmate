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
  — every move a player makes is recorded on Kaspa L1, not just settlement
- Challenges: direct challenge to a specific opponent for an agreed KAS
  stake, or a 0/near-0-stake "gas-only" friendly; per-account toggle to
  accept or block incoming challenges (see §7)
- Tournaments: auto-starting bracket once a fee tier's lobby fills (see §8)
- Learn page: chess-basics curriculum with gas-gated levels + optional AI
  teacher (see §9)
- Telegram alert bot: challenge received, your move, clock warning, match
  settled — notifications only, no wallet access, no game logic
- Kill switch, idempotency, stake caps, owner gate before any public launch
- Clean, minimal flat-2D board visual design (see §10) — not gamified

**OUT (later phases):**
- Discord alerts (planned, phase 2 — many Kaspa users are there)
- Trustless covenant chess-clock (removes the arbiter entirely — needs
  Silverscript / KIP-16 vprogs, testnet-10 first)
- Trustless covenant tournament pot (see §8's open design question — a true
  single shared N-way pot needs either pre-signed delegated settlement or a
  real covenant; v1 tournaments ship without it)

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

### 1.1 Identity — who the backend believes you are

A Kaspa address is public; it is printed on the match view. So an address in a
request body proves nothing, and treating one as identity made a funded match
takeable with no cryptography at all: challenge someone, wait for both stakes
to land, then `POST /resign` naming *their* address. Settlement would then
quite legitimately pay the attacker. The escrow signatures were never the weak
part — the game **result** was.

Identity is therefore a signature, not a string:

1. `POST /api/auth/nonce {address}` → a single-use nonce and the exact text to
   sign.
2. The wallet signs that text (`signMessage`, supported by Kasware and Kastle).
   Deliberately **not** `signPskt` — a login that asks players to sign a
   transaction teaches a habit that gets them robbed on some other site.
3. `POST /api/auth/verify {address, pubkey, nonce, signature}` → the sidecar
   checks it, and we return a session token.
4. Every mutating endpoint takes its account from that token via
   `Depends(require_account)` and **ignores any address in the body**.

Three properties carry the weight:

- **The signed message is rebuilt server-side** from the stored nonce row,
  never accepted from the client. Otherwise any signature that player had ever
  produced anywhere could be replayed as a login.
- **The nonce is single-use**, via a guarded `UPDATE ... WHERE used_ts IS NULL`.
  A signature stays valid forever, so this is the entire replay defence.
- **The pubkey must derive to the claimed address**, enforced in
  `service/auth.js` where the key maths lives. A signature alone proves
  *somebody* signed; the address derivation is what makes it proof of *who*.

Session tokens are random and stored only as a SHA-256 hash, so read access to
the DB doesn't hand out live logins. Expiry and revocation are enforced inside
the lookup query, so no endpoint can forget to check them.

The stored pubkey matters beyond login: it is what escrows are built from.
`get_or_create_account` is reachable only from the verify path, after the
sidecar has proven the key belongs to the address — otherwise an attacker
could repoint a victim's account at their own key.

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

### 2.4 Reclaim — spending the ELSE branch

Built: `service/escrow.js` (`buildReclaimUnsigned` / `broadcastReclaim`),
`site/backend/reclaim.py`, `tools/test_reclaim.py`.

Two-visit and non-custodial, like settlement: the backend builds an unsigned
tx, the depositor's own wallet signs it, the backend relays it. The ELSE
branch is single-sig on the depositor's key, which DAGmate does not hold, so
there is no version of this the service can do alone — which is the point.

Unlike settlement it is stateless. One signer in one sitting means a rebuild
costs nothing, so no tx is stored; only the resulting txid, as a receipt.

Four rules, in descending order of how expensive they'd be to get wrong:

1. **Never offered while the pot can still be won.** After the timelock the
   *script* lets a loser walk their own stake back, and no backend policy
   changes that. What the backend controls is whether DAGmate helps, and it
   does not build that tx for a match a winner can still claim — otherwise
   "wait two weeks" is a free undo on a lost game. Eligible: `expired`,
   `awaiting_deposit`, and `settled` with a pot too small to release
   (gas-only, where the 2-of-3 branch is open but useless).
2. **The escrow follows from the session, not the request.** Player A
   reclaims escrow A; there is no parameter for it. Same for the payout
   address and the redeem script — both come from the account and the DB.
3. **The tx uses the low-level `createTransaction()`**, because a
   `PendingTransaction`'s `.transaction` is a snapshot and an assigned
   `lockTime` doesn't persist (§3 rule 6). ⚠️ That builder has **no automatic
   change output**: the output is computed as `total − fee` exactly, or the
   remainder is silently donated to a miner.
4. **`lockTime = reclaimDaa` and every input's `sequence ≠ MAX`** — without
   the second, both the in-script CLTV and the consensus finality check are
   skipped and the timelock is decorative (§3 rules 7–8). `lockTime` and
   `sequence` were verified to survive `serializeToSafeJSON()` →
   `deserializeFromSafeJSON()`, which they must, since the tx crosses to the
   player's wallet as JSON and back.

The match view publishes each escrow's address, redeem script and reclaim DAA,
and the UI prints them next to the button whether or not the button works.
That is the disaster path above made checkable: with those three values a
player spends the branch from any Kaspa tooling, no server involved.

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
POST /escrow/reclaim-unsigned  { address, depositorAddr, reclaimDaa }
     → { txJson, inputs, totalSompi, feeSompi, payoutSompi, tipDaa, reclaimDaa }
                                          // ELSE-branch drain of ONE escrow;
                                          // refuses before the timelock opens
POST /escrow/reclaim-broadcast  { txJson, redeemHex, sigs }
     → { txid }                           // witness is <sig> OP_FALSE
POST /escrow/anchor  { matchId, payloadHex, feeSompi }
     → { txid }
GET  /escrow/daa      → { daaScore }
POST /auth/verify-message  { address, pubkey, message, signature }
     → { ok, reason }                     // always 200; a rejected login is a
                                          // normal outcome, not an error
```

### 3.0 Which node it talks to

DAGmate runs no node. `DAGMATE_KASPA_WRPC` pins one if you want it; unset, the
SDK's `Resolver` fetches a public node from the community pool for the
configured network — **once**, after which that URL is reused (see "staying
put" below).

That is safe because the node is a **liveness** dependency, not a trust one.
Every transaction is fully signed before a node sees it, so a hostile node can
refuse to answer or refuse to relay — it cannot alter a transaction without
invalidating the signatures, and it cannot invent a deposit, because deposits
are re-read and re-checked against the escrow's own address.

What it *can* do is answer wrongly, and two of those answers cost money — so
`core.withRpc` health-gates every connection and moves on to another node if it
fails:

1. **`isSynced`** — the virtual DAA score decides deposit confirmation and
   whether a timelock has opened. A lagging node writes a reclaim DAA that
   locks funds past 14 days, or never confirms a deposit that landed.
2. **`hasUtxoIndex`** — `getUtxosByAddresses` is how every deposit is seen and
   every spend is funded. Without the index the node cannot answer at all.
3. **`networkId` matches** — only reachable via an explicit URL, since the
   resolver picks per network. A mainnet service pointed at a testnet node
   derives mainnet addresses and reads a testnet UTXO set: every escrow looks
   empty and every deposit it does see is play money.
4. **`wss://`, not `ws://`** — the resolver may return a plaintext endpoint.
   Every address this service queries crosses that link, which is the full set
   of live match escrows. Refused on the resolver path; allowed if the operator
   set an explicit URL, since a private or localhost node is a fair reason to
   skip TLS. (Note the SDK's `Resolver({ tls: true })` is not usable here — it
   throws unless you also supply your own resolver list, despite `urls` being
   typed optional. Asserting on the URL we got is stronger anyway.)

The retry covers connect and health-check only, never the call itself — once
the call has run it may have broadcast, and a second attempt on another node
would double-spend or double-anchor.

**Staying put.** Having found a good node, `withRpc` reuses its URL rather than
re-resolving. That is correctness, not speed:

- **Mempool ancestry.** `withRpc` opens and closes a connection per call, so
  unpinned, consecutive calls land on different nodes. Move anchors chain —
  anchor N+1 spends the change from anchor N — so a fast game submits a child
  to a node that never saw its parent, and it orphans.
- **Cost per call.** Resolving is an HTTP round-trip before the WebSocket
  handshake, and `withRpc` serializes, so every operation would queue behind
  the previous one's discovery.

⚠️ The pin is dropped **only when the node is what failed** — connect failure,
health-gate failure, or a transport-shaped error from the call. A *rejected
transaction* must never drop it: a rejection is usually the node correctly
telling us something about our own transaction, and switching nodes in response
means the next transaction we build is a child of a parent the new node has
never seen. Both of these are Dagger `kron-service` lessons, reproduced here
rather than re-learned.

### 3.1 Dev routes — opt-in, and refused on mainnet

`DAGMATE_DEV_ROUTES=1` adds `/dev/demo-keypair` and `/dev/sign-message` here,
and `/api/dev/*` plus `/api/matches/{id}/dev-mark-funded` on the backend. That
last one flips a match to live with no on-chain check — it conjures a pot.

Two rules, both deliberate:

- **Opt-in, never opt-out.** Forgetting to switch it on costs a developer two
  minutes; forgetting to switch it off puts a free-money button on a public
  host. Anything that reads "convenient by default" gets inherited by a
  deployment.
- **Ignored on mainnet whatever the env says.** A demo wallet on mainnet is a
  throwaway key holding real money handed to someone who was told it's for
  testing. There's no legitimate reason to want it, so an env var isn't
  allowed to ask.

Disabled routes return **404, not 403** — probing a deployment shouldn't
reveal what's switched off. The backend reports the flag through
`GET /api/meta` (`devRoutes`) so the UI drops the demo-wallet fallback and the
"mark funded" button on its own, rather than keeping a second copy of the
answer that has to be remembered at deploy time.

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

### 5.1 Draw by agreement

The only ending that needs BOTH players to act, and it splits a pot one side
would otherwise take whole — so it's a money decision on two clicks, which
makes it the most attractive ending on the board to forge. Four rules, all
enforced as WHERE clauses on the statement that acts on them (`database.py`),
never as a check the route does first:

1. **You cannot accept your own offer** (`draw_offer_by <> accepter`). Without
   this, a losing player offers a draw and immediately takes half the pot.
2. **Accepting settles in one statement** — the offer's existence is part of
   the settlement UPDATE. A check-then-settle would let a move or a clock flag
   land in the gap and record an agreement nobody currently held.
3. **Playing on withdraws your offer** — cleared by the same guarded UPDATE
   that commits the move, because playing on IS how you decline. An offer that
   outlived its position could be banked and cashed twenty moves later by
   whoever turned out to be losing.
4. **One offer per position** — `draw_offer_ply` survives a decline, so the
   offer can't be re-sent until someone moves. Otherwise "decline" is just a
   button that makes the nag reappear.

Settlement is unchanged: an agreed draw is `winner_account_id IS NULL`, which
is the same signal stalemate and a 6.9 flag already produce, so it takes the
existing two-signer draw path (§2.3) with no special case.

---

## 7. Challenges & settings

Two ways to start a match:
- **Direct challenge** — pick an opponent (search / recent opponents on the
  site), propose a KAS stake, they accept or decline. Same escrow/settlement
  path as the rest of the spec.
- **Gas-only challenge** — stake is 0 or dust; the point isn't the wager,
  it's a free/near-free way to play with every move still anchored on-chain
  (each move still costs the small anchor fee, paid from DAGmate's own
  operating address per §2.3/§3 — never the player's wallet).

**Accept challenges toggle** — a profile setting (`accept_challenges:
on|off`, default on). When off, this account cannot be challenged by anyone
(site rejects the attempt with a clear "not accepting challenges" message).
Doesn't affect tournaments (those are opt-in by joining a lobby, not
challenge-based).

---

## 8. Tournaments

Auto-running, fee-tiered brackets — no manual "start tournament" step.

- **Fee tiers:** 20 / 100 / 250 / 500 KAS entry (config-driven list, not
  hardcoded — easy to add/remove tiers later).
- **Auto-start:** each tier has its own lobby; once it reaches the
  configured minimum entrant count (default **8**), it locks and the
  bracket pairs off automatically.
- **Payout:** winner takes the pot (entry fees × entrants) minus rake. No
  placed (2nd/3rd) payout in v1 — confirm with GoonBoy if that's still wanted,
  since it's a change from the old placeholder split.

**⚠️ Open design question — how the pot is actually escrowed.** A literal
single shared pot that only the eventual sole winner can sweep is *not* the
same problem as the 1v1 2-of-3 escrow: an eliminated entrant has zero
incentive to help co-sign their stake away to someone else, and the arbiter
should never get unilateral spend authority over a non-custodial deposit.
Two realistic v1 paths:

  - **A — bracket-of-matches (reuses the 1v1 engine as-is, zero new escrow
    code):** each round is a normal 2-of-3 stake-tier match; each round's
    winner is paid out immediately via the existing settlement path, then
    re-stakes into a fresh escrow for the next round. Fast to ship, zero new
    trust assumptions, but it's a *compounding bracket*, not one literal
    shared pot sitting in a single address the whole time.
  - **B — true shared pot:** every entrant's stake sits in one place until
    the bracket resolves. Doing this non-custodially needs either (i) each
    entrant pre-signing a conditional/delegated settlement authorization at
    entry time (one extra wallet-connect signature at signup — needs
    verifying Kasware/Kastle actually support that shape of pre-auth), or
    (ii) a real covenant (KIP-16/Silverscript) holding the pot
    programmatically, which isn't available yet.

**Recommendation:** ship **A** first — it matches the non-custodial
guarantee already built for 1v1 with no new escrow design, and can be
reframed later as **B** once covenants or a verified pre-auth flow land.
Needs GoonBoy's sign-off before building either way.

---

## 9. Learn page

- Structured curriculum: rules → tactics → openings → endgames, difficulty
  ramps as levels progress.
- Each level unlocks with a small KAS "gas" fee — a plain one-way send from
  the player's own wallet to DAGmate's operating address, **not** an escrow
  (there's no wager, no counterparty, nothing to settle).
- Free practice mode: play against a bundled low-skill engine (e.g. a weak
  Stockfish level) before any gated content.
- Optional **AI teacher**: chat-based hints/explanations per lesson
  (LLM-backed). Needs its own design pass before building — model choice,
  per-call cost, rate limits/abuse controls — flagged here, not scoped yet.
- Funnel: learn → practice vs bot → gas-only friendly → real-stake
  challenge/tournament.

---

## 10. Visual design

Clean, minimal flat-2D board — classic green/cream squares, flat-shaded
white/black piece set (reference: GoonBoy's supplied board image, chess.com/
lichess-default style). No 3D pieces, no heavy skinning, no game-y clutter.
Board is the hero element; wallet-connect status, clock, and match controls
stay small and out of the way.

---

## 11. Later phases (park, don't build yet)

- **Discord alerts** — mirror of the TG alert bot.
- **Trustless covenant chess-clock:** stateful covenant holds pot +
  board-hash + deadline; mutual-signed off-chain states, on-chain flag
  claims — removes the arbiter key entirely. Needs Silverscript / KIP-16
  vprogs.
- **Trustless covenant tournament pot** — see §8 option B once covenants or
  a verified wallet pre-auth flow are available.
- Compliance note before any public launch: wagering real money on skill
  games is jurisdiction-sensitive — sanity-check before opening publicly.
