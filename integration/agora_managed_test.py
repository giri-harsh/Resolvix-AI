"""Join ONE Agora Conversational AI agent with Agora-MANAGED ASR/TTS and the Resolvix custom LLM endpoint, look at its state, and ALWAYS stop it.

Python integration/agora_managed_test.py --llm-base https://<tunnel-host> [--asr-lang hi] [--voice Hindi_female_1_v1] [--hold 10]

Secrets come from integration/.env (git-ignored) and are never printed (only masked, last 4). No microphone is involved: this proves the join,
the managed vendor parameters, the greeting synthesis and the agent state. A person (or the join page) in the channel is needed for the
ASR -> LLM -> TTS loop; the printed channel/uid/token let integration/agora_join_test open it.
Cost guard: the agent is stopped in a finally block and idle_timeout is short, so a crashed script cannot leave a billable agent running.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "voice-runtime"))
sys.path.insert(0, str(ROOT / "contracts" / "python"))
from resolvix_runtime.agora_token import generate_convoai_token, generate_rtc_token  # noqa: E402

API = "https://api.agora.io/api/conversational-ai-agent/v2/projects"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (ROOT / "integration" / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"')
    return env


def mask(text: str, env: dict[str, str]) -> str:
    for k in ("AGORA_CUSTOMER_SECRET", "AGORA_APP_CERTIFICATE", "SHIM_API_KEY", "AGORA_CUSTOMER_ID"):
        v = env.get(k)
        if v and len(v) > 6:
            text = text.replace(v, f"***{v[-4:]}")
    return re.sub(r"007[A-Za-z0-9+/=_-]{20,}", "007<token>", text)


def build_payload(env: dict, llm_url: str, channel: str, user_uid: int, agent_uid: int, args) -> dict:
    token = generate_convoai_token(app_id=env["AGORA_APP_ID"], app_certificate=env["AGORA_APP_CERTIFICATE"], channel_name=channel, uid=agent_uid)
    tts_params: dict = {"url": "wss://api.minimax.io/ws/v1/t2a_v2", "model": args.tts_model,
                        "voice_setting": {"voice_id": args.voice, "speed": 1.0}, "audio_setting": {"sample_rate": 44100}}
    if args.language_boost:
        tts_params["language_boost"] = args.language_boost
    return {
        "name": f"rx-test-{uuid.uuid4().hex[:8]}",
        "properties": {
            "channel": channel, "token": token, "agent_rtc_uid": str(agent_uid), "remote_rtc_uids": [str(user_uid)], "enable_string_uid": False,
            "idle_timeout": 60,
            "asr": {"credential_mode": "managed", "vendor": "deepgram",
                    "params": {"url": "wss://api.deepgram.com/v1/listen", "model": "nova-3", "language": args.asr_lang}},
            "llm": {"vendor": "custom", "style": "openai", "url": llm_url, "api_key": env["SHIM_API_KEY"],
                    "greeting_message": args.greeting, "failure_message": "Sorry, I am having trouble responding right now. Please try again.",
                    "params": {"model": "resolvix-brain"}},
            "tts": {"credential_mode": "managed", "vendor": "minimax", "params": tts_params},
        },
    }


def main() -> int:
    logging.getLogger("httpx").setLevel(logging.WARNING)      # httpx logs full request URLs (agent ids); keep them out of the console
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm-base", required=True, help="https origin of the secured proxy (tunnel), no path")
    ap.add_argument("--asr-lang", default="hi")
    ap.add_argument("--voice", default="Hindi_female_1_v1")
    ap.add_argument("--tts-model", default="speech-2.8-turbo")
    ap.add_argument("--language-boost", default="Hindi")
    ap.add_argument("--greeting", default="Namaste, AKGEC helpline mein aapka swagat hai. Main aapki kaise madad kar sakta hoon?")
    ap.add_argument("--hold", type=float, default=10.0, help="seconds to watch the agent state before stopping it")
    args = ap.parse_args()
    env = load_env()
    for need in ("AGORA_APP_ID", "AGORA_APP_CERTIFICATE", "AGORA_CUSTOMER_ID", "AGORA_CUSTOMER_SECRET", "SHIM_API_KEY"):
        if not env.get(need):
            sys.exit(f"{need} is missing in integration/.env")
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+/?", args.llm_base):
        sys.exit("--llm-base must be a bare https origin")
    channel, user_uid, agent_uid = f"rxtest-{uuid.uuid4().hex[:6]}", 2000 + int(time.time()) % 7000, 1000
    llm_url = f"{args.llm_base.rstrip('/')}/v1/chat/completions?call_id=agora-managed-test"
    payload = build_payload(env, llm_url, channel, user_uid, agent_uid, args)
    auth = {"Authorization": "Basic " + base64.b64encode(f"{env['AGORA_CUSTOMER_ID']}:{env['AGORA_CUSTOMER_SECRET']}".encode()).decode()}
    app = env["AGORA_APP_ID"]
    agent_id = None
    started = time.time()
    try:
        with httpx.Client(timeout=20) as http:
            r = http.post(f"{API}/{app}/join", headers=auth, json=payload)
            print(f"join -> HTTP {r.status_code} {mask(r.text[:400], env)}")
            if r.status_code not in (200, 201):
                return 2
            agent_id = r.json().get("agent_id")
            print(f"agent_id=***{str(agent_id)[-4:]} channel={channel} user_uid={user_uid} (join page token: python integration/agora_join_test/make_config.py)")
            deadline = time.time() + args.hold
            last = None
            while time.time() < deadline:
                s = http.get(f"{API}/{app}/agents/{agent_id}", headers=auth)
                state = (s.json().get("status") if s.headers.get("content-type", "").startswith("application/json") else None) or f"HTTP {s.status_code}"
                if state != last:
                    print(f"  t+{time.time() - started:4.1f}s agent status: {state}  {mask(s.text[:200], env) if s.status_code != 200 else ''}")
                    last = state
                if state in ("FAILED", "STOPPED"):
                    break
                time.sleep(1.0)
            return 0 if last == "RUNNING" else 3
    finally:
        if agent_id:
            try:
                with httpx.Client(timeout=15) as http:
                    leave = http.post(f"{API}/{app}/agents/{agent_id}/leave", headers=auth)
                print(f"leave -> HTTP {leave.status_code} (agent minutes used ~{(time.time() - started) / 60:.2f})")
            except Exception as exc:
                print(f"LEAVE FAILED ({type(exc).__name__}) - stop agent ***{str(agent_id)[-4:]} manually in the Agora console!", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
