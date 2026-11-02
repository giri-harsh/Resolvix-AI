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

/** Short description of the chosen voice for the diagnostics log. */
export function describeVoice(voice, lang) {
  return voice ? `${voice.name} (${voice.lang})${voice.default ? " [default]" : ""}` : `browser default voice for ${lang}`;
}

/** Recognition error code -> { message, restart, stop } for the UI. restart: silently listen again (continuous mode). */
export function recognitionIssue(code) {
  switch (code) {
    case "network":
      return { message: "Speech service unreachable: use Google Chrome or Edge (not Brave/Opera), turn off VPN/ad-blocker, or try another network. You can still type below.", restart: false, stop: true };
    case "not-allowed":
    case "service-not-allowed":
      return { message: "Microphone blocked: click the lock/camera icon in the address bar, set Microphone to Allow (and check Windows Settings > Privacy > Microphone), reload, then press the mic again.", restart: false, stop: true };
    case "no-speech":
    case "aborted":
      return { message: "", restart: true, stop: false };
    case "audio-capture":
      return { message: "No microphone found: plug one in or pick it in the browser's site settings.", restart: false, stop: true };
    case "language-not-supported":
      return { message: "This browser does not support the selected recognition language: try the other one.", restart: false, stop: true };
    default:
      return { message: `Speech recognition error: ${code}`, restart: false, stop: false };
  }
}
