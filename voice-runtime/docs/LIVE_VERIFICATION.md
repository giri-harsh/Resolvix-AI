# Resolvix Voice Runtime & Real Brain Integration Verification

**Date:** 2026-11-02  
**Owner:** Member A (Voice Runtime)  
**Status:** Verified with Live Groq LLM + Real Brain + Real Ops API  

---

## 1. Loud Failure on Real Brain Load (No Silent Mock Fallback)

### Verification
- **Code:** `voice-runtime/resolvix_runtime/brain_loader.py` (`get_brain()`).
  - When `BRAIN_IMPL=real`, if `resolvix_brain` cannot be imported, a `RuntimeError` is raised loudly with instructions to install `resolvix-brain`.
  - If `create_brain()` encounters an initialization or configuration error, a `RuntimeError` is raised with the root cause.
  - Zero silent fallback to `MockBrain`.
- **Test Suite:** `voice-runtime/tests/test_brain_loader.py`
  ```bash
  pytest voice-runtime/tests/test_brain_loader.py -v
  ```
  - `test_brain_loader_mock_default`: PASSED (MockBrain returned when configured)
  - `test_brain_loader_real_import_error_fails_loudly`: PASSED (raises `RuntimeError`)
  - `test_brain_loader_real_init_error_fails_loudly`: PASSED (raises `RuntimeError`)

---

## 2. Real Brain & Groq LLM Connection Proof

### Service Health
- **Runtime Port:** 8000
- **Ops API Port:** 8100 (Real `resolvix_ops` with SQLite seed)
- **Brain Mode:** `BRAIN_IMPL=real`, `LLM_MODE=live` (Groq API Key verified)
- **`GET http://localhost:8000/healthz`**:
  ```json
  {
    "status": "ok",
    "brain": {
      "status": "ok",
      "kb_version": "99152f80995e",
      "llm": "ok",
      "contract_version": "1.0.0",
      "embedder": "intfloat/multilingual-e5-small",
      "sessions": 0,
      "llm_mode": "live",
      "init_error": null
    },
    "ops_api": "ok",
    "agora": "mock",
    "contract_version": "1.0.0"
  }
  ```

### Live Startup Log
```
2026-11-02 18:07:12,150 [INFO] resolvix_runtime.brain_loader: Initializing real resolvix_brain...
2026-11-02 18:07:13,389 [INFO] resolvix_brain.kb: kb activated version=99152f80995e entries=12 tokens=2876
2026-11-02 18:07:13,565 [INFO] httpx: HTTP Request: POST https://api.groq.com/openai/v1/chat/completions "HTTP/1.1 200 OK"
2026-11-02 18:07:13,616 [INFO] resolvix_runtime: Resolvix voice-runtime started on port 8000
```

---

## 3. End-to-End Live Voice Conversation Traces

### Conversation Turn 1: English Query through `/api/test/turn`
- **Caller Input:** *"What is the hostel gate closing time?"*
- **Response Received:**
  ```json
  {
    "call_id": "live-voice-call-01",
    "events": [
      {
        "type": "meta",
        "path": "llm",
        "tier": "confident",
        "lang": "en",
        "interruptable": true
      },
      {
        "type": "speech",
        "text": "Hostel gates close at 10 pm on weekdays and 11 pm on weekends."
      },
      {
        "type": "speech",
        "text": "Anything else I can help with?"
      },
      {
        "type": "done",
        "summary": {
          "call_id": "live-voice-call-01",
          "turn_index": 1,
          "path": "llm",
          "tier": "confident",
          "lang": "en",
          "kb_ids": ["HST-003"],
          "assistant_text": "Hostel gates close at 10 pm on weekdays and 11 pm on weekends. Anything else I can help with?",
          "latency_ms": {
            "safety": 0,
            "faq": 6,
            "llm_ttft": 991,
            "llm_total": 1548,
            "brain_total": 1556
          },
          "cached_tokens": 1536
        }
      }
    ],
    "spoken_text": "Hostel gates close at 10 pm on weekdays and 11 pm on weekends. Anything else I can help with?"
  }
  ```

### Conversation Turn 2: Hindi/Hinglish Query via Agora SSE Streaming Shim
- **Endpoint:** `POST /v1/chat/completions?call_id=live-conv-01`
- **Caller Input:** *"End semester exam kab se shuru hain?"*
- **Streaming Chunks Received:**
  ```
  data: {"id":"resolvix-live-conv-01-1","object":"chat.completion.custom_metadata","created":1793623138,"model":"resolvix-brain","choices":[],"metadata":{"interruptable":true}}

  data: {"id":"resolvix-live-conv-01-1","object":"chat.completion.chunk","created":1793623138,"model":"resolvix-brain","choices":[{"index":0,"delta":{"role":"assistant","content":"End-semester theory exams 8 December se shuru hain. Ye Academic Calendar 2026-27 mein diya hai."},"finish_reason":null}]}

  data: {"id":"resolvix-live-conv-01-1","object":"chat.completion.chunk","created":1793623138,"model":"resolvix-brain","choices":[{"index":0,"delta":{"content":"Kuch aur poochna hai?"},"finish_reason":null}]}

  data: {"id":"resolvix-live-conv-01-1","object":"chat.completion.chunk","created":1793623138,"model":"resolvix-brain","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

  data: [DONE]
  ```

---

## 4. Ops API Telemetry & Ticket Synchronization
All turns and calls were persisted in `resolvix_ops` with HTTP 201:
```
INFO: 127.0.0.1 - "POST /v1/calls HTTP/1.1" 201 Created
INFO: 127.0.0.1 - "POST /v1/calls/live-voice-call-01/turns HTTP/1.1" 201 Created
INFO: 127.0.0.1 - "POST /v1/calls HTTP/1.1" 201 Created
INFO: 127.0.0.1 - "POST /v1/calls/live-conv-01/turns HTTP/1.1" 201 Created
```

---

## 5. Test Suite Verification
- `pytest contracts/tests`: 6 passed
- `pytest voice-runtime/tests`: 19 passed (including 3 brain loader tests)
- `pytest integration/e2e_test.py`: 9 passed
- **Total: 34 passed, 0 failed in 4.11s.**
