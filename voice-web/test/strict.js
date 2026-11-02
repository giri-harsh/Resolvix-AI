// Browser-like strict globals for tests: calling setTimeout/clearTimeout/fetch with a foreign `this` throws "Illegal invocation",
// as it does in a browser (Node does not check). Install before constructing the code under test; call the returned function to restore.
export function strictGlobals(names = ["setTimeout", "clearTimeout", "fetch"]) {
  const saved = {};
  for (const name of names) {
    const real = globalThis[name];
    saved[name] = real;
    globalThis[name] = function strict(...args) {
      if (this !== undefined && this !== globalThis) throw new TypeError("Illegal invocation");
      return real.apply(globalThis, args);
    };
  }
  return () => { for (const [n, f] of Object.entries(saved)) globalThis[n] = f; };
}

export async function withStrict(fn) {
  const restore = strictGlobals();
  try { await fn(); } finally { restore(); }
}

export async function withMock(opts, fn) {
  const { createServer } = await import("../mock_server/server.js");
  const server = createServer(opts);
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const url = `http://127.0.0.1:${server.address().port}`;
  try { await fn(url, server); } finally { await new Promise((r) => { server.closeAllConnections?.(); server.close(r); }); }
}
