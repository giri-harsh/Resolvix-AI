// Client for the runtime streaming voice API (integration/VOICE_WEB_API.md, /api/voice/*): start call, SSE turn, cancel, end.
// Browser globals are only called through wrappers on globalThis (see control.js), never stored and called as methods.
import { ApiError } from "./api.js";

const trim = (url) => String(url || "").replace(/\/+$/, "");
const realFetch = (...args) => globalThis.fetch(...args);
const JSON_HEADERS = { "Content-Type": "application/json" };

/** Incremental Server-Sent-Events parser: feed() chunks of text, get back finished { event, data } messages. */
export class SSEParser {
  constructor() {
    this.buf = "";
  }

  feed(text) {
    this.buf = (this.buf + text).replace(/\r\n/g, "\n");
    const blocks = this.buf.split("\n\n");
    this.buf = blocks.pop(); // the last piece may be incomplete
    return blocks.map(parseBlock).filter(Boolean);
  }

  /** At end of stream: a final block without the blank line still counts. */
  flush() {
    const rest = this.buf;
    this.buf = "";
    const msg = rest.trim() ? parseBlock(rest) : null;
    return msg ? [msg] : [];
  }
}

function parseBlock(block) {
  let event = "message";
  const data = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const i = line.indexOf(":");
    const field = i < 0 ? line : line.slice(0, i);
    let value = i < 0 ? "" : line.slice(i + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  if (!data.length) return null;
  const raw = data.join("\n");
  try { return { event, data: JSON.parse(raw) }; } catch { return { event, data: raw }; }
}

async function envelopeError(resp, what) {
  let code = null;
  let message = `${what}: HTTP ${resp.status}`;
  try {
    const body = await resp.json();
    if (body?.error) { code = body.error.code ?? null; message = body.error.message ?? message; }
  } catch { /* not JSON (proxy page, FastAPI default) */ }
  const err = new ApiError(message, resp.status);
  err.code = code;
  return err;
}

async function post(url, body, { signal, fetchImpl, what }) {
  let resp;
  try {
    resp = await fetchImpl(url, { method: "POST", headers: JSON_HEADERS, body: body === undefined ? undefined : JSON.stringify(body), signal });
  } catch (err) {
    if (err?.name === "AbortError") throw err;
    throw new ApiError(`Cannot reach the runtime (${err?.message ?? err})`);
  }
  if (!resp.ok) throw await envelopeError(resp, what);
  return resp;
}

/** POST /api/voice/calls -> { callId, greeting, lang, idleTimeoutS, maxTextChars }. */
export async function startCall({ runtimeUrl, callerNumber, signal, fetchImpl = realFetch }) {
  const resp = await post(`${trim(runtimeUrl)}/api/voice/calls`, callerNumber ? { caller_number: callerNumber } : {}, { signal, fetchImpl, what: "start call" });
  const d = await resp.json();
  return { callId: d.call_id, greeting: String(d.greeting_text ?? ""), lang: d.lang ?? null, idleTimeoutS: d.idle_timeout_s ?? null, maxTextChars: d.max_text_chars ?? 600 };
}

/**
 * POST /api/voice/calls/{id}/turns as a stream. onEvent({ event, data }) is called for every SSE message as it arrives.
 * Resolves when the stream ends; rejects with AbortError when `signal` aborts (barge-in) or ApiError for HTTP failures.
 */
export async function streamTurn({ runtimeUrl, callId, text, signal, onEvent, fetchImpl = realFetch }) {
  const resp = await post(`${trim(runtimeUrl)}/api/voice/calls/${encodeURIComponent(callId)}/turns`, { text }, { signal, fetchImpl, what: "turn" });
  if (!resp.body?.getReader) throw new ApiError("This browser cannot read response streams", resp.status);
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SSEParser();
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      for (const msg of parser.feed(decoder.decode(value, { stream: true }))) onEvent(msg);
    }
    for (const msg of parser.flush()) onEvent(msg);
  } finally {
    try { reader.releaseLock(); } catch { /* already released */ }
  }
}

/** POST /api/voice/calls/{id}/end. 404 means the call is already ended (server idle timeout or the brain ended it): fine. */
export async function endVoiceCall({ runtimeUrl, callId, fetchImpl = realFetch }) {
  if (!callId) return { ok: true, skipped: true };
  try {
    const resp = await fetchImpl(`${trim(runtimeUrl)}/api/voice/calls/${encodeURIComponent(callId)}/end`, { method: "POST" });
    if (resp.status === 404) return { ok: true, alreadyEnded: true };
    if (!resp.ok) return { ok: false, status: resp.status };
    return { ok: true, ...(await resp.json().catch(() => ({}))) };
  } catch (err) {
    return { ok: false, error: String(err?.message ?? err) };
  }
}

/** GET /api/voice/config (limits); null when the endpoint does not exist. */
export async function voiceConfig({ runtimeUrl, fetchImpl = realFetch }) {
  try {
    const resp = await fetchImpl(`${trim(runtimeUrl)}/api/voice/config`);
    return resp.ok ? await resp.json() : null;
  } catch {
    return null;
  }
}

/** Should the page fall back to /api/test/* after this failure of the streaming API? (route missing, server error, runtime not reachable) */
export function shouldFallback(err) {
  if (!err || err.name === "AbortError") return false;
  const s = err.status ?? 0;
  return s === 0 || s === 404 || s === 405 || s === 501 || s >= 500;
}

/** Collects the events of one streamed turn into the summary the page renders. */
export class TurnAccumulator {
  constructor() {
    this.events = [];
    this.meta = {};
    this.done = null;
    this.error = null;
    this.actions = [];
    this.sentences = [];
  }

  add({ event, data }) {
    const d = data && typeof data === "object" ? data : {};
    this.events.push({ event, ...d });
    if (event === "meta") this.meta = d;
    else if (event === "speech") {
      const t = String(d.text ?? "").trim();
      if (t) this.sentences[Number.isInteger(d.index) ? d.index : this.sentences.length] = t;
    } else if (event === "action") this.actions.push(d);
    else if (event === "done") this.done = d;
    else if (event === "error") this.error = d;
  }

  get interruptable() {
    return this.meta.interruptable !== false;
  }

  /** The documented end of a barge-in: `error` with code conflict. Not a fault. */
  get cancelled() {
    return this.error?.code === "conflict";
  }

  summary() {
    const done = this.done ?? {};
    const ticketAction = this.actions.find((a) => a.kind === "create_ticket" && a.ticket_id);
    const kinds = this.actions.map((a) => a.kind).filter(Boolean);
    return {
      lang: this.meta.lang ?? done.lang ?? null,
      path: this.meta.path ?? done.path ?? null,
      tier: this.meta.tier ?? done.tier ?? null,
      interruptable: this.interruptable,
      priority: done.priority ?? ticketAction?.priority ?? null,
      ticketId: ticketAction?.ticket_id ?? done.ticket_ids?.[0] ?? null,
      actionTypes: kinds,
      transferred: kinds.includes("transfer"),
      ended: done.ended === true || kinds.includes("end_call"),
      latencyMs: done.latency_ms ?? null,
    };
  }
}
