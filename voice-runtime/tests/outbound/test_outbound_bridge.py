"""The Voicebot WebSocket bridge, driven by a fake Exotel client with synthetic PCM. No network, no real ASR/TTS/provider."""
from __future__ import annotations

import base64
import json

import pytest
from starlette.websockets import WebSocketDisconnect

from resolvix_runtime.outbound.audio import b64encode_pcm, silence, tone
from resolvix_runtime.sessions import get_session_registry

from helpers import GOOD_NUMBER, OTHER_NUMBER, cb_token, create, headers, start_event, ws_token

URL = "/v1/outbound-calls"
VOICE = b64encode_pcm(tone(200))             # voiced chunk (energy above the threshold)
QUIET = b64encode_pcm(silence(200))


def media(payload):
    return json.dumps({"event": "media", "stream_sid": "ST1", "media": {"payload": payload}})


def connect(client, provider, index=-1):
    return client.websocket_connect(f"/ws/exotel/{ws_token(provider, index)}")


def recv_until(ws, event, limit=300):
    """Collect messages until one with the given event arrives (returns all collected, last is the match)."""
    got = []
    for _ in range(limit):
        m = ws.receive_json()
        got.append(m)
        if m["event"] == event:
            return got
    raise AssertionError(f"no {event!r} event within {limit} messages: {[g['event'] for g in got][:10]}")


def drain(ws):
    got = []
    try:
        while True:
            got.append(ws.receive_json())
    except WebSocketDisconnect as exc:
        return got, exc.code
    except Exception as exc:                                       # a closed socket may surface as a runtime error in the test client
        return got, getattr(exc, "code", None)


def status(client, call):
    return client.get(f"{URL}/{call['call_id']}", headers=headers(key=None)).json()


def started(client, provider, ws, sid="MOCKSID0001"):
    ws.send_text(json.dumps({"event": "connected"}))
    ws.send_text(start_event(call_sid=sid))
    return recv_until(ws, "mark")                                  # greeting audio, then its mark


def speak_turn(ws, voiced=2):
    for _ in range(voiced):
        ws.send_text(media(VOICE))
    for _ in range(3):
        ws.send_text(media(QUIET))


# ------------------------------------------------------------------ connection rules
def test_unknown_expired_used_or_finished_tokens_are_never_accepted(client, provider, clock):
    call = create(client).json()
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/exotel/not-a-real-token"):
            pass
    assert exc.value.code == 1008
    clock.t += 181                                                  # token_ttl_s
    with pytest.raises(WebSocketDisconnect):
        with connect(client, provider):
            pass
    assert status(client, call)["status"] == "dialing"
    clock.t -= 181
    client.post(f"/v1/webhooks/exotel/{cb_token(provider)}", json={"CallSid": "MOCKSID0001", "Status": "busy"})
    with pytest.raises(WebSocketDisconnect):
        with connect(client, provider):
            pass


def test_start_connects_the_call_and_plays_the_greeting_in_valid_chunks(client, provider, svc, ops_mock):
    call = create(client).json()
    with connect(client, provider) as ws:
        frames = started(client, provider, ws)
        media_frames = [f for f in frames if f["event"] == "media"]
        assert media_frames and frames[-1]["event"] == "mark" and all(f["stream_sid"] == "ST1" for f in frames)
        for f in media_frames:
            raw = base64.b64decode(f["media"]["payload"])
            assert len(raw) % 320 == 0 and 3200 <= len(raw) <= 100000
        now = status(client, call)
        assert now["status"] == "connected" and now["connected_at"]
        ws.send_text(json.dumps({"event": "stop", "stop": {"call_sid": "MOCKSID0001", "reason": "callended"}}))
        drain(ws)
    final = status(client, call)
    assert final["status"] == "ended" and final["hangup_confirmed"] is True and final["duration_s"] is not None
    paths = [r.request.url.path for r in ops_mock.calls]
    assert "/v1/calls" in paths and f"/v1/calls/{call['call_id']}/end" in paths           # ops sees the call start and end (channel pstn)
    start_body = json.loads([r for r in ops_mock.calls if r.request.url.path == "/v1/calls"][0].request.content)
    assert start_body["channel"] == "pstn" and start_body["call_id"] == call["call_id"]


def test_a_token_is_single_use_once_the_stream_has_started(client, provider):
    create(client)
    with connect(client, provider) as ws:
        started(client, provider, ws)
        with pytest.raises(WebSocketDisconnect):
            with connect(client, provider):
                pass


def test_stop_reason_stopped_does_not_claim_the_phone_leg_is_closed(client, provider):
    call = create(client).json()
    with connect(client, provider) as ws:
        started(client, provider, ws)
        ws.send_text(json.dumps({"event": "stop", "stop": {"reason": "stopped"}}))
        drain(ws)
    final = status(client, call)
    assert final["status"] == "ended" and final["hangup_confirmed"] is False


def test_dropping_the_websocket_does_not_end_the_call(client, provider, svc):
    call = create(client).json()
    with connect(client, provider) as ws:
        started(client, provider, ws)
    assert status(client, call)["status"] == "connected"            # closing the socket is not a hang-up
    assert "ws_disconnected" in [e["event"] for e in svc.store.audit_rows(call["call_id"])]


# ------------------------------------------------------------------ protocol violations
def test_start_for_a_different_provider_sid_is_rejected(client, provider, svc):
    call = create(client).json()
    with connect(client, provider) as ws:
        ws.send_text(start_event(call_sid="SOMEONE-ELSES-CALL"))
        _, code = drain(ws)
    assert code == 1008 and status(client, call)["status"] == "dialing"
    assert "ws_rejected" in [e["event"] for e in svc.store.audit_rows(call["call_id"])]


def test_start_naming_another_calls_id_is_rejected(client, provider):
    call = create(client).json()
    with connect(client, provider) as ws:
        ws.send_text(start_event(custom={"param1": "call-out-someoneelse1"}))
        _, code = drain(ws)
    assert code == 1008 and status(client, call)["status"] == "dialing"


@pytest.mark.parametrize("frame,code", [("{not json", 1003), ("[1,2,3]", 1003), (json.dumps({"no_event": 1}), 1003),
                                        (json.dumps({"event": "media", "media": {"payload": "AAAA"}}), 1002), ("OVERSIZE", 1009)])
def test_malformed_or_out_of_order_messages_close_the_socket(client, provider, frame, code):
    create(client)
    frame = "x" * 300_000 if frame == "OVERSIZE" else frame
    with connect(client, provider) as ws:
        ws.send_text(frame)
        _, got = drain(ws)
    assert got == code


@pytest.mark.parametrize("payload", ["not base64!!", "QQ==", "TOOBIG"])
def test_bad_media_payloads_close_the_socket(client, provider, payload):
    payload = b64encode_pcm(b"\x00" * 100_002) if payload == "TOOBIG" else payload      # built here: a huge parametrize id breaks the env on Windows
    create(client)
    with connect(client, provider) as ws:
        started(client, provider, ws)
        ws.send_text(media(payload))
        _, got = drain(ws)
    assert got == 1007


# ------------------------------------------------------------------ conversation through the existing Brain
def test_a_spoken_question_goes_through_the_brain_and_comes_back_as_audio(client, provider, svc, tts_log):
    svc.scripts["*"] = ["what time does the hostel gate close"]
    call = create(client).json()
    with connect(client, provider) as ws:
        started(client, provider, ws)
        ws.send_text(json.dumps({"event": "mark", "stream_sid": "ST1", "mark": {"name": "m1"}}))
        speak_turn(ws)
        reply = recv_until(ws, "mark")
        assert any(f["event"] == "media" for f in reply)
        ws.send_text(json.dumps({"event": "stop", "stop": {"reason": "callended"}}))
        drain(ws)
    spoken = " ".join(tts_log[call["call_id"]].spoken)
    assert "Hostel gates close at 10 pm" in spoken
    session = get_session_registry().get(call["call_id"])
    assert session is None                                           # finalized at the end of the call, nothing left behind


def test_barge_in_clears_queued_audio_for_an_interruptable_answer(client, provider, svc):
    svc.scripts["*"] = ["what time does the hostel gate close"]
    create(client)
    with connect(client, provider) as ws:
        started(client, provider, ws)
        ws.send_text(json.dumps({"event": "mark", "stream_sid": "ST1", "mark": {"name": "m1"}}))
        speak_turn(ws)
        recv_until(ws, "mark")                                       # the answer is queued on the phone side; its mark has not come back
        ws.send_text(media(VOICE))                                   # the caller talks over it
        frames = recv_until(ws, "clear")
        assert frames[-1] == {"event": "clear", "stream_sid": "ST1"}
        ws.send_text(json.dumps({"event": "stop", "stop": {"reason": "callended"}}))
        drain(ws)


def test_an_emergency_turn_cannot_be_interrupted(client, provider, svc):
    svc.scripts["*"] = ["mere dost ko chakkar aaya aur wo behosh ho gaya"]
    call = create(client).json()
    with connect(client, provider) as ws:
        started(client, provider, ws)
        ws.send_text(json.dumps({"event": "mark", "stream_sid": "ST1", "mark": {"name": "m1"}}))
        speak_turn(ws)
        recv_until(ws, "mark")
        ws.send_text(media(VOICE))                                   # barge-in attempt during the emergency script
        ws.send_text(json.dumps({"event": "stop", "stop": {"reason": "callended"}}))
        rest, _ = drain(ws)
    assert not any(m["event"] == "clear" for m in rest)
    assert status(client, call)["status"] == "ended"


# ------------------------------------------------------------------ failures
def test_asr_failure_ends_the_call_as_failed_after_a_spoken_apology(client, provider, svc, tts_log):
    svc.asr_fail["*"] = 2
    call = create(client).json()
    with connect(client, provider) as ws:
        started(client, provider, ws)
        ws.send_text(media(VOICE))
        ws.send_text(media(VOICE))
        drain(ws)
    final = status(client, call)
    assert final["status"] == "failed" and final["failure_reason"] == "asr_failure"
    assert any("call back" in s for s in tts_log[call["call_id"]].spoken)


def test_tts_failure_ends_the_call_as_failed(client, provider, svc):
    call = create(client).json()
    svc.tts_fail.add(call["call_id"])
    with connect(client, provider) as ws:
        ws.send_text(start_event())
        drain(ws)
    assert status(client, call)["status"] == "failed" and status(client, call)["failure_reason"] == "tts_failure"


def test_brain_unavailable_fails_the_call_cleanly(client, provider, monkeypatch):
    import resolvix_runtime.outbound.bridge as bridge

    def boom(settings):
        raise RuntimeError("brain down")

    monkeypatch.setattr(bridge, "get_brain", boom)
    call = create(client).json()
    with connect(client, provider) as ws:
        ws.send_text(start_event())
        drain(ws)
    final = status(client, call)
    assert final["status"] == "failed" and final["failure_reason"] == "brain_unavailable"


def test_ops_being_down_does_not_drop_the_call(client, provider, ops_mock):
    ops_mock.post("/v1/calls").respond(503)
    call = create(client).json()
    with connect(client, provider) as ws:
        started(client, provider, ws)
        assert status(client, call)["status"] == "connected"
        ws.send_text(json.dumps({"event": "stop", "stop": {"reason": "callended"}}))
        drain(ws)


# ------------------------------------------------------------------ isolation between concurrent calls
def test_two_simultaneous_calls_never_share_context(client, provider, svc, tts_log, ops_mock):
    a = create(client, to=GOOD_NUMBER, key="idem-iso-00001").json()
    b = create(client, to=OTHER_NUMBER, key="idem-iso-00002").json()
    svc.scripts[a["call_id"]] = ["what time does the hostel gate close"]
    svc.scripts[b["call_id"]] = ["meri fee ki last date kya hai"]
    with connect(client, provider, 0) as wa, connect(client, provider, 1) as wb:
        wa.send_text(start_event(stream_sid="STA", call_sid="MOCKSID0001"))
        wb.send_text(start_event(stream_sid="STB", call_sid="MOCKSID0002"))
        recv_until(wa, "mark")
        recv_until(wb, "mark")
        for w in (wa, wb):
            w.send_text(json.dumps({"event": "mark", "mark": {"name": "m1"}}))
        for _ in range(2):
            wa.send_text(media(VOICE))
            wb.send_text(media(VOICE))
        for _ in range(3):
            wa.send_text(media(QUIET))
            wb.send_text(media(QUIET))
        ra, rb = recv_until(wa, "mark"), recv_until(wb, "mark")
        assert {f["stream_sid"] for f in ra} == {"STA"} and {f["stream_sid"] for f in rb} == {"STB"}
        sa = get_session_registry().get(a["call_id"])
        sb = get_session_registry().get(b["call_id"])
        assert sa is not sb and sa.caller_number == GOOD_NUMBER and sb.caller_number == OTHER_NUMBER
        assert sa.turn_count == 1 and sb.turn_count == 1 and sa.created_tickets == [] and sb.created_tickets == []
        for w in (wa, wb):
            w.send_text(json.dumps({"event": "stop", "stop": {"reason": "callended"}}))
            drain(w)
    spoken_a, spoken_b = " ".join(tts_log[a["call_id"]].spoken), " ".join(tts_log[b["call_id"]].spoken)
    assert "Hostel gates close" in spoken_a and "Hostel gates" not in spoken_b
    assert "30 November" in spoken_b and "30 November" not in spoken_a
    turn_calls = [r.request.url.path for r in ops_mock.calls if r.request.url.path.endswith("/turns")]
    assert any(a["call_id"] in p for p in turn_calls) and any(b["call_id"] in p for p in turn_calls)
    assert status(client, a)["status"] == "ended" and status(client, b)["status"] == "ended"
