// Browser functions (setTimeout, fetch, ...) throw "Illegal invocation" when called with another object as `this`,
// so defaults are arrow wrappers that call them on globalThis; never store `globalThis.fn` and call it as `obj.fn()`.
// Idle timer and the barge-in / turn controller. Both take their browser dependencies as arguments so they are testable.

/** Calls onIdle after `ms` without activity. reset() on any activity; pause() while the bot is speaking or a turn is pending. */
export class IdleTimer {
  constructor(ms, onIdle, { setTimeoutFn = (fn, ms) => globalThis.setTimeout(fn, ms), clearTimeoutFn = (h) => globalThis.clearTimeout(h) } = {}) {
    this.ms = ms;
    this.onIdle = onIdle;
    this._set = setTimeoutFn;
    this._clear = clearTimeoutFn;
    this._handle = null;
    this._paused = false;
    this.running = false;
  }

  start() {
    this.running = true;
    this._arm();
  }

  reset() {
    if (this.running && !this._paused) this._arm();
  }

  pause() {
    this._paused = true;
    this._disarm();
  }

  resume() {
    this._paused = false;
    if (this.running) this._arm();
  }

  stop() {
    this.running = false;
    this._paused = false;
    this._disarm();
  }

  _arm() {
    this._disarm();
    this._handle = this._set(() => {
      this._handle = null;
      if (this.running && !this._paused) {
        this.running = false;
        this.onIdle();
      }
    }, this.ms);
  }

  _disarm() {
    if (this._handle !== null) {
      this._clear(this._handle);
      this._handle = null;
    }
  }
}

/**
 * Owns "what is happening right now": the sentences being spoken and the turn request in flight.
 * bargeIn() (user started talking, or pressed the mic) silences the bot at once and aborts the request in flight.
 * Every response is checked with isCurrent(turnId): an answer that comes back after a barge-in is dropped.
 */
export class TurnController {
  constructor({ synth, makeUtterance, onFirstAudio = () => {}, onSpeechEnd = () => {}, now = () => Date.now() }) {
    this.synth = synth;
    this.makeUtterance = makeUtterance;
    this.onFirstAudio = onFirstAudio;
    this.onSpeechEnd = onSpeechEnd;
    this.now = now;
    this.turnId = 0;
    this.abort = null;
    this.pendingSentences = 0;
    this.speaking = false;
  }

  /** Start a new turn: aborts any request still in flight and returns { turnId, signal }. */
  beginTurn() {
    this.abort?.abort();
    if (this.speaking) { this.synth.cancel(); this.pendingSentences = 0; this.speaking = false; } // the old turn's speech is stale now
    this.abort = new AbortController();
    this._firstQueued = false;
    this.turnId += 1;
    return { turnId: this.turnId, signal: this.abort.signal };
  }

  isCurrent(turnId) {
    return turnId === this.turnId;
  }

  /** Queue the sentences; the first one's start is reported through onFirstAudio (end of speech -> first audio timing). */
  speak(turnId, sentences, { voice = null, lang = "en-IN", rate = 1, report = true } = {}) {
    if (!this.isCurrent(turnId) || !sentences.length) return;
    this.speaking = true;
    this.pendingSentences += sentences.length;
    sentences.forEach((sentence) => {
      const u = this.makeUtterance(sentence);
      u.lang = lang;
      u.rate = rate;
      if (voice) u.voice = voice;
      if (report && !this._firstQueued) {
        this._firstQueued = true; // only the first sentence of the turn reports "first audio"
        u.onstart = () => this.isCurrent(turnId) && this.onFirstAudio(this.now());
      }
      const finish = () => {
        if (!this.isCurrent(turnId)) return;
        this.pendingSentences = Math.max(0, this.pendingSentences - 1);
        if (this.pendingSentences === 0 && !this._requestOpen) { // a stream may still deliver more sentences
          this.speaking = false;
          this.onSpeechEnd();
        }
      };
      u.onend = finish;
      u.onerror = finish;
      this.synth.speak(u);
    });
  }

  /** The user started speaking or pressed the mic. Returns what was actually interrupted. */
  bargeIn() {
    const hadSpeech = this.speaking || this.synth.speaking || this.synth.pending;
    const hadRequest = Boolean(this.abort) && !this.abort.signal.aborted && this._requestOpen;
    this.synth.cancel();
    this.abort?.abort();
    this.turnId += 1; // invalidates callbacks of the cancelled turn
    this.pendingSentences = 0;
    this.speaking = false;
    this._requestOpen = false;
    return { cancelledSpeech: Boolean(hadSpeech), abortedRequest: hadRequest };
  }

  markRequestOpen() {
    this._requestOpen = true;
  }

  markRequestDone() {
    this._requestOpen = false;
    if (this.speaking && this.pendingSentences === 0) { // the queue ran dry while the stream was still open
      this.speaking = false;
      this.onSpeechEnd();
    }
  }
}
