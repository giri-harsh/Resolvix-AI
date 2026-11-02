import test from "node:test";
import assert from "node:assert/strict";
import { extractTicketIds, hasDevanagari, pickVoice, spellTicketIds, splitSentences, utteranceLang, voiceKind } from "../src/text.js";

test("splitSentences: ordinary, Hindi danda, question and exclamation", () => {
  assert.deepEqual(splitSentences("Fee 30 November hai. Kuch aur poochna hai?"), ["Fee 30 November hai.", "Kuch aur poochna hai?"]);
  assert.deepEqual(splitSentences("नमस्ते। आप कैसे हैं? ठीक है!"), ["नमस्ते।", "आप कैसे हैं?", "ठीक है!"]);
});

test("splitSentences: decimals, domains and abbreviations stay whole", () => {
  assert.deepEqual(splitSentences("Fee is 10.5 lakh. See akgec.ac.in now."), ["Fee is 10.5 lakh.", "See akgec.ac.in now."]);
  assert.deepEqual(splitSentences("Caution money is Rs. 5000 and refundable."), ["Caution money is Rs. 5000 and refundable."]);
});

test("splitSentences: no final punctuation, empty and null input", () => {
  assert.deepEqual(splitSentences("Ek second"), ["Ek second"]);
  assert.deepEqual(splitSentences(""), []);
  assert.deepEqual(splitSentences(null), []);
  assert.deepEqual(splitSentences("  A.  B.  "), ["A.", "B."]);
});

test("spellTicketIds reads characters, not a word", () => {
  assert.equal(spellTicketIds("Your ticket number is EX-1042."), "Your ticket number is E X, 1 0 4 2.");
  assert.equal(spellTicketIds("HS-1044 aur HD-2001"), "H S, 1 0 4 4 aur H D, 2 0 0 1");
  assert.equal(spellTicketIds("Ticket number nahi bana, call 112"), "Ticket number nahi bana, call 112"); // untouched
  assert.equal(spellTicketIds("EXAM-1042 and ex-1042 and E-1042"), "EXAM-1042 and ex-1042 and E-1042"); // only two capital letters + 4 digits
});

test("extractTicketIds: order, duplicates, none", () => {
  assert.deepEqual(extractTicketIds("EX-1042 phir HS-1044, EX-1042"), ["EX-1042", "HS-1044"]);
  assert.deepEqual(extractTicketIds("no ticket"), []);
  assert.deepEqual(extractTicketIds(undefined), []);
});

test("voiceKind: Devanagari and Hinglish use the hi family, English the en family", () => {
  assert.equal(voiceKind("hi", "fee kab hai"), "hi");
  assert.equal(voiceKind("hinglish", "fee kab hai"), "hi");
  assert.equal(voiceKind("en", "The fee is due"), "en");
  assert.equal(voiceKind("en", "फीस 30 नवंबर"), "hi");
  assert.equal(voiceKind(null, "x"), "hi");
  assert.equal(hasDevanagari("abc"), false);
});

const VOICES = [
  { name: "US", lang: "en-US" }, { name: "IN-en", lang: "en-IN" }, { name: "IN-hi", lang: "hi-IN" }, { name: "GB", lang: "en_GB" },
];

test("pickVoice: hi prefers hi-IN, then en-IN; en prefers en-IN", () => {
  assert.equal(pickVoice(VOICES, "hi").name, "IN-hi");
  assert.equal(pickVoice(VOICES, "en").name, "IN-en");
  assert.equal(pickVoice(VOICES.filter((v) => v.name !== "IN-hi"), "hi").name, "IN-en");
  assert.equal(pickVoice(VOICES.filter((v) => !v.lang.endsWith("IN")), "en").name, "GB"); // underscore tags are normalised
});

test("pickVoice: the Hinglish override and an empty voice list", () => {
  assert.equal(pickVoice(VOICES, "hi", { hinglishVoice: "en" }).name, "IN-en");
  assert.equal(pickVoice([], "hi"), null);
  assert.equal(pickVoice(undefined, "en"), null);
  assert.equal(pickVoice([{ name: "X", lang: "fr-FR" }], "en").name, "X"); // anything beats silence
});

test("utteranceLang falls back to a sensible tag without a voice", () => {
  assert.equal(utteranceLang("hi", null), "hi-IN");
  assert.equal(utteranceLang("en", null), "en-IN");
  assert.equal(utteranceLang("hi", { lang: "en-IN" }), "en-IN");
});
