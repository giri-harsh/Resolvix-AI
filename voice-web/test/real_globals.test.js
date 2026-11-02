// No injected fakes: the code runs against the real timers/fetch of the test runtime. Browsers throw "Illegal invocation"
// when a stored global is called with another object as `this`, Node does not, so these tests install browser-like
// strict globals first. Do not use fakeClock here.
import test from "node:test";
import assert from "node:assert/strict";
import { IdleTimer, TurnController } from "../src/control.js";
import { endCall, health, sendTurn } from "../src/api.js";
import { createServer } from "../mock_server/server.js";
import { describeVoice, recognitionIssue } from "../src/text.js";

/** Replace globals by wrappers that throw like a browser when called with a foreign `this`. Returns a restore function. */
function strictGlobals(names = ["setTimeout", "clearTimeout", "fetch"]) {
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

async function withStrict(fn) {
  const restore = strictGlobals();
  try { await fn(); } finally { restore(); }
}

test("IdleTimer with the real setTimeout fires, resets and stops", () => withStrict(async () => {
  let fired = 0;
  const t = new IdleTimer(40, () => fired++);
  t.start();
  await new Promise((r) => globalThis.setTimeout(r, 15));
  t.reset(); // re-arms: must not have fired yet
  await new Promise((r) => globalThis.setTimeout(r, 25));
  assert.equal(fired, 0);
  await new Promise((r) => globalThis.setTimeout(r, 60));
  assert.equal(fired, 1);
  const t2 = new IdleTimer(20, () => fired++);
  t2.start(); t2.pause(); t2.resume(); t2.stop();
  await new Promise((r) => globalThis.setTimeout(r, 50));
  assert.equal(fired, 1);
}));

test("turn client uses the real fetch (turn, end, health)", () => withStrict(async () => {
  const server = createServer({ latencyMs: 0 });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const url = `http://127.0.0.1:${server.address().port}`;
  try {
    assert.equal(await health({ runtimeUrl: url }), "ok");
    const t = await sendTurn({ runtimeUrl: url, callId: null, text: "fees kab tak jama karni hai" });
    assert.match(t.callId, /^call-test-/);
    assert.equal((await endCall({ runtimeUrl: url, callId: t.callId })).ok, true);
  } finally { await new Promise((r) => server.close(r)); }
}));

test("TurnController speaks through a synth that checks `this`, and barge-in cancels it", () => {
  const spoken = [];
  const synth = {
    speaking: false, pending: false,
    speak(u) { if (this !== synth) throw new TypeError("Illegal invocation"); spoken.push(u); },
    cancel() { if (this !== synth) throw new TypeError("Illegal invocation"); spoken.length = 0; },
  };
  const c = new TurnController({ synth, makeUtterance: (text) => ({ text }) });
  const { turnId } = c.beginTurn();
  c.speak(turnId, ["one.", "two."], { voice: { name: "v", lang: "en-IN" }, lang: "en-IN" });
  assert.equal(spoken.length, 2);
  assert.equal(c.bargeIn().cancelledSpeech, true);
  assert.equal(spoken.length, 0);
});

test("recognition errors map to messages and behaviour", () => {
  assert.match(recognitionIssue("network").message, /Chrome or Edge.*VPN/);
  assert.equal(recognitionIssue("network").stop, true);
  assert.match(recognitionIssue("not-allowed").message, /Microphone blocked/);
  assert.equal(recognitionIssue("service-not-allowed").stop, true);
  for (const code of ["no-speech", "aborted"]) assert.deepEqual(recognitionIssue(code), { message: "", restart: true, stop: false });
  assert.match(recognitionIssue("weird").message, /weird/);
});

test("describeVoice names the voice or the fallback", () => {
  assert.match(describeVoice({ name: "Google हिन्दी", lang: "hi-IN" }, "hi-IN"), /hi-IN/);
  assert.match(describeVoice(null, "en-IN"), /default voice for en-IN/);
});
