// Side-effect shim imported BEFORE the vendored noble lib so its top-level `self.crypto` resolves
// under Node (the byte-match test). ES imports are evaluated in source order, so importing this first
// guarantees `self` exists before noble-secp256k1.js's module body runs. No-op in the browser.
if (typeof self === 'undefined') { globalThis.self = globalThis; }
