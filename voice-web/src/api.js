// Client for the runtime's typed-turn API (voice-runtime /api/test/turn). See ../API_ASSUMPTIONS.md.
import { extractTicketIds } from "./text.js";

const trim = (url) => String(url || "").replace(/\/+$/, "");

export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.status = status;
    this.code = null; // error-envelope code from /api/voice/*, when there is one
  }
}

/** Summarise the runtime's `events` array into what the page shows. Tolerates missing or extra event fields. */
export function summariseEvents(events, spokenText) {
  const evs = Array.isArray(events) ? events : [];
  const meta = evs.find((e) => e?.type === "meta") ?? {};
  const done = evs.find((e) => e?.type === "done")?.summary ?? {};
  const actions = evs.filter((e) => e?.type === "action").map((e) => e.action ?? {});
  const speech = evs.filter((e) => e?.type === "speech").map((e) => String(e.text ?? "")).filter(Boolean);
  const ticketIds = extractTicketIds(spokenText);
  return {
    lang: meta.lang ?? done.lang ?? null,
    path: meta.path ?? done.path ?? null,
    tier: meta.tier ?? done.tier ?? null,
    interruptable: meta.interruptable !== false,
    priority: done.priority ?? null,
    kbIds: done.kb_ids ?? [],
    actions,
    actionTypes: actions.map((a) => a.type).filter(Boolean),
    ticketId: ticketIds[0] ?? null,
    speechParts: speech,
    ended: actions.some((a) => a.type === "end_call"),
    transferred: actions.some((a) => a.type === "transfer"),
  };
}

/** The runtime now returns ticket_id itself (R-4): prefer it over the regex on the spoken text. */
function withTicket(summary, ticketId) {
  return typeof ticketId === "string" && ticketId ? { ...summary, ticketId } : summary;
}

/** POST /api/test/turn. The first turn of a call is sent with callId = null: the runtime starts the call and returns its id. */
export async function sendTurn({ runtimeUrl, callId, text, signal, fetchImpl = (...args) => globalThis.fetch(...args) }) {
  const t0 = Date.now();
  let resp;
  try {
    resp = await fetchImpl(`${trim(runtimeUrl)}/api/test/turn`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ call_id: callId || null, text }),
      signal,
    });
  } catch (err) {
    if (err?.name === "AbortError") throw err;
    throw new ApiError(`Cannot reach the runtime at ${trim(runtimeUrl)} (${err?.message ?? err})`);
  }
  if (!resp.ok) throw new ApiError(`Runtime answered HTTP ${resp.status}`, resp.status);
  let data;
  try {
    data = await resp.json();
  } catch {
    throw new ApiError("Runtime returned invalid JSON", resp.status);
  }
  const spoken = String(data.spoken_text ?? "");
  return { callId: data.call_id, spokenText: spoken, events: data.events ?? [], summary: withTicket(summariseEvents(data.events, spoken), data.ticket_id), ms: Date.now() - t0 };
}

/**
 * End the call. The stock runtime has no end endpoint for typed calls (REQUESTS_FOR_KAMAL R-2): we try POST /api/test/end
 * and report { ok:false, unsupported:true } when it does not exist, so the page can say so instead of failing.
 */
export async function endCall({ runtimeUrl, callId, fetchImpl = (...args) => globalThis.fetch(...args) }) {
  if (!callId) return { ok: true, skipped: true };
  try {
    const resp = await fetchImpl(`${trim(runtimeUrl)}/api/test/end`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ call_id: callId }),
    });
    if (resp.status === 404 || resp.status === 405) return { ok: false, unsupported: true, status: resp.status };
    if (!resp.ok) return { ok: false, status: resp.status };
    return { ok: true, ...(await resp.json().catch(() => ({}))) };
  } catch (err) {
    return { ok: false, error: String(err?.message ?? err) };
  }
}

/** GET /healthz -> "ok" | "degraded" | "down" */
export async function health({ runtimeUrl, fetchImpl = (...args) => globalThis.fetch(...args), timeoutMs = 2500 }) {
  const ctl = new AbortController();
  const timer = globalThis.setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const resp = await fetchImpl(`${trim(runtimeUrl)}/healthz`, { signal: ctl.signal });
    if (!resp.ok) return "down";
    const body = await resp.json().catch(() => ({}));
    return body.status === "ok" ? "ok" : "degraded";
  } catch {
    return "down";
  } finally {
    globalThis.clearTimeout(timer);
  }
}
