/**
 * DAGmate — wallet-ownership proof (docs/DAGMATE_SPEC.md §3).
 *
 * The site backend has no cryptography of its own: it decides WHAT string a
 * player must sign, and this module decides whether they actually signed it.
 * Keeping the two apart means the backend can never accidentally "verify" a
 * signature by comparing strings.
 *
 * ⚠️ THE ADDRESS CHECK IS THE WHOLE POINT. `verifyMessage` only proves that
 * the holder of *some* private key signed the message — on its own it says
 * nothing about which account that is, so anyone could sign with their own
 * key and claim someone else's address. Deriving the address FROM the
 * supplied pubkey and requiring it to equal the claimed one is what turns a
 * signature into an identity.
 *
 * Fails closed everywhere: a malformed pubkey, a wrong-length signature and a
 * forged signature all return the same `{ ok: false }` rather than throwing,
 * so a login attempt can never 500 its way past the check.
 */
import * as core from './core.js';

/**
 * Does `signature` prove that the holder of `address` signed `message`?
 * @returns {{ok: boolean, reason?: string}}
 */
export function verifyOwnership({ address, pubkey, message, signature }) {
  if (!address || !pubkey || !message || !signature) {
    return { ok: false, reason: 'address, pubkey, message and signature are all required' };
  }
  const k = core.wasm();

  let derived;
  try {
    derived = new k.PublicKey(String(pubkey)).toAddress(core.netType()).toString();
  } catch (e) {
    return { ok: false, reason: 'unreadable public key' };
  }
  if (derived !== String(address)) {
    // Either a genuine wallet/network mismatch, or someone signing with their
    // own key while claiming another player's address. Same answer for both.
    return { ok: false, reason: 'public key does not belong to that address' };
  }

  let ok = false;
  try {
    ok = k.verifyMessage({ message: String(message), signature: String(signature), publicKey: String(pubkey) });
  } catch (e) {
    return { ok: false, reason: 'malformed signature' };
  }
  return ok ? { ok: true } : { ok: false, reason: 'signature does not match' };
}

/**
 * Dev-only: sign a message with a throwaway demo-wallet key, so the local
 * click-through flow goes through the REAL auth path (nonce → signature →
 * verification) instead of around it. Never reachable when DAGMATE_DEV_ROUTES=0.
 */
export function signWithDemoKey({ privateKeyHex, message }) {
  const k = core.wasm();
  const key = new k.PrivateKey(String(privateKeyHex));
  return { signature: k.signMessage({ message: String(message), privateKey: key }) };
}
