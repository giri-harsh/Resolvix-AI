// Mock of the voice-runtime typed-turn API, so the page can be tested with no runtime, brain or keys.
//   node mock_server/server.js [port]      (default 8200; also serves the page at http://localhost:8200/)
// Same request/response shapes as voice-runtime: GET /healthz, POST /api/test/turn, POST /api/test/end, and the streaming
// API of integration/VOICE_WEB_API.md: /api/voice/config, /calls, /calls/{id}/turns (SSE, one speech event per sentence), /cancel, /end.
import http from "node:http";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8" };

const isHindi = (t) => /[ऀ-ॿ]/.test(t) || /\b(kya|hai|kab|kitni|kaise|mujhe|aap|nahi|karo|baat|insaan|fees|kahan)\b/i.test(t);

/** Canned brain: keyword -> events in the runtime's shape. `state` carries the ticket counter and per-call data. */
export function mockReply(text, state) {
  const t = String(text || "").toLowerCase();
  const hi = isHindi(text);
  const lang = hi ? "hi" : "en";
  const done = (path, tier, extra = {}) => ({ type: "done", summary: { path, tier, lang, kb_ids: [], priority: "P4", ...extra } });
  const speech = (s) => ({ type: "speech", text: s });
  const ticket = (prefix) => `${prefix}-${state.next++}`;
  const meta = (path, tier, interruptable = true) => ({ type: "meta", path, tier, lang, interruptable });
  let events;

  if (/suicide|ambulance|behosh|bleeding|ragging|aag|emergency|accident/.test(t)) {
    const id = ticket("EM");
    events = [meta("safety", "escalate", false),
      speech(hi ? "Yeh emergency lag rahi hai. Main abhi aapko emergency desk se jod raha hoon." : "This sounds like an emergency. I am connecting you to the emergency desk now."),
      speech(hi ? `Aapka ticket number ${id} hai.` : `Your ticket number is ${id}.`),
      { type: "action", action: { type: "create_ticket", ticket: { type: "EMERGENCY", priority: "P1" } } },
      { type: "action", action: { type: "transfer", department: "emergency" } },
      done("safety", "escalate", { priority: "P1", kb_ids: [] })];
  } else if (/exam.*(minute|abhi|now)|room nahi|hall ticket nahi|paper start/.test(t)) {
    const id = ticket("EX");
    events = [meta("escalation", "escalate", false),
      speech(hi ? "Yeh urgent hai. Maine Exam Cell ke liye ticket bana diya hai aur abhi aapko unse jod raha hoon." : "This is urgent. I have created a ticket for the Exam Cell and I am connecting you now."),
      speech(hi ? `Ticket number ${id}.` : `Your ticket number is ${id}.`),
      { type: "action", action: { type: "create_ticket", ticket: { type: "URGENT", priority: "P1", department: "exam_cell" } } },
      { type: "action", action: { type: "transfer", department: "exam_cell" } },
      done("escalation", "escalate", { priority: "P1", rule_id: "R-EXAM-60" })];
  } else if (/insaan|human|operator|staff se baat|kisi se baat|real person/.test(t)) {
    const id = ticket("HD");
    events = [meta("escalation", "escalate"),
      speech(hi ? "Theek hai, main aapko staff se jod raha hoon." : "Sure, I am connecting you to a staff member."),
      speech(hi ? `Ticket number ${id}.` : `Your ticket number is ${id}.`),
      { type: "action", action: { type: "create_ticket", ticket: { type: "HUMAN_REQUEST", priority: "P2" } } },
      done("escalation", "escalate", { priority: "P2" })];
  } else if (/complain|shikayat|pareshan|kharab|mess|ragging/.test(t)) {
    const id = ticket("HS");
    events = [meta("slot_filling", "escalate"),
      speech(hi ? "Mujhe afsos hai. Maine aapki shikayat note kar li hai." : "I am sorry about that. I have noted your complaint."),
      speech(hi ? `Ticket number ${id}. Hostel office aapko call karega.` : `Your ticket number is ${id}. The hostel office will call you back.`),
      { type: "action", action: { type: "create_ticket", ticket: { type: "COMPLAINT", priority: "P2" } } },
      done("slot_filling", "escalate", { priority: "P2" })];
  } else if (/fee|fees|last date|deadline/.test(t)) {
    events = [meta("faq", "confident"),
      speech(hi ? "B.Tech second year ki semester fee ki last date 30 November hai. Ye Fee Notice 2026-27 mein diya hai." : "The last date for the B.Tech second-year semester fee is 30 November. This is in the Fee Notice 2026-27."),
      speech(hi ? "Kuch aur poochna hai?" : "Anything else?"),
      done("faq", "confident", { kb_ids: ["FEE-001"] })];
  } else if (/document|admission|eligibility/.test(t)) {
    events = [meta("faq", "confident"),
      speech(hi ? "Admission ke liye marksheet, photo ID aur category certificate chahiye. Ye Admission Guide mein hai." : "For admission you need your marksheets, a photo ID and the category certificate, as per the Admission Guide."),
      done("faq", "confident", { kb_ids: ["ADM-001"] })];
  } else if (/hello|hi\b|namaste|hey/.test(t)) {
    events = [meta("faq", "chitchat"), speech(hi ? "Namaste! Main college helpline ka AI sahayak hoon. Kaise madad karun?" : "Hello! I am the college helpline assistant. How can I help?"),
      done("faq", "chitchat")];
  } else {
    const id = ticket("TR");
    events = [meta("llm", "unknown"),
      speech(hi ? "Is baare mein mere paas abhi pakki jaankari nahi hai. Main ticket bana deta hoon." : "I do not have verified information on that yet. I will raise a ticket."),
      speech(hi ? `Ticket number ${id}.` : `Your ticket number is ${id}.`),
      { type: "action", action: { type: "create_ticket", ticket: { type: "KNOWLEDGE_GAP", priority: "P3" } } },
      { type: "action", action: { type: "log_gap", question: String(text) } },
      done("llm", "unknown", { priority: "P3" })];
  }
  return { events, spoken_text: events.filter((e) => e.type === "speech").map((e) => e.text).join(" ") };
}

const DIGITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"];
/** "EX-1042" -> "E X one zero four two": how the runtime writes ticket ids in streamed speech. */
export const spellForSpeech = (text) => String(text).replace(/\b([A-Z])([A-Z])-(\d{4})\b/g, (_m, a, b, d) => `${a} ${b} ${[...d].map((c) => DIGITS[Number(c)]).join(" ")}`);

/** Streaming-contract events for one turn: meta, speech {index,text}, action {kind,...}, done. Built from the canned reply. */
export function streamEvents(text, state, turnIndex) {
  const { events } = mockReply(text, state);
  const meta = events.find((e) => e.type === "meta");
  const done = events.find((e) => e.type === "done").summary;
  const speeches = events.filter((e) => e.type === "speech").map((e) => e.text);
  const ticketId = speeches.join(" ").match(/\b[A-Z]{2}-\d{4}\b/)?.[0] ?? null;
  const out = [{ event: "meta", data: { turn_index: turnIndex, path: meta.path, tier: meta.tier, lang: meta.lang, interruptable: meta.interruptable } }];
  let index = 0;
  for (const e of events) {
    if (e.type === "speech") out.push({ event: "speech", data: { index: index++, text: spellForSpeech(e.text) } });
    else if (e.type === "action") {
      const a = e.action;
      if (a.type === "create_ticket") out.push({ event: "action", data: { kind: "create_ticket", ticket_id: ticketId, type: a.ticket?.type, priority: a.ticket?.priority, department: a.ticket?.department ?? null } });
      else if (a.type === "transfer") out.push({ event: "action", data: { kind: "transfer", department: a.department, simulated: true } });
      else out.push({ event: "action", data: { kind: a.type } });
    }
  }
  out.push({ event: "done", data: { turn_index: turnIndex, path: done.path, tier: done.tier, lang: done.lang, rule_id: done.rule_id ?? null, priority: done.priority === "P4" ? null : done.priority, ticket_ids: ticketId ? [ticketId] : [], ended: false, latency_ms: 0 } });
  return out;
}

const sse = (e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`;

/**
 * latencyMs: time until the first sentence is ready; sentenceDelayMs: extra time per following sentence.
 * The streaming API sends every sentence as soon as it is ready; the /api/test/turn answer waits for the whole turn.
 * streaming:false makes /api/voice/* answer 404 (an older runtime), to test the page's fallback.
 */
export function createServer({ latencyMs = 250, sentenceDelayMs = Math.min(200, latencyMs), streaming = true, chunkSplit = false } = {}) {
  const calls = new Map(); // call_id -> { turns, ended }
  const state = { next: 1042 };
  const cors = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "GET,POST,OPTIONS" };
  const json = (res, status, body) => {
    res.writeHead(status, { "Content-Type": "application/json", ...cors });
    res.end(JSON.stringify(body));
  };
  const readBody = (req) => new Promise((resolve) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => { try { resolve(JSON.parse(raw || "{}")); } catch { resolve({}); } });
  });

  const envelope = (res, status, code, message, retryable = false) => json(res, status, { error: { code, message, retryable, details: {} } });
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function voiceApi(req, res, url) {
    const parts = url.pathname.replace(/^\/api\/voice\//, "").split("/").filter(Boolean); // config | calls | calls/{id}/{turns|cancel|end}
    if (parts[0] === "config" && req.method === "GET") return json(res, 200, { api_version: 1, contract_version: "1.0.0", streaming: true, idle_timeout_s: 120, max_text_chars: 600, caller_number_allowed: true });
    if (parts[0] !== "calls") return envelope(res, 404, "not_found", "Unknown route.");
    if (parts.length === 1 && req.method === "POST") {
      const id = `call-voice-${Math.random().toString(16).slice(2, 12)}`;
      calls.set(id, { turns: 0, ended: false, running: null, voice: true });
      return json(res, 201, { call_id: id, greeting_text: "Namaste! AKGEC helpline mein aapka swagat hai. Main aapki kya madad kar sakta hoon?", lang: "hinglish", kb_version: "mock", interruptable: false, idle_timeout_s: 120, max_text_chars: 600 });
    }
    const call = calls.get(parts[1]);
    const action = parts[2];
    if (!call || call.ended) return envelope(res, 404, "not_found", "Unknown or ended call.");
    if (action === "end" && req.method === "POST") {
      call.ended = true;
      return json(res, 200, { status: "ended", call_id: parts[1], outcome: call.turns ? "answered" : "abandoned", ai_resolved: call.turns > 0, turns_count: call.turns });
    }
    if (action === "cancel" && req.method === "POST") {
      const run = call.running;
      if (!run) return json(res, 200, { call_id: parts[1], cancelled: false, reason: "no_turn_in_progress" });
      if (!run.interruptable) return json(res, 200, { call_id: parts[1], cancelled: false, reason: "not_interruptable" });
      run.cancelled = true;
      return json(res, 200, { call_id: parts[1], cancelled: true });
    }
    if (action === "turns" && req.method === "POST") {
      const body = await readBody(req);
      const text = typeof body.text === "string" ? body.text.trim() : "";
      if (!text || text.length > 600) return envelope(res, 422, "validation_error", "text must be 1..600 characters.");
      if (call.running) {
        if (!call.running.interruptable) return envelope(res, 409, "conflict", "The previous turn cannot be interrupted yet.", true);
        call.running.cancelled = true;
      }
      call.turns += 1;
      const run = { interruptable: true, cancelled: false };
      call.running = run;
      server.turnLog.push({ callId: parts[1], text });
      const events = streamEvents(text, state, call.turns);
      run.interruptable = events[0].data.interruptable !== false;
      // the connection closing (the page aborted its fetch) cancels an interruptable turn
      res.on("close", () => { if (!res.writableEnded && run.interruptable) run.cancelled = true; });
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", ...cors });
      let spoken = 0;
      for (const e of events) {
        if (e.event === "speech") await sleep(spoken++ === 0 ? latencyMs : sentenceDelayMs);
        if (run.cancelled) {
          if (!res.writableEnded && !res.destroyed) { res.write(sse({ event: "error", data: { code: "conflict", message: "The turn was cancelled (barge-in).", retryable: false } })); res.end(); }
          if (call.running === run) call.running = null;
          server.cancelled += 1;
          return;
        }
        if (chunkSplit) { const raw = sse(e); res.write(raw.slice(0, 7)); await sleep(1); res.write(raw.slice(7)); } else res.write(sse(e));
      }
      if (call.running === run) call.running = null;
      return res.end();
    }
    return envelope(res, 404, "not_found", "Unknown route.");
  }

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://x");
    if (req.method === "OPTIONS") { res.writeHead(204, cors); return res.end(); }
    if (url.pathname === "/healthz") return json(res, 200, { status: "ok", mock: true, ops_api: "mock", brain: { status: "ok" } });
    if (url.pathname === "/api/test/turn" && req.method === "POST") {
      const body = await readBody(req);
      if (typeof body.text !== "string" || !body.text.trim()) return json(res, 422, { error: "text required" });
      const callId = body.call_id || `call-test-${Math.random().toString(16).slice(2, 10)}`;
      const call = calls.get(callId) ?? { turns: 0, ended: false };
      calls.set(callId, call);
      call.turns += 1;
      const reply = mockReply(body.text, state);
      const nSpeech = reply.events.filter((e) => e.type === "speech").length;
      await new Promise((r) => setTimeout(r, latencyMs + Math.max(0, nSpeech - 1) * sentenceDelayMs));
      return json(res, 200, { call_id: callId, ticket_id: reply.spoken_text.match(/\b[A-Z]{2}-\d{4}\b/)?.[0] ?? null, ...reply });
    }
    if (url.pathname === "/api/test/end" && req.method === "POST") {
      const body = await readBody(req);
      const call = calls.get(body.call_id);
      if (!call) return json(res, 404, { error: "unknown call" });
      call.ended = true;
      return json(res, 200, { ok: true, call_id: body.call_id, turns_count: call.turns, outcome: "answered" });
    }
    if (url.pathname.startsWith("/api/voice/")) {
      if (!streaming) return json(res, 404, { detail: "Not Found" });
      return voiceApi(req, res, url);
    }
    // static page
    const rel = url.pathname === "/" ? "/index.html" : url.pathname;
    if (/\.\./.test(rel) || !TYPES[path.extname(rel)] || rel.startsWith("/mock_server") || rel.startsWith("/test")) { res.writeHead(404, cors); return res.end("not found"); }
    try {
      const data = await readFile(path.join(ROOT, rel));
      res.writeHead(200, { "Content-Type": TYPES[path.extname(rel)], ...cors });
      res.end(data);
    } catch {
      res.writeHead(404, cors);
      res.end("not found");
    }
  });
  server.calls = calls;
  server.turnLog = []; // every streamed turn text the mock received
  server.cancelled = 0; // streamed turns that ended through a cancel / aborted connection
  return server;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const port = Number(process.argv[2] || process.env.PORT || 8200);
  createServer().listen(port, () => console.log(`mock runtime + voice page on http://localhost:${port}/  (Chrome)`));
}
