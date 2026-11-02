// Pure text helpers for the voice page: sentence splitting, ticket-id spelling, language and voice choice.

const ABBREVIATIONS = /\b(Rs|Dr|Mr|Mrs|Ms|No|St|vs|etc|approx)$/i;

/** Split a reply into speakable sentences (. ! ? and the Devanagari danda). Decimals and common abbreviations stay whole. */
export function splitSentences(text) {
  const out = [];
  let current = "";
  const s = String(text ?? "");
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    current += ch;
    if (!".!?।".includes(ch)) continue;
    const next = s[i + 1];
    const atBoundary = next === undefined || /\s/.test(next);
    if (!atBoundary) continue; // "10.5", "akgec.ac.in"
    if (ch === "." && ABBREVIATIONS.test(current.slice(0, -1))) continue;
    out.push(current.trim());
    current = "";
  }
  if (current.trim()) out.push(current.trim());
  return out.filter(Boolean);
}

const TICKET_ID = /\b([A-Z]{2})-(\d{4})\b/g;

/** "EX-1042" -> "E X, 1 0 4 2" so the voice reads characters, not a word. */
export function spellTicketIds(text) {
  return String(text ?? "").replace(TICKET_ID, (_m, letters, digits) => `${letters.split("").join(" ")}, ${digits.split("").join(" ")}`);
}

/** Ticket ids mentioned in a reply, in order, without duplicates. */
export function extractTicketIds(text) {
  const ids = [];
  for (const m of String(text ?? "").matchAll(TICKET_ID)) if (!ids.includes(m[0])) ids.push(m[0]);
  return ids;
}

export const hasDevanagari = (text) => /[ऀ-ॿ]/.test(String(text ?? ""));

/** Which voice family a reply wants: "hi" for Hindi/Hinglish (or Devanagari text), "en" for English. */
export function voiceKind(metaLang, replyText) {
  if (hasDevanagari(replyText)) return "hi";
  const l = String(metaLang ?? "").toLowerCase();
  return l === "en" ? "en" : "hi";
}

/**
 * Pick a speechSynthesis voice. kind "hi": hi-IN first, then en-IN (reads romanised Hindi acceptably), then anything.
 * kind "en": en-IN, then other English. `hinglishVoice` ("hi" | "en") overrides the family for Latin-script Hindi replies.
 */
export function pickVoice(voices, kind, { hinglishVoice = "hi" } = {}) {
  const list = Array.from(voices ?? []);
  const by = (prefix) => list.find((v) => String(v.lang).replace("_", "-").toLowerCase().startsWith(prefix));
  const order = kind === "hi" ? (hinglishVoice === "en" ? ["en-in", "hi-in", "en"] : ["hi-in", "en-in", "en"]) : ["en-in", "en-gb", "en-us", "en"];
  for (const p of order) {
    const v = by(p);
    if (v) return v;
  }
  return list[0] ?? null;
}

/** BCP-47 tag to put on the utterance (so the browser picks a sensible engine even with no matching voice object). */
export function utteranceLang(kind, voice) {
  if (voice?.lang) return voice.lang;
  return kind === "hi" ? "hi-IN" : "en-IN";
}
