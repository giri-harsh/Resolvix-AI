import test from "node:test";
import assert from "node:assert/strict";
import { IdleTimer, TurnController } from "../src/control.js";

// ---- fake clock for the idle timer
function fakeClock() {
  let now = 0, next = 1;
  const timers = new Map();
  return {
    setTimeoutFn: (fn, ms) => { const id = next++; timers.set(id, { at: now + ms, fn }); return id; },
    clearTimeoutFn: (id) => timers.delete(id),
    advance(ms) {
      now += ms;
      for (const [id, t] of [...timers]) if (t.at <= now) { timers.delete(id); t.fn(); }
    },
    pending: () => timers.size,
  };
}

test("IdleTimer fires once after the idle period", () => {
  const clock = fakeClock();
  let fired = 0;
  const t = new IdleTimer(60_000, () => fired++, clock);
  t.start();
  clock.advance(59_999);
  assert.equal(fired, 0);
  clock.advance(1);
  assert.equal(fired, 1);
  clock.advance(120_000);
  assert.equal(fired, 1); // does not repeat
  assert.equal(t.running, false);
});

test("IdleTimer.reset restarts the period; pause stops it; resume starts a fresh period", () => {
  const clock = fakeClock();
  let fired = 0;
  const t = new IdleTimer(1000, () => fired++, clock);
  t.start();
  clock.advance(900);
  t.reset();
  clock.advance(900);
  assert.equal(fired, 0);
  t.pause();
  clock.advance(10_000);
  assert.equal(fired, 0); // paused while the bot speaks
  t.reset(); // activity while paused changes nothing
  assert.equal(clock.pending(), 0);
  t.resume();
  clock.advance(999);
  assert.equal(fired, 0);
  clock.advance(1);
  assert.equal(fired, 1);
});

test("IdleTimer.stop cancels and ignores later resets", () => {
  const clock = fakeClock();
  let fired = 0;
  const t = new IdleTimer(1000, () => fired++, clock);
  t.start();
  t.stop();
  t.reset();
  clock.advance(5000);
  assert.equal(fired, 0);
  assert.equal(clock.pending(), 0);
});

// ---- fake speechSynthesis for the controller
function fakeSynth() {
  const queue = [];
  return {
    queue, cancelled: 0,
    speaking: false, pending: false,
    speak(u) { queue.push(u); this.speaking = true; },
    cancel() { this.cancelled++; queue.length = 0; this.speaking = false; this.pending = false; },
  };
}
const makeUtterance = (text) => ({ text });

test("TurnController.speak queues sentence by sentence and reports first audio once", () => {
  const synth = fakeSynth();
  const audio = [];
  let ended = 0;
  const c = new TurnController({ synth, makeUtterance, onFirstAudio: (t) => audio.push(t), onSpeechEnd: () => ended++, now: () => 123 });
  const { turnId } = c.beginTurn();
  c.speak(turnId, ["One.", "Two.", "Three."], { lang: "hi-IN", voice: { name: "v" } });
  assert.deepEqual(synth.queue.map((u) => u.text), ["One.", "Two.", "Three."]);
  assert.equal(synth.queue[0].lang, "hi-IN");
  assert.equal(synth.queue[1].voice.name, "v");
  synth.queue[0].onstart(); // only the first sentence reports first audio
  assert.deepEqual(audio, [123]);
  assert.equal(synth.queue[1].onstart, undefined);
  synth.queue[0].onend(); synth.queue[1].onend();
  assert.equal(ended, 0);
  synth.queue[2].onend();
  assert.equal(ended, 1);
  assert.equal(c.speaking, false);
});

test("barge-in silences the bot at once and invalidates the turn", () => {
  const synth = fakeSynth();
  let ended = 0;
  const c = new TurnController({ synth, makeUtterance, onSpeechEnd: () => ended++ });
  const { turnId } = c.beginTurn();
  c.speak(turnId, ["A.", "B."]);
  const first = synth.queue[0];
  const result = c.bargeIn();
  assert.equal(synth.cancelled, 1);
  assert.equal(result.cancelledSpeech, true);
  assert.equal(c.isCurrent(turnId), false);
  first.onend?.(); // a late event of the cancelled turn changes nothing
  assert.equal(ended, 0);
  assert.equal(c.speaking, false);
});

test("barge-in aborts the request in flight and reports it", () => {
  const synth = fakeSynth();
  const c = new TurnController({ synth, makeUtterance });
  const { signal } = c.beginTurn();
  c.markRequestOpen();
  const r = c.bargeIn();
  assert.equal(signal.aborted, true);
  assert.deepEqual(r, { cancelledSpeech: false, abortedRequest: true });
});

test("barge-in with nothing going on is harmless", () => {
  const c = new TurnController({ synth: fakeSynth(), makeUtterance });
  assert.deepEqual(c.bargeIn(), { cancelledSpeech: false, abortedRequest: false });
});

test("a new turn aborts the previous request and answers of old turns are not current", () => {
  const c = new TurnController({ synth: fakeSynth(), makeUtterance });
  const first = c.beginTurn();
  const second = c.beginTurn();
  assert.equal(first.signal.aborted, true);
  assert.equal(second.signal.aborted, false);
  assert.equal(c.isCurrent(first.turnId), false);
  assert.equal(c.isCurrent(second.turnId), true);
  const synth = fakeSynth();
  const d = new TurnController({ synth, makeUtterance });
  const t = d.beginTurn();
  d.beginTurn();
  d.speak(t.turnId, ["stale."]); // speaking for a turn that is no longer current is ignored
  assert.equal(synth.queue.length, 0);
});

test("speak with no sentences does not mark the controller as speaking", () => {
  const c = new TurnController({ synth: fakeSynth(), makeUtterance });
  const { turnId } = c.beginTurn();
  c.speak(turnId, []);
  assert.equal(c.speaking, false);
});
