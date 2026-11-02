import test from "node:test";
import assert from "node:assert/strict";
import { ApiError, endCall, health, sendTurn, summariseEvents } from "../src/api.js";
import { createServer, mockReply } from "../mock_server/server.js";

async function withServer(fn, opts = { latencyMs: 0 }) {
  const server = createServer(opts);
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const url = `http://127.0.0.1:${server.address().port}`;
  try { await fn(url, server); } finally { await new Promise((r) => server.close(r)); }
}

test("first turn starts a call; the next turns reuse its id", async () => {
  await withServer(async (url) => {
    const a = await sendTurn({ runtimeUrl: url, callId: null, text: "fees kab tak jama karni hai" });
    assert.match(a.callId, /^call-test-/);
    assert.equal(a.summary.path, "faq");
    assert.equal(a.summary.tier, "confident");
    assert.equal(a.summary.lang, "hi");
    assert.deepEqual(a.summary.kbIds, ["FEE-001"]);
    assert.ok(a.spokenText.includes("30 November"));
    const b = await sendTurn({ runtimeUrl: url + "/", callId: a.callId, text: "hello" }); // trailing slash tolerated
    assert.equal(b.callId, a.callId);
  });
});

test("exam P1: ticket id extracted, actions listed, priority P1", async () => {
  await withServer(async (url) => {
    const r = await sendTurn({ runtimeUrl: url, callId: null, text: "Exam 15 minute mein hai, room nahi mil raha!" });
    assert.equal(r.summary.priority, "P1");
    assert.match(r.summary.ticketId, /^EX-\d{4}$/);
    assert.deepEqual(r.summary.actionTypes, ["create_ticket", "transfer"]);
    assert.equal(r.summary.transferred, true);
    assert.equal(r.summary.interruptable, false);
  });
});

test("emergency, human request, complaint and unknown question", async () => {
  await withServer(async (url) => {
    const em = await sendTurn({ runtimeUrl: url, callId: null, text: "ambulance chahiye" });
    assert.equal(em.summary.path, "safety");
    assert.match(em.summary.ticketId, /^EM-/);
    const hu = await sendTurn({ runtimeUrl: url, callId: null, text: "mujhe kisi insaan se baat karni hai" });
    assert.match(hu.summary.ticketId, /^HD-/);
    const co = await sendTurn({ runtimeUrl: url, callId: null, text: "hostel mess ki shikayat hai" });
    assert.match(co.summary.ticketId, /^HS-/);
    const un = await sendTurn({ runtimeUrl: url, callId: null, text: "What is the bus route to Noida sector 62?" });
    assert.equal(un.summary.tier, "unknown");
    assert.equal(un.summary.lang, "en");
    assert.ok(un.summary.actionTypes.includes("log_gap"));
  });
});

test("ticket ids are sequential and unique", async () => {
  await withServer(async (url) => {
    const ids = [];
    for (let i = 0; i < 3; i++) ids.push((await sendTurn({ runtimeUrl: url, callId: null, text: "random unknown question " + i })).summary.ticketId);
    assert.equal(new Set(ids).size, 3);
  });
});

test("an aborted request throws AbortError (barge-in cancels the in-flight turn)", async () => {
  await withServer(async (url) => {
    const ctl = new AbortController();
    const p = sendTurn({ runtimeUrl: url, callId: null, text: "fees", signal: ctl.signal });
    setTimeout(() => ctl.abort(), 20);
    await assert.rejects(p, (e) => e.name === "AbortError");
  }, { latencyMs: 400 });
});

test("unreachable runtime and HTTP errors become ApiError with a readable message", async () => {
  await assert.rejects(sendTurn({ runtimeUrl: "http://127.0.0.1:1", callId: null, text: "x" }), (e) => e instanceof ApiError && /Cannot reach the runtime/.test(e.message));
  await withServer(async (url) => {
    await assert.rejects(sendTurn({ runtimeUrl: url, callId: null, text: "   " }), (e) => e instanceof ApiError && e.status === 422);
  });
});

test("endCall: ok, unknown call, unsupported endpoint, no call", async () => {
  await withServer(async (url, server) => {
    const t = await sendTurn({ runtimeUrl: url, callId: null, text: "fees" });
    const ok = await endCall({ runtimeUrl: url, callId: t.callId });
    assert.equal(ok.ok, true);
    assert.equal(ok.turns_count, 1);
    assert.equal(server.calls.get(t.callId).ended, true);
    assert.equal((await endCall({ runtimeUrl: url, callId: "nope" })).ok, false);
  });
  assert.deepEqual(await endCall({ runtimeUrl: "http://x", callId: null }), { ok: true, skipped: true });
  const stock = async () => ({ status: 404, ok: false });
  assert.deepEqual(await endCall({ runtimeUrl: "http://x", callId: "c1", fetchImpl: stock }), { ok: false, unsupported: true, status: 404 });
});

test("health: ok, degraded, down", async () => {
  await withServer(async (url) => assert.equal(await health({ runtimeUrl: url }), "ok"));
  assert.equal(await health({ runtimeUrl: "http://127.0.0.1:1" }), "down");
  const degraded = async () => ({ ok: true, json: async () => ({ status: "degraded" }) });
  assert.equal(await health({ runtimeUrl: "http://x", fetchImpl: degraded }), "degraded");
});

test("summariseEvents tolerates missing and unknown fields", () => {
  const s = summariseEvents([{ type: "mystery" }, { type: "meta" }], "");
  assert.equal(s.path, null);
  assert.equal(s.interruptable, true);
  assert.deepEqual(s.actionTypes, []);
  assert.equal(summariseEvents(undefined, undefined).ticketId, null);
  const ended = summariseEvents([{ type: "action", action: { type: "end_call" } }], "bye");
  assert.equal(ended.ended, true);
});

test("mockReply speaks only text the page can read: every speech event is a non-empty string", () => {
  const state = { next: 1042 };
  for (const text of ["fees", "exam abhi hai", "ambulance", "insaan se baat", "shikayat", "kuch bhi", "hello"]) {
    const r = mockReply(text, state);
    assert.ok(r.events.some((e) => e.type === "done"));
    for (const e of r.events.filter((x) => x.type === "speech")) assert.ok(typeof e.text === "string" && e.text.length > 0);
  }
});
