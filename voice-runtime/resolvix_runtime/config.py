from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR.parent / "contracts" / "config"


def load_default_runtime_lines() -> dict[str, dict[str, str]]:
    lines_file = Path(__file__).parent / "runtime_lines.yaml"
    if lines_file.exists():
        return yaml.safe_load(lines_file.read_text(encoding="utf-8")) or {}
    return {}


@dataclass
class RuntimeSettings:
    port: int = 8000
    public_base_url: str = "http://localhost:8000"
    shim_api_key: str = "change-me"
    service_key: str = "change-me"
    ops_api_url: str = "http://localhost:8100"
    brain_impl: str = "mock"  # "mock" or "real"
    telephony_mode: str = "mock"  # "mock" or "agora"
    telephony_transfer_success: bool = True
    agora_app_id: str = ""
    agora_app_certificate: str = ""
    agora_customer_id: str = ""
    agora_customer_secret: str = ""
    asr_vendor: str = "deepgram"
    asr_api_key: str = ""
    asr_language: str = "en"  # English-only for Phase 1.1 real voice test
    asr_model: str = "nova-3"
    asr_credential_mode: str = "byok"  # "byok" or "managed"
    tts_vendor: str = "elevenlabs"
    tts_api_key: str = ""
    tts_model: str = "eleven_flash_v2_5"
    tts_voice_id: str = "pNInz6obpgDQGcFmaJgB"
    tts_credential_mode: str = "byok"  # "byok" or "managed"
    tts_language_boost: str = ""       # MiniMax only, e.g. "Hindi"
    tts_group_id: str = ""             # MiniMax BYOK only (managed mode does not need it)
    voice_web_cors_origins: str = "http://localhost:5174,http://127.0.0.1:5174"   # comma separated; add the tunnel origin here
    voice_web_api_key: str = ""            # optional bearer token for /api/voice (set it whenever the API is reachable from the internet)
    voice_web_idle_timeout_s: float = 120.0
    voice_web_max_text_chars: int = 600
    voice_web_max_calls: int = 20
    voice_web_allow_caller_number: bool = True   # lets the page set a test caller number (repeat-caller memory, SMS demo)
    runtime_lines: dict[str, dict[str, str]] = field(default_factory=load_default_runtime_lines)

    def __post_init__(self) -> None:
        if not self.runtime_lines:
            self.runtime_lines = load_default_runtime_lines()

    @classmethod
    def from_env(cls) -> RuntimeSettings:
        lines_file = Path(__file__).parent / "runtime_lines.yaml"
        runtime_lines = {}
        if lines_file.exists():
            runtime_lines = yaml.safe_load(lines_file.read_text(encoding="utf-8")) or {}

        return cls(
            port=int(os.getenv("PORT", "8000")),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
            shim_api_key=os.getenv("SHIM_API_KEY", "change-me"),
            service_key=os.getenv("SERVICE_KEY", "change-me"),
            ops_api_url=os.getenv("OPS_API_URL", "http://localhost:8100").rstrip("/"),
            brain_impl=os.getenv("BRAIN_IMPL", "mock").lower(),
            telephony_mode=os.getenv("TELEPHONY_MODE", "mock").lower(),
            telephony_transfer_success=os.getenv("TELEPHONY_TRANSFER_SUCCESS", "true").lower() in ("true", "1", "yes"),
            agora_app_id=os.getenv("AGORA_APP_ID", ""),
            agora_app_certificate=os.getenv("AGORA_APP_CERTIFICATE", ""),
            agora_customer_id=os.getenv("AGORA_CUSTOMER_ID", ""),
            agora_customer_secret=os.getenv("AGORA_CUSTOMER_SECRET", ""),
            asr_vendor=os.getenv("ASR_VENDOR", "deepgram"),
            asr_api_key=os.getenv("ASR_API_KEY", ""),
            asr_language=os.getenv("ASR_LANGUAGE", "en"),
            asr_model=os.getenv("ASR_MODEL", "nova-3"),
            asr_credential_mode=os.getenv("ASR_CREDENTIAL_MODE", "byok").lower(),
            tts_vendor=os.getenv("TTS_VENDOR", "elevenlabs"),
            tts_api_key=os.getenv("TTS_API_KEY", ""),
            tts_model=os.getenv("TTS_MODEL", "eleven_flash_v2_5"),
            tts_voice_id=os.getenv("TTS_VOICE_ID", "pNInz6obpgDQGcFmaJgB"),
            tts_credential_mode=os.getenv("TTS_CREDENTIAL_MODE", "byok").lower(),
            tts_language_boost=os.getenv("TTS_LANGUAGE_BOOST", ""),
            tts_group_id=os.getenv("TTS_GROUP_ID", ""),
            voice_web_cors_origins=os.getenv("VOICE_WEB_CORS_ORIGINS", "http://localhost:5174,http://127.0.0.1:5174"),
            voice_web_api_key=os.getenv("VOICE_WEB_API_KEY", ""),
            voice_web_idle_timeout_s=float(os.getenv("VOICE_WEB_IDLE_TIMEOUT_S", "120")),
            voice_web_max_text_chars=int(os.getenv("VOICE_WEB_MAX_TEXT_CHARS", "600")),
            voice_web_max_calls=int(os.getenv("VOICE_WEB_MAX_CALLS", "20")),
            voice_web_allow_caller_number=os.getenv("VOICE_WEB_ALLOW_CALLER_NUMBER", "true").lower() in ("true", "1", "yes"),
            runtime_lines=runtime_lines,
        )

    def get_runtime_line(self, key: str, lang: str = "hinglish") -> str:
        """Fetch configured runtime line for key and lang, with fallback."""
        lines = self.runtime_lines.get(key, {})
        if lang in lines:
            return lines[lang]
        if "hinglish" in lines:
            return lines["hinglish"]
        if "en" in lines:
            return lines["en"]
        if "hi" in lines:
            return lines["hi"]
        return ""
