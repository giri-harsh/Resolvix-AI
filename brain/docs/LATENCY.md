# Latency and prompt-cache measurements

Every number below was produced by `python -m resolvix_brain bench --mock` (command at the bottom) on the development laptop
(Windows 11, CPU only, Python 3.12, real `multilingual-e5-small`, **mock LLM**). Nothing here involves the network.
Spike S2 (Groq caching) is **NOT RUN**: no `GROQ_API_KEY` was available.

## Measured (this machine, 12-entry sample KB, 2,876 prompt tokens)

| What | Result |
|---|---|
| Embedder + KB cold start (model already cached on disk) | 2.1 s (first ever run downloads ~470 MB: ~47 s) |
| Safety lexicon (`safety_hit`) | 8 us per call (budget: 5 ms) |
| Urgency rule evaluation | 5 us per call |
| Query embedding (e5-small, CPU) | p50 6.0 ms, p95 6.9 ms |
| Whole `handle_turn`, FAQ path (mock LLM) | p50 6 ms, p95 7 ms (n=5) |
| Whole `handle_turn`, other paths (mock LLM, includes embedding) | p50 7 ms, p95 8 ms (n=40) |
| 50 simultaneous one-turn calls | 120 ms wall, per call p50 62 ms / p95 106 ms |
| Unit tests: 50 interleaved multi-turn complaint calls | isolated, no cross-talk (test_fifty_concurrent_calls_are_isolated) |

These are brain-only costs. End-to-end time-to-first-audio also includes ASR endpointing, the LLM and TTS, none of which
were measured here.

## NOT measured (needs a Groq key)

| Question (PRD Spike S2) | Status |
|---|---|
| `cached_tokens > 0` on the second identical-prefix call, 60k and 90k token prefixes, `gpt-oss-120b` vs `-20b` | NOT RUN |
| Warm time-to-first-token (target <= ~700 ms) | NOT RUN |
| Whether `max_tokens: 300` leaves room for reasoning tokens on `gpt-oss` | NOT RUN |
| Default model / `KB_TOKEN_BUDGET` choice | **Kept at PRD defaults (120b / 90,000)**; not validated. The sample KB is 2.9k tokens, far below any budget |

How to run S2 (the code is written and unit-tested against a mocked SSE stream; the live call itself is untested):

```powershell
cd brain
$env:GROQ_API_KEY = "<your key>"      # never commit it
$env:LLM_MODE = "live"
python -m resolvix_brain bench        # prints cold/warm ttft, total, prompt_tokens, cached_tokens for both models
```

To test a 60k/90k-token prefix, compile a KB padded with *clearly synthetic* entries (not provided here) and point
`KB_SOURCE` at it; `KB_TOKEN_BUDGET` must be raised above the padded size. Record the results in this file and choose the
model/budget from them. Fallbacks if caching fails (PRD): smaller budget, `gpt-oss-20b`, more FAQ coverage.

## Where latency can still hurt (design notes, not measurements)

- One LLM call per non-FAQ turn, buffered and validated before speaking (PRD: ~100-200 ms cost accepted for the
  no-fabrication guarantee). Retry on the fallback model only happens if the first attempt failed within 1.5 s.
- Safety, human-request, urgent pre-check, chit-chat and FAQ-hit turns never call the LLM.
- The first request after a KB reload pays the cache-miss cost unless the warm-up call (`WARMUP_ON_RELOAD`) succeeded.

## Live Groq on the AKGEC draft KB (21 entries, 4,418 prompt tokens), measured 2026-11-02
Run with `python -m resolvix_brain eval --kb-set akgec-draft` and `LLM_MODE=live` (temporary approved copy of the draft KB for the run only; nothing
approved or published). One run per figure below on one machine (Windows, CPU embeddings); these are single measurements, not a benchmark.

| What | Result |
|---|---|
| Questions | 44 answerable + 24 must-abstain (`eval/akgec/`), 58 to 60 LLM calls (the rest were answered from the FAQ cache) |
| End-to-end answerable | 42/44 (95%) in the final run (41/44 in the first run). Misses: one run-to-run LLM variation ("kitne hostel hain": the model abstained) and one turn where both models returned no text (fallback ticket path). The FAQ cache wrongly answered the founding-year question with the director entry in the first run; the director entry was therefore removed from the FAQ cache. |
| Must-abstain | 24/24 (no unsupported answer, no invented figure) |
| Fabricated figures in answers | 0 of 42 checked (every number spoken is in the cited entry) |
| Model that actually answered | `openai/gpt-oss-20b` (56 of 57 successful calls); `gpt-oss-120b` returned no answer text 3 times and was then tried second (benching) |
| `gpt-oss-20b` latency, successful calls (n=56) | total p50 627 ms, p95 1,229 ms, p99 2,170 ms; time to first token p50 476 ms, p95 1,106 ms |
| Prompt cache | `cached_tokens > 0` on 55 of 56 calls, mean 93% of the prompt cached |
| Failures | 4 answer-less responses in 60 calls (3 from 120b, 1 from 20b) |

### The `gpt-oss-120b` empty-answer problem (diagnosed, mitigated, not solved by us)
- With `reasoning_effort=low` and our real prompts `gpt-oss-120b` often finishes with `finish_reason=stop` and **no answer text**: only a reasoning trace
  (about 20 reasoning tokens, ~100 more completion tokens that Groq does not return). Same result streaming and non-streaming.
- Not caused by `max_tokens` vs `max_completion_tokens` (identical results), by the token budget (800 tokens: still empty), or by our stream parser.
  On requests that had failed once: effort low 10/12 empty, medium 4/12 (300 tokens) and 10/12 (600 tokens), high 3/12 plus 7/12 truncated. `gpt-oss-20b`: 0/12 empty at low.
- The warm-up call (`max_tokens=8`) always ends with `finish_reason=length` and no text because gpt-oss spends the 8 tokens on reasoning: the warm-up now accepts that.
- Mitigation in `llm.py`: reasoning is never treated as an answer; an answer-less reply is `empty` (or `truncated` when the budget ran out) with token counts in the error; after 2
  empties in a row the primary is tried second for 5 minutes (`LLM_BENCH_AFTER_EMPTY`, `LLM_BENCH_SECONDS`); an answer-less reply no longer blocks the fallback retry on the 1.5 s gate.
- Consequence: with the default models nearly every live answer comes from `gpt-oss-20b`. Set `LLM_MODEL=openai/gpt-oss-20b` to skip the wasted first attempts, or report the issue to Groq.

### Still not measured
Spike S2 at 60k to 90k prompt tokens (the AKGEC KB is 4.4k tokens), concurrency under load, and the 50-call `bench` against the live API.
