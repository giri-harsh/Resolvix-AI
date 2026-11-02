// Mock of the voice-runtime typed-turn API, so the page can be tested with no runtime, brain or keys.
//   node mock_server/server.js [port]      (default 8200; also serves the page at http://localhost:8200/)
// Same request/response shapes as voice-runtime: GET /healthz, POST /api/test/turn, plus the proposed POST /api/test/end.
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

export function createServer({ latencyMs = 250 } = {}) {
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
      await new Promise((r) => setTimeout(r, latencyMs));
      return json(res, 200, { call_id: callId, ...mockReply(body.text, state) });
    }
    if (url.pathname === "/api/test/end" && req.method === "POST") {
      const body = await readBody(req);
      const call = calls.get(body.call_id);
      if (!call) return json(res, 404, { error: "unknown call" });
      call.ended = true;
      return json(res, 200, { ok: true, call_id: body.call_id, turns_count: call.turns, outcome: "answered" });
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
  return server;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const port = Number(process.argv[2] || process.env.PORT || 8200);
  createServer().listen(port, () => console.log(`mock runtime + voice page on http://localhost:${port}/  (Chrome)`));
}
