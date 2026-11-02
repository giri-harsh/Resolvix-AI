"""Groq streaming edge cases and the retry/bench policy (mocked SSE transport; nothing here touches the network).

Background (measured against live Groq): gpt-oss-120b intermittently returns only a reasoning trace and no answer text,
with finish_reason=stop, streaming or not, whatever the reasoning effort or max_tokens. See docs/LATENCY.md."""
from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from conftest import make_settings
from resolvix_brain.llm import GroqClient, LLMError, LLMResponse, LLMService, LLMUnavailable, _meta

COMMON = dict(model="openai/gpt-oss-120b", max_tokens=300, temperature=0.2, reasoning_effort="low", ttft_timeout=3.0, total_timeout=6.0)
SECRET_THOUGHT = "private-chain-of-thought-marker"


def sse(chunks, done=True) -> bytes:
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + ("data: [DONE]\n\n" if done else "")).encode()


def client(chunks) -> GroqClient:
    s = make_settings(groq_api_key="sk-test-secret", llm_mode="live")
    return GroqClient(s, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=sse(chunks))))


def delta(**kw):
    return {"choices": [{"delta": kw}]}


def finish(reason="stop"):
    return {"choices": [{"delta": {}, "finish_reason": reason}]}


USAGE = {"prompt_tokens": 4493, "completion_tokens": 102, "prompt_tokens_details": {"cached_tokens": 4352},
         "completion_tokens_details": {"reasoning_tokens": 15}}


# ------------------------------------------------------------------ the client
async def test_normal_streamed_content():
    c = client([delta(role="assistant", content=""), delta(content="Hello "), delta(content="there"), finish(),
                {"choices": [], "usage": USAGE}])
    r = await c.complete([{"role": "user", "content": "x"}], **COMMON)
    assert r.text == "Hello there" and r.finish_reason == "stop" and r.model == "openai/gpt-oss-120b"
    assert (r.prompt_tokens, r.completion_tokens, r.cached_tokens, r.reasoning_tokens) == (4493, 102, 4352, 15)


async def test_reasoning_chunks_then_content_returns_only_the_content():
    c = client([delta(role="assistant", content=""), delta(reasoning=SECRET_THOUGHT, channel="analysis"),
                delta(reasoning=" more", channel="analysis"), delta(content="The answer."), finish()])
    r = await c.complete([{"role": "user", "content": "x"}], **COMMON)
    assert r.text == "The answer." and SECRET_THOUGHT not in r.text
    assert r.ttft_ms >= 0 and r.finish_reason == "stop"


async def test_reasoning_only_response_is_empty_not_an_answer_and_never_leaks_the_reasoning():
    c = client([delta(role="assistant", content=""), delta(reasoning=SECRET_THOUGHT, channel="analysis"), finish(),
                {"choices": [], "usage": USAGE}])
    with pytest.raises(LLMError) as e:
        await c.complete([{"role": "user", "content": "x"}], **COMMON)
    assert e.value.kind == "empty"
    msg = str(e.value)
    assert "finish_reason=stop" in msg and "completion_tokens=102" in msg and "reasoning_tokens=15" in msg
    assert SECRET_THOUGHT not in msg                                            # metadata only


async def test_empty_stream_with_no_chunks_at_all():
    with pytest.raises(LLMError) as e:
        await client([]).complete([{"role": "user", "content": "x"}], **COMMON)
    assert e.value.kind == "empty"


async def test_truncated_before_any_answer_text_is_reported_as_truncated():
    c = client([delta(reasoning="thinking", channel="analysis"), finish("length")])
    with pytest.raises(LLMError) as e:
        await c.complete([{"role": "user", "content": "x"}], **COMMON)
    assert e.value.kind == "truncated" and "finish_reason=length" in str(e.value)


async def test_partial_answer_cut_by_the_token_limit_is_returned_with_finish_length():
    c = client([delta(content="Half an ans"), finish("length")])
    r = await c.complete([{"role": "user", "content": "x"}], **COMMON)
    assert r.text == "Half an ans" and r.finish_reason == "length"                 # the service then retries it (llm_truncated)


async def test_usage_only_final_chunk_and_groq_x_groq_usage_are_both_read():
    top = client([delta(content="ok"), finish(), {"choices": [], "usage": USAGE}])
    r = await top.complete([{"role": "user", "content": "x"}], **COMMON)
    assert r.prompt_tokens == 4493 and r.cached_tokens == 4352 and r.reasoning_tokens == 15
    nested = client([delta(content="ok"), {"choices": [{"delta": {}, "finish_reason": "stop"}], "x_groq": {"usage": USAGE}}])
    r = await nested.complete([{"role": "user", "content": "x"}], **COMMON)
    assert r.completion_tokens == 102 and r.cached_tokens == 4352


async def test_require_content_false_accepts_an_answerless_response_for_the_warmup():
    c = client([delta(reasoning="x", channel="analysis"), finish("length"), {"choices": [], "usage": USAGE}])
    r = await c.complete([{"role": "user", "content": "ping"}], **{**COMMON, "max_tokens": 8}, require_content=False)
    assert r.text == "" and r.finish_reason == "length" and r.cached_tokens == 4352


# ------------------------------------------------------------------ the service: fallback and benching
GOOD = _meta("confident", ["FEE-001"], "en") + "Pay by demand draft."
P, F = "openai/gpt-oss-120b", "openai/gpt-oss-20b"
MSG = [{"role": "user", "content": "x"}]
EMPTY = LLMError("empty", "no answer text")


class Scripted:
    """Replies per model: a list of LLMError | str items, consumed in order (the last one repeats)."""

    def __init__(self, script):
        self.script = script
        self.calls: list[str] = []

    async def aclose(self):
        return None

    async def complete(self, messages, *, model, max_tokens, temperature, reasoning_effort, ttft_timeout, total_timeout,
                       require_content=True):
        self.calls.append(model)
        items = self.script[model]
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, BaseException):
            raise item
        return LLMResponse(item, model, 5, 10, 100, 20, 0, "stop")


def service(cl, **over):
    return LLMService(cl, dataclasses.replace(make_settings(), llm_model=P, llm_fallback_model=F, **over))


async def test_primary_empty_then_fallback_succeeds():
    cl = Scripted({P: [EMPTY], F: [GOOD]})
    out = await service(cl).run(MSG)
    assert cl.calls == [P, F] and out.used_fallback_model and out.flags == ["llm_empty"]
    assert out.spoken == "Pay by demand draft." and out.meta.kb_ids == ["FEE-001"]


async def test_both_models_empty_raises_unavailable_with_flags():
    cl = Scripted({P: [EMPTY], F: [EMPTY]})
    with pytest.raises(LLMUnavailable) as e:
        await service(cl).run(MSG)
    assert e.value.flags == ["llm_empty", "llm_retry_failed"]


async def test_primary_is_tried_second_after_repeated_empties():
    cl = Scripted({P: [EMPTY, EMPTY, GOOD], F: [GOOD]})
    svc = service(cl, llm_bench_after_empty=2, llm_bench_seconds=300.0)
    await svc.run(MSG)                                   # empty #1 -> fallback answers
    assert cl.calls == [P, F]
    await svc.run(MSG)                                   # empty #2 -> benched
    assert cl.calls == [P, F, P, F]
    out = await svc.run(MSG)                             # benched: fallback first, one call, no wasted attempt
    assert cl.calls[4:] == [F] and "llm_primary_benched" in out.flags and out.used_fallback_model


async def test_bench_expires_and_a_primary_success_clears_it():
    cl = Scripted({P: [EMPTY, EMPTY, GOOD], F: [GOOD]})
    svc = service(cl, llm_bench_after_empty=2, llm_bench_seconds=300.0)
    await svc.run(MSG)
    await svc.run(MSG)
    assert svc._is_benched(P)
    svc._benched_until[P] = 0.0                          # the cool-down has passed
    out = await svc.run(MSG)
    assert cl.calls[-1] == P and not out.used_fallback_model and "llm_primary_benched" not in out.flags
    assert svc._empty_streak[P] == 0 and not svc._is_benched(P)


async def test_non_consecutive_empties_never_bench_the_primary():
    cl = Scripted({P: [EMPTY, GOOD, EMPTY, GOOD], F: [GOOD]})
    svc = service(cl, llm_bench_after_empty=2)
    for _ in range(4):
        await svc.run(MSG)
    assert not svc._is_benched(P)


async def test_benching_can_be_disabled():
    cl = Scripted({P: [EMPTY], F: [GOOD]})
    svc = service(cl, llm_bench_after_empty=0)
    for _ in range(4):
        await svc.run(MSG)
    assert not svc._is_benched(P) and cl.calls.count(P) == 4


async def test_truncated_primary_falls_back_without_being_counted_as_empty():
    cl = Scripted({P: [LLMError("truncated", "no answer text (finish_reason=length)")], F: [GOOD]})
    svc = service(cl, llm_bench_after_empty=1)
    out = await svc.run(MSG)
    assert out.flags == ["llm_truncated"] and not svc._is_benched(P)


async def test_warmup_does_not_fail_on_an_answerless_response():
    s = make_settings(groq_api_key="sk-test-secret", llm_mode="live")
    g = GroqClient(s, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=sse(
        [delta(reasoning="x", channel="analysis"), finish("length"), {"choices": [], "usage": USAGE}]))))
    info = await LLMService(g, s).warmup("system prefix")
    assert info["cached_tokens"] == 4352 and info["prompt_tokens"] == 4493


async def test_empty_primary_reply_that_took_over_the_short_gate_still_gets_the_fallback():
    """An answer-less reply is quick; the fallback must not be skipped just because it took 1.5 s+ (cold start)."""
    import asyncio

    class SlowEmpty(Scripted):
        async def complete(self, messages, **kw):
            if kw["model"] == P:
                self.calls.append(P)
                await asyncio.sleep(0.25)
                raise EMPTY
            return await super().complete(messages, **kw)

    cl = SlowEmpty({F: [GOOD]})
    svc = service(cl, llm_retry_if_elapsed_under_s=0.1, llm_total_timeout_s=6.0)
    out = await svc.run(MSG)
    assert cl.calls == [P, F] and out.used_fallback_model and "llm_retry_skipped" not in out.flags
