import test from "node:test";
import assert from "node:assert/strict";
import { SSEParser, TurnAccumulator, endVoiceCall, shouldFallback, startCall, streamTurn, voiceConfig } from "../src/stream.js";
import { ApiError, sendTurn } from "../src/api.js";
import { TurnController } from "../src/control.js";
import { spellForSpeech } from "../mock_server/server.js";
import { withMock, withStrict } from "./strict.js";

const sleep = (ms) => new Promise((r) => globalThis.setTimeout(r, ms));
const msg = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

// ------------------------------------------------------------------ SSE parser
test("SSEParser: whole messages, in order, with the JSON parsed", () => {
  const p = new SSEParser();
  const out = p.feed(msg("meta", { path: "faq" }) + msg("speech", { index: 0, text: "Hello." }));
  assert.deepEqual(out, [{ event: "meta", data: { path: "faq" } }, { event: "speech", data: { index: 0, text: "Hello." } }]);
});

test("SSEParser: a message split at any byte is delivered once, after its blank line", () => {
  const raw = msg("speech", { index: 0, text: "Fee ki last date 15 July hai." }) + msg("done", { ended: false });
  for (let cut = 1; cut < raw.length; cut += 7) {
    const p = new SSEParser();
    const got = [...p.feed(raw.slice(0, cut)), ...p.feed(raw.slice(cut))];
    assert.deepEqual(got.map((m) => m.event), ["speech", "done"], `cut at ${cut}`);
  }
});

test("SSEParser: CRLF, comments, multi-line data, non-JSON data and a final block without a blank line", () => {
  const p = new SSEParser();
  const out = p.feed(": keep-alive\r\n\r\nevent: speech\r\ndata: {\"index\":0,\r\ndata: \"text\":\"a\"}\r\n\r\nevent: note\r\ndata: plain\r\n\r\n");
  assert.deepEqual(out, [{ event: "speech", data: { index: 0, text: "a" } }, { event: "note", data: "plain" }]);
  assert.deepEqual(p.feed("event: done\ndata: {\"ended\":true}"), []);
  assert.deepEqual(p.flush(), [{ event: "done", data: { ended: true } }]);
  assert.deepEqual(p.flush(), []);
});

test("SSEParser: Hindi text survives the decoder when a character is split between chunks", async () => {
  const bytes = new TextEncoder().encode(msg("speech", { index: 0, text: "फीस की आखिरी तारीख" }));
  const dec = new TextDecoder();
  const p = new SSEParser();
  const got = [...p.feed(dec.decode(bytes.slice(0, 40), { stream: true })), ...p.feed(dec.decode(bytes.slice(40), { stream: true }))];
  assert.equal(got[0].data.text, "फीस की आखिरी तारीख");
});

// ------------------------------------------------------------------ accumulator
test("TurnAccumulator: summary from meta, speech, actions and done; cancelled turn", () => {
  const a = new TurnAccumulator();
  a.add({ event: "meta", data: { path: "escalation", tier: "escalate", lang: "hi", interruptable: false } });
  a.add({ event: "speech", data: { index: 1, text: "Second." } });
  a.add({ event: "speech", data: { index: 0, text: "First." } });
  a.add({ event: "action", data: { kind: "create_ticket", ticket_id: "EX-1042", priority: "P1" } });
  a.add({ event: "action", data: { kind: "transfer", simulated: true } });
  a.add({ event: "done", data: { priority: "P1", ticket_ids: ["EX-1042"], ended: false, latency_ms: 412 } });
  assert.deepEqual(a.sentences, ["First.", "Second."]);
  assert.equal(a.interruptable, false);
  assert.deepEqual(a.summary(), { lang: "hi", path: "escalation", tier: "escalate", interruptable: false, priority: "P1", ticketId: "EX-1042", actionTypes: ["create_ticket", "transfer"], transferred: true, ended: false, latencyMs: 412 });
  const c = new TurnAccumulator();
  c.add({ event: "error", data: { code: "conflict", message: "The turn was cancelled (barge-in)." } });
  assert.equal(c.cancelled, true);
  const f = new TurnAccumulator();
  f.add({ event: "error", data: { code: "internal" } });
  assert.equal(f.cancelled, false);
});

test("spellForSpeech writes ticket ids the way the runtime does", () => {
  assert.equal(spellForSpeech("Ticket number EX-1042."), "Ticket number E X one zero four two.");
});

// ------------------------------------------------------------------ against the streaming mock (real fetch, strict globals)
test("start call, stream a turn sentence by sentence, end call", () => withStrict(() => withMock({ latencyMs: 60, sentenceDelayMs: 120, chunkSplit: true }, async (url, server) => {
  const c = await startCall({ runtimeUrl: url });
  assert.match(c.callId, /^call-voice-/);
  assert.match(c.greeting, /Namaste/);
  assert.equal((await voiceConfig({ runtimeUrl: url })).streaming, true);

  const t0 = Date.now();
  const seen = [];
  await streamTurn({ runtimeUrl: url + "/", callId: c.callId, text: "fees kab tak jama karni hai", onEvent: (m) => seen.push({ ...m, at: Date.now() - t0 }) });
  assert.deepEqual(seen.map((m) => m.event), ["meta", "speech", "speech", "done"]);
  const speech = seen.filter((m) => m.event === "speech");
  assert.deepEqual(speech.map((m) => m.data.index), [0, 1]);
  assert.ok(speech[1].at - speech[0].at >= 90, `sentences arrive progressively (${speech[1].at - speech[0].at} ms apart)`);
  assert.equal(server.turnLog.length, 1);

  const end = await endVoiceCall({ runtimeUrl: url, callId: c.callId });
  assert.equal(end.status, "ended");
  assert.equal(end.ai_resolved, true);
  assert.deepEqual(await endVoiceCall({ runtimeUrl: url, callId: c.callId }), { ok: true, alreadyEnded: true }); // 404 after the end is fine
  assert.deepEqual(await endVoiceCall({ runtimeUrl: url, callId: null }), { ok: true, skipped: true });
})));

test("ticket turn: action events carry the ticket id, speech has it spelled out", () => withStrict(() => withMock({ latencyMs: 0 }, async (url) => {
  const c = await startCall({ runtimeUrl: url });
  const acc = new TurnAccumulator();
  await streamTurn({ runtimeUrl: url, callId: c.callId, text: "Exam 15 minute mein hai, room nahi mil raha", onEvent: (m) => acc.add(m) });
  const s = acc.summary();
  assert.equal(s.ticketId, "EX-1042");
  assert.equal(s.priority, "P1");
  assert.equal(s.interruptable, false);
  assert.ok(acc.sentences.join(" ").includes("E X one zero four two"));
})));

test("error envelopes: unknown call, empty text, over-long text", () => withStrict(() => withMock({ latencyMs: 0 }, async (url) => {
  await assert.rejects(streamTurn({ runtimeUrl: url, callId: "nope", text: "hi", onEvent() {} }), (e) => e instanceof ApiError && e.status === 404 && e.code === "not_found");
  const c = await startCall({ runtimeUrl: url });
  await assert.rejects(streamTurn({ runtimeUrl: url, callId: c.callId, text: "   ", onEvent() {} }), (e) => e.status === 422 && e.code === "validation_error");
  await assert.rejects(streamTurn({ runtimeUrl: url, callId: c.callId, text: "x".repeat(601), onEvent() {} }), (e) => e.status === 422);
})));

// ------------------------------------------------------------------ barge-in: abort the stream
test("aborting the stream mid-turn stops delivery and cancels the turn on the server", () => withStrict(() => withMock({ latencyMs: 40, sentenceDelayMs: 150 }, async (url, server) => {
  const c = await startCall({ runtimeUrl: url });
  const ctl = new AbortController();
  const seen = [];
  const p = streamTurn({ runtimeUrl: url, callId: c.callId, text: "fees kab tak jama karni hai", signal: ctl.signal, onEvent: (m) => { seen.push(m.event); if (m.event === "speech") ctl.abort(); } });
  await assert.rejects(p, (e) => e.name === "AbortError");
  await sleep(300);
  assert.deepEqual(seen, ["meta", "speech"]); // the second sentence never arrived
  assert.equal(server.cancelled, 1);
  // the call is usable again: a new turn is accepted at once
  const acc = new TurnAccumulator();
  await streamTurn({ runtimeUrl: url, callId: c.callId, text: "hello", onEvent: (m) => acc.add(m) });
  assert.ok(acc.done);
})));

test("a turn that is not interruptable keeps running when the browser disconnects", () => withStrict(() => withMock({ latencyMs: 30, sentenceDelayMs: 60 }, async (url, server) => {
  const c = await startCall({ runtimeUrl: url });
  const ctl = new AbortController();
  const p = streamTurn({ runtimeUrl: url, callId: c.callId, text: "ambulance chahiye", signal: ctl.signal, onEvent: (m) => { if (m.event === "meta") ctl.abort(); } });
  await assert.rejects(p, (e) => e.name === "AbortError");
  await sleep(300);
  assert.equal(server.cancelled, 0);
})));

// ------------------------------------------------------------------ fallback to /api/test/*
test("shouldFallback: route missing, runtime down and server errors fall back; aborts and 4xx do not", () => {
  for (const status of [0, 404, 405, 501, 500, 503]) assert.equal(shouldFallback(new ApiError("x", status)), true, String(status));
  for (const status of [401, 409, 422, 429]) assert.equal(shouldFallback(new ApiError("x", status)), false, String(status));
  const abort = new Error("aborted"); abort.name = "AbortError";
  assert.equal(shouldFallback(abort), false);
  assert.equal(shouldFallback(null), false);
});

test("a runtime without /api/voice answers 404 to startCall, and /api/test/turn still works", () => withStrict(() => withMock({ latencyMs: 0, streaming: false }, async (url) => {
  const err = await startCall({ runtimeUrl: url }).catch((e) => e);
  assert.ok(err instanceof ApiError);
  assert.equal(err.status, 404);
  assert.equal(shouldFallback(err), true);
  const r = await sendTurn({ runtimeUrl: url, callId: null, text: "fees kab tak jama karni hai" });
  assert.match(r.callId, /^call-test-/);
  assert.equal(r.summary.path, "faq");
  assert.equal(await voiceConfig({ runtimeUrl: url }), null);
})));

test("an unreachable runtime is status 0, which also falls back (and then fails with a readable message)", async () => {
  const err = await startCall({ runtimeUrl: "http://127.0.0.1:1" }).catch((e) => e);
  assert.equal(err.status, 0);
  assert.match(err.message, /Cannot reach the runtime/);
  assert.equal(shouldFallback(err), true);
});

test("legacy turn response: ticket_id from the runtime wins over the regex", () => withStrict(() => withMock({ latencyMs: 0 }, async (url) => {
  const r = await sendTurn({ runtimeUrl: url, callId: null, text: "Exam 15 minute mein hai, room nahi mil raha" });
  assert.equal(r.summary.ticketId, "EX-1042");
})));

// ------------------------------------------------------------------ first audio: streaming vs the whole-turn API
test("first reply text arrives earlier with streaming than with /api/test/turn (multi-sentence reply)", () => withStrict(() => withMock({ latencyMs: 100, sentenceDelayMs: 150 }, async (url) => {
  const c = await startCall({ runtimeUrl: url });
  const t0 = Date.now();
  let first = null;
  await streamTurn({ runtimeUrl: url, callId: c.callId, text: "fees kab tak jama karni hai", onEvent: (m) => { if (m.event === "speech" && first === null) first = Date.now() - t0; } });
  const t1 = Date.now();
  await sendTurn({ runtimeUrl: url, callId: null, text: "fees kab tak jama karni hai" });
  const whole = Date.now() - t1;
  assert.ok(first < whole - 100, `stream first sentence ${first} ms vs whole turn ${whole} ms`);
})));

// ------------------------------------------------------------------ sentence queue (TurnController with a progressive stream)
function fakeSynth() {
  const synth = {
    spoken: [], speaking: false, pending: false, cancelled: 0,
    speak(u) { if (this !== synth) throw new TypeError("Illegal invocation"); synth.spoken.push(u); },
    cancel() { if (this !== synth) throw new TypeError("Illegal invocation"); synth.cancelled += 1; synth.spoken.length = 0; },
  };
  return synth;
}
function build() {
  const synth = fakeSynth();
  const log = { first: 0, end: 0 };
  const c = new TurnController({ synth, makeUtterance: (text) => ({ text }), onFirstAudio: () => log.first++, onSpeechEnd: () => log.end++ });
  return { synth, c, log };
}

test("sentence queue: sentences are queued as they arrive, first audio is reported once, speech ends only after the stream is done", () => {
  const { synth, c, log } = build();
  const { turnId } = c.beginTurn();
  c.markRequestOpen();
  c.speak(turnId, ["One."]);
  synth.spoken[0].onstart();
  synth.spoken[0].onend(); // queue ran dry while the stream is still open
  assert.equal(log.end, 0);
  c.speak(turnId, ["Two."]);
  assert.equal(synth.spoken[1].onstart, undefined); // not the first sentence of the turn: no second first-audio report
  assert.equal(log.first, 1);
  synth.spoken[1].onend();
  assert.equal(log.end, 0);
  c.markRequestDone(); // stream finished and everything has been spoken
  assert.equal(log.end, 1);
  assert.equal(c.speaking, false);
  assert.deepEqual(synth.spoken.map((u) => u.text), ["One.", "Two."]);
});

test("sentence queue: speech finishing after the stream ends reports the end once", () => {
  const { synth, c, log } = build();
  const { turnId } = c.beginTurn();
  c.markRequestOpen();
  c.speak(turnId, ["A."]);
  c.speak(turnId, ["B."]);
  c.markRequestDone();
  assert.equal(log.end, 0);
  synth.spoken[0].onend();
  assert.equal(log.end, 0);
  synth.spoken[1].onend();
  assert.equal(log.end, 1);
});

test("greeting does not count as first audio of the next turn", () => {
  const { synth, c, log } = build();
  const g = c.beginTurn();
  c.speak(g.turnId, ["Namaste!"], { report: false });
  assert.equal(synth.spoken[0].onstart, undefined); // greeting is not timed
  assert.equal(log.first, 0);
  const t = c.beginTurn(); // a new turn silences the stale greeting
  assert.equal(synth.cancelled, 1);
  assert.equal(c.speaking, false);
  c.speak(t.turnId, ["Reply."]);
  synth.spoken[0].onstart();
  assert.equal(log.first, 1);
});

test("barge-in: aborts the stream, cancels speech, and a late sentence of the cancelled turn is never spoken", () => {
  const { synth, c } = build();
  const { turnId, signal } = c.beginTurn();
  c.markRequestOpen();
  c.speak(turnId, ["One."]);
  const r = c.bargeIn();
  assert.equal(r.cancelledSpeech, true);
  assert.equal(r.abortedRequest, true);
  assert.equal(signal.aborted, true);
  assert.equal(synth.spoken.length, 0);
  c.speak(turnId, ["Two, arriving late."]); // the stream delivered one more event before it was closed
  assert.equal(synth.spoken.length, 0);
  const next = c.beginTurn();
  c.speak(next.turnId, ["New answer."]);
  assert.deepEqual(synth.spoken.map((u) => u.text), ["New answer."]);
});
