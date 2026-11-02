// Page wiring: Web Speech API recognition -> typed-turn API -> speechSynthesis, with barge-in and an idle timeout.
import { endCall, health, sendTurn } from "./api.js";
import { endVoiceCall, shouldFallback, startCall, streamTurn, TurnAccumulator } from "./stream.js";
import { IdleTimer, TurnController } from "./control.js";
import { describeVoice, pickVoice, recognitionIssue, spellTicketIds, splitSentences, utteranceLang, voiceKind } from "./text.js";

const $ = (id) => document.getElementById(id);
const IDLE_MS = 60_000;
const synth = window.speechSynthesis;
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

const store = {
  get: (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

const params = new URLSearchParams(location.search);
$("runtimeUrl").value = params.get("runtime") || store.get("runtimeUrl", location.port === "8200" || location.pathname.startsWith("/voice") ? location.origin : "http://localhost:8000");
$("lang").value = store.get("lang", "hi-IN");
$("hinglishVoice").value = store.get("hinglishVoice", "hi");
$("apiMode").value = store.get("apiMode", "auto");
for (const id of ["runtimeUrl", "lang", "hinglishVoice", "apiMode"]) $(id).addEventListener("change", () => { store.set(id, $(id).value); if (id === "runtimeUrl") pollHealth(); });

let callId = null;
let mode = null; // "stream" (/api/voice/*) or "legacy" (/api/test/*) for the call in progress
let locked = false; // the bot is saying something it must finish (meta.interruptable false): no barge-in
let queuedText = null; // a transcript that arrived while locked; sent when the bot has finished
let opening = null; // promise of a call start in progress
let listening = false;
let recognition = null;
let pausedForSpeech = false;
let lastEnd = 0; // performance.now() when the last user utterance ended

const controller = new TurnController({
  synth,
  makeUtterance: (text) => new SpeechSynthesisUtterance(text),
  onFirstAudio: () => { $(mode === "stream" ? "tAudioStream" : "tAudioLegacy").textContent = ms(performance.now() - lastEnd); },
  onSpeechEnd: () => {
    locked = false;
    idle.resume();
    if (queuedText) { const t = queuedText; queuedText = null; handleFinal(t); return; }
    if (pausedForSpeech) { pausedForSpeech = false; startRecognition(); }
    if (pendingEnd) { pendingEnd = false; finishCall("the bot ended the call"); }
  },
});
let pendingEnd = false;
const idle = new IdleTimer(IDLE_MS, () => finishCall("60 s without speech"));

const ms = (n) => `${Math.round(n)} ms`;
const runtimeUrl = () => $("runtimeUrl").value.trim();
const notice = (text) => { $("notice").textContent = text || ""; };

/** Append a line to the Raw events panel (diagnostics: recognition errors, voice used). */
function diag(text) {
  const el = $("diag");
  el.textContent += `${new Date().toLocaleTimeString()}  ${text}
`;
}

/** Chrome only lets speechSynthesis speak after the page had a user gesture: unlock it from a click/submit handler. */
let primed = false;
function primeSpeech() {
  if (primed || !synth) return;
  primed = true;
  try {
    const u = new SpeechSynthesisUtterance(" ");
    u.volume = 0;
    synth.speak(u);
  } catch (err) { diag(`speech unlock failed: ${err}`); }
}

function addLog(kind, text, extra = "") {
  const li = document.createElement("li");
  li.className = kind;
  li.textContent = text;
  if (extra) tagLine(li, extra);
  $("log").appendChild(li);
  li.scrollIntoView({ block: "end" });
  return li;
}

function tagLine(li, extra) {
  const tag = document.createElement("span");
  tag.className = `tag ${extra}`;
  tag.textContent = extra;
  li.appendChild(tag);
}

async function pollHealth() {
  const state = await health({ runtimeUrl: runtimeUrl() });
  const el = $("status");
  el.textContent = state === "ok" ? "runtime connected" : state === "degraded" ? "runtime degraded" : "runtime unreachable";
  el.className = `pill ${state === "ok" ? "ok" : state === "degraded" ? "warn" : "bad"}`;
}
window.setInterval(pollHealth, 8000);
pollHealth();

// ---------------------------------------------------------------- recognition
function startRecognition() {
  if (!SR) return notice("This browser has no speech recognition: use Chrome. You can still type.");
  if (recognition) return;
  recognition = new SR();
  recognition.lang = $("lang").value;
  recognition.continuous = $("continuous").checked;
  recognition.interimResults = true;
  recognition.onresult = (event) => {
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const result = event.results[i];
      const text = result[0].transcript.trim();
      if (!text) continue;
      if (!result.isFinal) {
        $("interim").textContent = text;
        if (text.length >= 2 && $("headphones").checked) bargeIn("started talking");
        idle.reset();
      } else {
        $("interim").textContent = "";
        lastEnd = performance.now();
        handleFinal(text);
        if (!$("continuous").checked) stopListening();
      }
    }
  };
  recognition.onerror = (e) => {
    diag(`recognition error: ${e.error}`);
    const issue = recognitionIssue(e.error);
    if (issue.message) notice(issue.message);
    if (issue.stop) stopListening(); // no-speech / aborted: onend restarts silently while listening
  };
  recognition.onend = () => {
    recognition = null;
    if (listening && !pausedForSpeech) window.setTimeout(startRecognition, 250); // Chrome ends sessions on its own: keep going
  };
  try { recognition.start(); } catch { recognition = null; }
}

function stopRecognition() {
  const r = recognition;
  recognition = null;
  try { r?.abort(); } catch { /* already stopped */ }
}

function setListening(on) {
  listening = on;
  const b = $("mic");
  b.classList.toggle("on", on);
  b.setAttribute("aria-pressed", String(on));
  b.textContent = on ? "⏹ Stop listening" : "🎤 Start talking";
}

function stopListening() { setListening(false); stopRecognition(); $("interim").textContent = ""; }

$("mic").addEventListener("click", () => {
  primeSpeech();
  const wasSpeaking = controller.speaking || synth.speaking;
  if (wasSpeaking && !bargeIn("mic pressed")) return notice("The bot has to finish this message first."); // pressing the mic silences the bot at once
  if (listening && !wasSpeaking) return stopListening();
  notice("");
  setListening(true);
  startRecognition();
  if (!callId) greet();
});
$("lang").addEventListener("change", () => { if (listening) { stopRecognition(); startRecognition(); } });

/** Silence the bot and abort the stream in flight (closing the stream also cancels the turn on the server). false = not allowed. */
function bargeIn(why) {
  if (locked) { diag(`barge-in ignored (${why}): the runtime marked this reply as not interruptable`); return false; }
  const r = controller.bargeIn();
  if (r.cancelledSpeech || r.abortedRequest) addLog("sys", `interrupted (${why})`);
  if (pausedForSpeech) { pausedForSpeech = false; }
  idle.reset();
  return true;
}

// ---------------------------------------------------------------- call start (auto: streaming API first, then /api/test)
/** Open the call if there is none. Streaming API first; any failure of it falls back to /api/test (which opens its call on the first turn). */
function openCall() {
  if (callId || mode === "legacy") return Promise.resolve(null);
  if (opening) return opening;
  const pref = $("apiMode").value;
  opening = (async () => {
    if (pref !== "legacy") {
      try {
        const c = await startCall({ runtimeUrl: runtimeUrl() });
        mode = "stream";
        callId = c.callId;
        $("callId").textContent = callId;
        $("end").disabled = false;
        idle.start();
        idle.pause();
        diag(`streaming call ${callId} started (greeting ${c.lang ?? "?"}, server idle timeout ${c.idleTimeoutS ?? "?"} s)`);
        return c;
      } catch (err) {
        if (pref === "stream" || !shouldFallback(err)) throw err;
        diag(`streaming API unavailable (${err.message}); using /api/test/* instead`);
      }
    }
    mode = "legacy";
    return null;
  })().finally(() => { opening = null; });
  return opening;
}

/** Speak the greeting of a newly started streaming call. */
async function greet() {
  let c;
  try { c = await openCall(); } catch (err) { notice(err.message); return; }
  if (!c?.greeting) return;
  const { turnId } = controller.beginTurn();
  addLog("bot", c.greeting);
  speakSentence(turnId, c.greeting, c.lang, { report: false });
}

function speakSentence(turnId, sentence, replyLang, { report = true } = {}) {
  const kind = voiceKind(replyLang, sentence);
  const voice = pickVoice(synth.getVoices(), kind, { hinglishVoice: $("hinglishVoice").value });
  const lang = utteranceLang(kind, voice);
  if (!controller.speaking) {
    diag(`speaking with ${describeVoice(voice, lang)}; ${synth.getVoices().length} voices installed`);
    if (!$("headphones").checked && listening) { pausedForSpeech = true; stopRecognition(); } // no echo of the bot into the mic
  }
  controller.speak(turnId, [sentence], { voice, lang, report });
}

// ---------------------------------------------------------------- turns
async function handleFinal(text) {
  if (locked) { queuedText = text; diag("transcript queued: the bot is finishing a message that cannot be interrupted"); return; }
  idle.reset();
  addLog("user", text);
  if (!lastEnd) lastEnd = performance.now();
  const { turnId, signal } = controller.beginTurn();
  controller.markRequestOpen();
  idle.pause();
  $("tReply").textContent = "…";
  $("tAudioStream").textContent = $("tAudioLegacy").textContent = "…";
  try {
    await openCall();
    if (mode === "stream") await handleStreamTurn(turnId, signal, text);
    else await handleLegacyTurn(turnId, signal, text);
  } catch (err) {
    controller.markRequestDone();
    locked = false;
    if (err?.name === "AbortError") return;
    if (!controller.isCurrent(turnId)) return;
    if (mode === "stream" && err.status === 404 && err.code === "not_found") { // the server ended the call (idle timeout / brain ended it)
      callId = null; mode = null; idle.stop(); $("end").disabled = true; $("callId").textContent = "-";
      notice("The call was closed by the server. Say it again to start a new one.");
      return;
    }
    if (mode === "stream" && $("apiMode").value === "auto" && !err.code && (err.status === 404 || err.status === 405)) { // route missing: use the old API
      diag(`streaming turn failed (${err.message}); using /api/test/* instead`);
      callId = null; mode = "legacy";
      return handleFinalRetry(text);
    }
    notice(err.message);
    idle.resume();
    pollHealth();
  }
}

function handleFinalRetry(text) {
  const { turnId, signal } = controller.beginTurn();
  controller.markRequestOpen();
  return handleLegacyTurn(turnId, signal, text).catch((err) => { controller.markRequestDone(); if (err?.name !== "AbortError") notice(err.message); idle.resume(); });
}

// ---- streaming API: one speech event per sentence, spoken as it arrives
async function handleStreamTurn(turnId, signal, text) {
  const acc = new TurnAccumulator();
  let line = null;
  const onEvent = (msg) => {
    if (!controller.isCurrent(turnId)) return; // barge-in: nothing of a cancelled turn is shown or spoken
    acc.add(msg);
    if (msg.event === "meta") {
      locked = !acc.interruptable;
      $("pathTier").textContent = `${acc.meta.path ?? "-"} / ${acc.meta.tier ?? "-"}`;
      $("replyLang").textContent = acc.meta.lang ?? "-";
    } else if (msg.event === "speech") {
      const sentence = String(msg.data?.text ?? "").trim();
      if (!sentence) return;
      if (!line) { $("tReply").textContent = ms(performance.now() - lastEnd); line = addLog("bot", sentence); }
      else line.firstChild.nodeValue += ` ${sentence}`;
      speakSentence(turnId, sentence, acc.meta.lang);
    } else if (msg.event === "action" && msg.data?.kind === "transfer") addLog("sys", "transfer requested (simulated by the runtime)");
  };
  await streamTurn({ runtimeUrl: runtimeUrl(), callId, text, signal, onEvent });
  if (!controller.isCurrent(turnId)) { controller.markRequestDone(); return; }
  const sum = acc.summary();
  pendingEnd = sum.ended;
  controller.markRequestDone(); // may finish the call when the last sentence has already been spoken
  if (acc.error && !acc.cancelled) { locked = false; notice(acc.error.message || "The turn failed."); }
  else notice("");
  renderPanels({ callId, events: acc.events, summary: sum });
  if (line && sum.priority === "P1") tagLine(line, "P1");
  if (!line) addLog("bot", "(no speech)");
  if (!controller.speaking) { locked = false; if (sum.ended && pendingEnd) { pendingEnd = false; finishCall("the bot ended the call"); } else idle.resume(); }
}

// ---- fallback: /api/test/turn answers once, after the whole turn
async function handleLegacyTurn(turnId, signal, text) {
  const result = await sendTurn({ runtimeUrl: runtimeUrl(), callId, text, signal });
  controller.markRequestDone();
  if (!controller.isCurrent(turnId)) return; // the user talked over the request: drop this answer
  notice("");
  if (!callId) { callId = result.callId; $("end").disabled = false; idle.start(); idle.pause(); }
  callId = result.callId;
  addLog("bot", result.spokenText || "(no speech)", result.summary.priority === "P1" ? "P1" : "");
  renderPanels(result);
  $("tReply").textContent = ms(performance.now() - lastEnd);
  speakReply(turnId, result);
}

function renderPanels(result) {
  const s = result.summary;
  $("callId").textContent = result.callId;
  $("pathTier").textContent = `${s.path ?? "-"} / ${s.tier ?? "-"}`;
  $("replyLang").textContent = s.lang ?? "-";
  $("ticket").textContent = s.ticketId ?? "-";
  $("actions").textContent = s.actionTypes.length ? s.actionTypes.join(", ") : "-";
  $("raw").textContent = JSON.stringify(result.events, null, 2);
  if (s.transferred) addLog("sys", "transfer requested (simulated by the runtime)");
}

function speakReply(turnId, result) {
  const s = result.summary;
  const sentences = splitSentences(result.spokenText).map(spellTicketIds);
  if (!sentences.length) { idle.resume(); return; }
  const kind = voiceKind(s.lang, result.spokenText);
  const voice = pickVoice(synth.getVoices(), kind, { hinglishVoice: $("hinglishVoice").value });
  if (!$("headphones").checked && listening) { pausedForSpeech = true; stopRecognition(); } // no echo of the bot into the mic
  pendingEnd = s.ended;
  const lang = utteranceLang(kind, voice);
  diag(`speaking with ${describeVoice(voice, lang)}; ${synth.getVoices().length} voices installed`);
  controller.speak(turnId, sentences, { voice, lang });
}

// ---------------------------------------------------------------- end of call
async function finishCall(reason) {
  idle.stop();
  controller.bargeIn();
  const id = callId;
  const endMode = mode;
  callId = null;
  mode = null;
  locked = false;
  queuedText = null;
  $("end").disabled = true;
  if (!id) return;
  const r = endMode === "stream" ? await endVoiceCall({ runtimeUrl: runtimeUrl(), callId: id }) : await endCall({ runtimeUrl: runtimeUrl(), callId: id });
  addLog("sys", `call ended (${reason})${r.unsupported ? ": the runtime has no end endpoint, the call stays open in ops" : r.ok ? "" : ": end request failed"}`);
  if (r.unsupported) notice("The runtime cannot end typed calls yet (see REQUESTS_FOR_KAMAL.md).");
  $("callId").textContent = "-";
}

$("end").addEventListener("click", () => finishCall("End call pressed"));
$("typed").addEventListener("submit", (e) => {
  e.preventDefault();
  primeSpeech();
  const text = $("typedText").value.trim();
  if (!text) return;
  if (!bargeIn("typed message")) return notice("The bot has to finish this message first, then send again.");
  $("typedText").value = "";
  lastEnd = performance.now();
  handleFinal(text);
});

if (!SR) notice("This browser has no speech recognition: use Chrome. You can still type.");
if (synth) synth.onvoiceschanged = () => {}; // voices load lazily in Chrome; getVoices() is read at speak time
window.addEventListener("beforeunload", () => { if (callId) (mode === "stream" ? navigator.sendBeacon?.(`${runtimeUrl()}/api/voice/calls/${encodeURIComponent(callId)}/end`) : navigator.sendBeacon?.(`${runtimeUrl()}/api/test/end`, new Blob([JSON.stringify({ call_id: callId })], { type: "application/json" }))); });
