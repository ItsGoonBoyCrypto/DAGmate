/**
 * DAGmate — generate the master seed. Run this ON THE SERVER, once, ever.
 *
 *     node tools/genseed.mjs
 *
 * This prints the one secret in the whole system: the BIP39 phrase every
 * arbiter co-signing key is derived from. Whoever holds it can co-sign any
 * match settlement.
 *
 * Two rules, both about where the phrase has been rather than how strong it is:
 *
 * 1. **Generate it on the machine that will use it.** A phrase generated on a
 *    laptop and pasted into a terminal has been through a clipboard, a shell
 *    history, and possibly a scrollback buffer that syncs somewhere. Generated
 *    here, it goes from this process straight into a root-only file.
 *
 * 2. **Never reuse a seed from another project.** DAGmate's seed is DAGmate's.
 *    Sharing one with, say, a trading bot means a compromise of either is a
 *    compromise of both, and it silently links funds that were meant to be
 *    unrelated.
 *
 * It also prints the derived OPERATING address (account 0, index 0). That is a
 * public address, safe to write down, and it is what pays for on-chain move
 * anchors — so it needs a small KAS float. Fund it, and check it here after,
 * because a wrong seed derives a different address and the mistake is
 * otherwise invisible until anchors start failing.
 *
 * The phrase is NOT written to disk by this script on purpose: writing it would
 * mean choosing a path, a mode and an owner on your behalf, and getting any of
 * those wrong is worse than making you paste it once. See deploy/README.md.
 */
import { loadKaspa } from '@kronsdk/kron-sdk/wasm';

const NETWORK_ID = process.env.DAGMATE_NETWORK_ID || 'mainnet';

const k = await loadKaspa();
const mnemonic = k.Mnemonic.random(24);
const phrase = mnemonic.phrase;

const xprv = new k.XPrv(mnemonic.toSeed());
const netType = NETWORK_ID === 'mainnet' ? k.NetworkType.Mainnet : k.NetworkType.Testnet;
const operating = new k.PrivateKeyGenerator(xprv, false, 0n)
  .receiveKey(0).toPublicKey().toAddress(netType).toString();

console.log(`
network:  ${NETWORK_ID}

DAGMATE_MASTER_MNEMONIC — the only secret in the system. Put it in the env
file, then clear your scrollback. Anyone with this can co-sign settlements.

  ${phrase}

OPERATING ADDRESS (public — this is derived from the phrase above, account 0
index 0). Move anchors are paid from here, so send it a small KAS float:

  ${operating}
`);
process.exit(0);
