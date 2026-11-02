// Page wiring: Web Speech API recognition -> typed-turn API -> speechSynthesis, with barge-in and an idle timeout.
import { endCall, health, sendTurn } from "./api.js";
import { IdleTimer, TurnController } from "./control.js";
import { pickVoice, spellTicketIds, splitSentences, utteranceLang, voiceKind } from "./text.js";

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
for (const id of ["runtimeUrl", "lang", "hinglishVoice"]) $(id).addEventListener("change", () => { store.set(id, $(id).value); if (id === "runtimeUrl") pollHealth(); });

let callId = null;
let listening = false;
let recognition = null;
let pausedForSpeech = false;
let lastEnd = 0; // performance.now() when the last user utterance ended

const controller = new TurnController({
  synth,
  makeUtterance: (text) => new SpeechSynthesisUtterance(text),
  onFirstAudio: () => { $("tAudio").textContent = ms(performance.now() - lastEnd); },
  onSpeechEnd: () => {
    idle.resume();
    if (pausedForSpeech) { pausedForSpeech = false; startRecognition(); }
    if (pendingEnd) { pendingEnd = false; finishCall("the bot ended the call"); }
  },
});
let pendingEnd = false;
const idle = new IdleTimer(IDLE_MS, () => finishCall("60 s without speech"));

const ms = (n) => `${Math.round(n)} ms`;
const runtimeUrl = () => $("runtimeUrl").value.trim();
const notice = (text) => { $("notice").textContent = text || ""; };

function addLog(kind, text, extra = "") {
  const li = document.createElement("li");
  li.className = kind;
  li.textContent = text;
  if (extra) { const tag = document.createElement("span"); tag.className = `tag ${extra}`; tag.textContent = extra; li.appendChild(tag); }
  $("log").appendChild(li);
  li.scrollIntoView({ block: "end" });
}

async function pollHealth() {
  const state = await health({ runtimeUrl: runtimeUrl() });
  const el = $("status");
  el.textContent = state === "ok" ? "runtime connected" : state === "degraded" ? "runtime degraded" : "runtime unreachable";
  el.className = `pill ${state === "ok" ? "ok" : state === "degraded" ? "warn" : "bad"}`;
}
setInterval(pollHealth, 8000);
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
    if (e.error === "not-allowed" || e.error === "service-not-allowed") { notice("Microphone blocked: allow it in the address bar, then press the mic again."); stopListening(); }
    else if (e.error === "network") notice("Speech recognition needs an internet connection (Chrome sends audio to Google).");
    else if (e.error !== "no-speech" && e.error !== "aborted") notice(`Speech recognition error: ${e.error}`);
  };
  recognition.onend = () => {
    recognition = null;
    if (listening && !pausedForSpeech) setTimeout(startRecognition, 250); // Chrome ends sessions on its own: keep going
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
  const wasSpeaking = controller.speaking || synth.speaking;
  if (wasSpeaking) bargeIn("mic pressed"); // pressing the mic always silences the bot at once
  if (listening && !wasSpeaking) return stopListening();
  notice("");
  setListening(true);
  startRecognition();
});
$("lang").addEventListener("change", () => { if (listening) { stopRecognition(); startRecognition(); } });

function bargeIn(why) {
  const r = controller.bargeIn();
  if (r.cancelledSpeech || r.abortedRequest) addLog("sys", `interrupted (${why})`);
  if (pausedForSpeech) { pausedForSpeech = false; }
  idle.reset();
}

// ---------------------------------------------------------------- turns
async function handleFinal(text) {
  idle.reset();
  addLog("user", text);
  if (!lastEnd) lastEnd = performance.now();
  const { turnId, signal } = controller.beginTurn();
  controller.markRequestOpen();
  idle.pause();
  $("tReply").textContent = "…";
  $("tAudio").textContent = "…";
  let result;
  try {
    result = await sendTurn({ runtimeUrl: runtimeUrl(), callId, text, signal });
  } catch (err) {
    controller.markRequestDone();
    if (err?.name === "AbortError") return;
    notice(err.message);
    idle.resume();
    pollHealth();
    return;
  }
  controller.markRequestDone();
  if (!controller.isCurrent(turnId)) return; // the user talked over the request: drop this answer
  notice("");
  if (!callId) { callId = result.callId; $("end").disabled = false; idle.start(); idle.pause(); }
  callId = result.callId;
  render(result);
  $("tReply").textContent = ms(performance.now() - lastEnd);
  speakReply(turnId, result);
}

function render(result) {
  const s = result.summary;
  addLog("bot", result.spokenText || "(no speech)", s.priority === "P1" ? "P1" : "");
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
  controller.speak(turnId, sentences, { voice, lang: utteranceLang(kind, voice) });
}

// ---------------------------------------------------------------- end of call
async function finishCall(reason) {
  idle.stop();
  controller.bargeIn();
  const id = callId;
  callId = null;
  $("end").disabled = true;
  if (!id) return;
  const r = await endCall({ runtimeUrl: runtimeUrl(), callId: id });
  addLog("sys", `call ended (${reason})${r.unsupported ? ": the runtime has no end endpoint, the call stays open in ops" : r.ok ? "" : ": end request failed"}`);
  if (r.unsupported) notice("The runtime cannot end typed calls yet (see REQUESTS_FOR_KAMAL.md).");
  $("callId").textContent = "-";
}

$("end").addEventListener("click", () => finishCall("End call pressed"));
$("typed").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("typedText").value.trim();
  if (!text) return;
  $("typedText").value = "";
  bargeIn("typed message");
  lastEnd = performance.now();
  handleFinal(text);
});

if (!SR) notice("This browser has no speech recognition: use Chrome. You can still type.");
if (synth) synth.onvoiceschanged = () => {}; // voices load lazily in Chrome; getVoices() is read at speak time
window.addEventListener("beforeunload", () => { if (callId) navigator.sendBeacon?.(`${runtimeUrl()}/api/test/end`, new Blob([JSON.stringify({ call_id: callId })], { type: "application/json" })); });
