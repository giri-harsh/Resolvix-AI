"""SHIM_API_KEY generation (integration/keygen.ps1, used by run_all.ps1): strength, persistence, preservation, no printing, consistency.

The bearer key protects the one public route (/v1/chat/completions). These tests drive the real PowerShell functions in throw-away temp
files and a child Python process that uses the real runtime code; nothing touches integration/.env or any running service.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
KEYGEN = HERE / "keygen.ps1"
RUN_ALL = HERE / "run_all.ps1"
PS = shutil.which("powershell.exe") or shutil.which("powershell")
pytestmark = pytest.mark.skipif(PS is None, reason="needs Windows PowerShell")

URLSAFE = re.compile(r"^[A-Za-z0-9_-]+$")


def ps(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    e = {k: v for k, v in os.environ.items() if k != "SHIM_API_KEY"}
    e.update(env or {})
    full = f". '{KEYGEN}'; $ErrorActionPreference = 'Stop'; " + script
    return subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", full], capture_output=True, text=True, env=e, timeout=60)


def load_env_into_process(path: Path) -> str:
    """The same thing run_all.ps1 does with integration/.env, as a PowerShell snippet."""
    return (f"Get-Content '{path}' | ForEach-Object {{ $l = $_.Trim(); if ($l -and -not $l.StartsWith('#') -and $l.Contains('=')) "
            f"{{ $i = $l.IndexOf('='); $v = $l.Substring($i + 1).Trim().Trim('\"'); if ($v) {{ Set-Item -Path ('env:' + $l.Substring(0, $i).Trim()) -Value $v }} }} }}; ")


def key_lines(path: Path) -> list[str]:
    return [l for l in path.read_text(encoding="utf-8").splitlines() if l.startswith("SHIM_API_KEY=")]


def b64url_bytes(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ------------------------------------------------------------------------------------------------ generation
def test_generator_gives_at_least_32_random_bytes_in_a_url_safe_format():
    r = ps("1..25 | ForEach-Object { New-RandomKey } | ForEach-Object { Write-Output $_ }")
    keys = r.stdout.split()
    assert r.returncode == 0 and len(keys) == 25
    assert len(set(keys)) == 25                                              # never repeats
    for k in keys:
        assert URLSAFE.fullmatch(k) and "=" not in k and len(k) == 43       # 32 bytes -> 43 Base64URL characters, no padding
        assert len(b64url_bytes(k)) == 32
    assert len(set("".join(keys))) > 55                                      # uses the whole alphabet (not a 16-hex or digits-only key)


def test_longer_keys_and_the_32_byte_floor():
    r = ps("Write-Output (New-RandomKey 48)")
    assert len(b64url_bytes(r.stdout.strip())) == 48
    bad = ps("New-RandomKey 16")
    assert bad.returncode != 0 and "at least 32 bytes" in (bad.stderr + bad.stdout)


def test_the_helper_and_run_all_use_the_cryptographic_rng_never_get_random():
    src = KEYGEN.read_text(encoding="utf-8")
    assert "System.Security.Cryptography.RandomNumberGenerator" in src
    for f in (KEYGEN, RUN_ALL):
        code = re.sub(r"<#.*?#>", "", f.read_text(encoding="utf-8"), flags=re.S)
        code = "\n".join(l.split("#")[0] if "#" in l and "'" not in l and '"' not in l else l for l in code.splitlines())
        assert "Get-Random" not in code, f.name
    assert "New-Secret" not in RUN_ALL.read_text(encoding="utf-8")


def test_demo_password_helper_is_unbiased_alphanumeric_and_long_enough():
    r = ps("1..20 | ForEach-Object { New-RandomString 16 } | ForEach-Object { Write-Output $_ }")
    vals = r.stdout.split()
    assert len(vals) == 20 and len(set(vals)) == 20
    assert all(len(v) == 16 and re.fullmatch(r"[A-Za-z0-9]+", v) and not set(v) & set("0O1lI") for v in vals)


# ------------------------------------------------------------------------------------------------ generate + persist
def test_missing_key_is_generated_persisted_and_reused(tmp_path):
    envf = tmp_path / ".env"
    envf.write_text("SERVICE_KEY=abc\nGROQ_API_KEY=\n", encoding="utf-8")
    first = ps(f"$r = Initialize-ShimApiKey -EnvFile '{envf}'; Write-Output \"result=$r\"; Write-Output (\"len=\" + $env:SHIM_API_KEY.Length)")
    assert first.returncode == 0 and "result=generated" in first.stdout and "len=43" in first.stdout
    lines = key_lines(envf)
    assert len(lines) == 1
    key = lines[0].split("=", 1)[1]
    assert URLSAFE.fullmatch(key) and len(b64url_bytes(key)) == 32
    assert "SERVICE_KEY=abc" in envf.read_text(encoding="utf-8")            # the rest of the file is untouched
    assert key not in first.stdout and key not in first.stderr              # never printed
    before = envf.read_bytes()
    second = ps(load_env_into_process(envf) + f"$r = Initialize-ShimApiKey -EnvFile '{envf}'; Write-Output \"result=$r\"; Write-Output (\"same=\" + ($env:SHIM_API_KEY -eq '{key}'))")
    assert "result=supplied" in second.stdout and "same=True" in second.stdout     # the next run reuses it
    assert envf.read_bytes() == before                                              # and does not rewrite the file


def test_empty_placeholder_line_is_replaced_not_duplicated(tmp_path):
    envf = tmp_path / ".env"
    envf.write_text("A=1\nSHIM_API_KEY=\nB=2\n", encoding="utf-8")
    r = ps(f"[void](Initialize-ShimApiKey -EnvFile '{envf}')")
    assert r.returncode == 0
    assert len(key_lines(envf)) == 1 and len(key_lines(envf)[0].split("=", 1)[1]) == 43
    assert "A=1" in envf.read_text() and "B=2" in envf.read_text()


def test_no_env_file_at_all_creates_one(tmp_path):
    envf = tmp_path / "new.env"
    r = ps(f"[void](Initialize-ShimApiKey -EnvFile '{envf}')")
    assert r.returncode == 0 and len(key_lines(envf)) == 1


# ------------------------------------------------------------------------------------------------ explicit keys are preserved
SUPPLIED = "explicit-operator-supplied-key-0123456789-ABCDEFGHIJ"


def test_key_supplied_in_the_shell_environment_is_kept_and_not_written(tmp_path):
    envf = tmp_path / ".env"
    envf.write_text("SERVICE_KEY=abc\n", encoding="utf-8")
    before = envf.read_bytes()
    r = ps(f"$r = Initialize-ShimApiKey -EnvFile '{envf}'; Write-Output \"result=$r\"; Write-Output (\"kept=\" + ($env:SHIM_API_KEY -eq '{SUPPLIED}'))",
           env={"SHIM_API_KEY": SUPPLIED})
    assert "result=supplied" in r.stdout and "kept=True" in r.stdout
    assert envf.read_bytes() == before                                      # a key given in the shell is not copied to disk
    assert SUPPLIED not in r.stdout and SUPPLIED not in r.stderr


def test_key_supplied_in_dot_env_is_kept(tmp_path):
    envf = tmp_path / ".env"
    envf.write_text(f"SHIM_API_KEY={SUPPLIED}\n", encoding="utf-8")
    before = envf.read_bytes()
    r = ps(load_env_into_process(envf) + f"$r = Initialize-ShimApiKey -EnvFile '{envf}'; Write-Output \"result=$r\"; Write-Output (\"kept=\" + ($env:SHIM_API_KEY -eq '{SUPPLIED}'))")
    assert "result=supplied" in r.stdout and "kept=True" in r.stdout and envf.read_bytes() == before


def test_a_short_supplied_key_is_kept_with_a_warning_that_does_not_contain_it(tmp_path):
    envf = tmp_path / ".env"
    envf.write_text("", encoding="utf-8")
    short = "short-key-123"
    r = ps(f"$r = Initialize-ShimApiKey -EnvFile '{envf}' -WarningAction SilentlyContinue -WarningVariable w; Write-Output \"result=$r\"; "
           f"Write-Output (\"kept=\" + ($env:SHIM_API_KEY -eq '{short}')); Write-Output (\"warned=\" + ($w.Count -eq 1)); "
           f"Write-Output (\"warning_mentions_key=\" + (($w -join ' ').Contains('{short}')))", env={"SHIM_API_KEY": short})
    assert "result=supplied" in r.stdout and "kept=True" in r.stdout and "warned=True" in r.stdout and "warning_mentions_key=False" in r.stdout
    assert envf.read_text() == ""                                            # still nothing written


def test_placeholder_change_me_is_refused_for_a_public_url_and_only_warned_locally(tmp_path):
    envf = tmp_path / ".env"
    envf.write_text("", encoding="utf-8")
    pub = ps(f"[void](Initialize-ShimApiKey -EnvFile '{envf}' -PublicBaseUrl 'https://example.trycloudflare.com')", env={"SHIM_API_KEY": "change-me"})
    assert pub.returncode != 0 and "placeholder" in (pub.stderr + pub.stdout)
    loc = ps(f"$r = Initialize-ShimApiKey -EnvFile '{envf}' -PublicBaseUrl 'http://localhost:8000' -WarningAction SilentlyContinue -WarningVariable w; "
             f"Write-Output \"result=$r\"; Write-Output (\"warned=\" + ($w.Count -eq 1))", env={"SHIM_API_KEY": "change-me"})
    assert loc.returncode == 0 and "result=supplied" in loc.stdout and "warned=True" in loc.stdout


# ------------------------------------------------------------------------------------------------ consistency with the runtime and Agora payload
CHILD = textwrap.dedent('''
    import asyncio, json, os, sys
    sys.dont_write_bytecode = True
    from fastapi.testclient import TestClient
    import resolvix_runtime.agora_control as ac
    from resolvix_runtime.app import app
    from resolvix_runtime.config import RuntimeSettings

    settings = RuntimeSettings.from_env()
    sent = {}
    class FakeResp:
        status_code = 200
        text = "{}"
        def json(self): return {"agent_id": "a1"}
    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None): sent["json"] = json; return FakeResp()
    ac.httpx.AsyncClient = FakeClient
    ctl = ac.AgoraController(settings, ops_client=None)
    asyncio.run(ctl._start_agora_agent(channel="c", call_id="call-x", llm_url="https://t.example/v1/chat/completions?call_id=call-x", greeting="hi", user_uid=1))
    key = os.environ["SHIM_API_KEY"]
    llm = sent["json"]["properties"]["llm"]
    client = TestClient(app)
    body = {"stream": False, "messages": []}
    ok = client.post("/v1/chat/completions", json=body, headers={"Authorization": "Bearer " + key}).status_code
    bad = client.post("/v1/chat/completions", json=body, headers={"Authorization": "Bearer " + key + "x"}).status_code
    print(json.dumps({"settings_equals_env": settings.shim_api_key == key, "agora_payload_api_key_equals_env": llm["api_key"] == key,
                      "shim_accepts_the_key_it_gives_agora": ok == 400, "shim_rejects_another_key": bad == 401}))
''')


def test_one_generated_key_is_what_the_runtime_expects_and_what_agora_is_told(tmp_path):
    envf = tmp_path / ".env"
    envf.write_text("", encoding="utf-8")
    r = ps(f"[void](Initialize-ShimApiKey -EnvFile '{envf}')")
    assert r.returncode == 0
    key = key_lines(envf)[0].split("=", 1)[1]
    env = {**os.environ, "SHIM_API_KEY": key, "BRAIN_IMPL": "mock", "PUBLIC_BASE_URL": "https://t.example", "PYTHONUTF8": "1",
           "AGORA_APP_ID": "0" * 32, "AGORA_APP_CERTIFICATE": "1" * 32, "AGORA_CUSTOMER_ID": "2" * 32, "AGORA_CUSTOMER_SECRET": "3" * 32}
    c = subprocess.run([sys.executable, "-c", CHILD], capture_output=True, text=True, env=env, timeout=120, cwd=str(tmp_path))
    assert c.returncode == 0, c.stderr[-800:]
    result = json.loads(c.stdout.strip().splitlines()[-1])
    assert result == {"settings_equals_env": True, "agora_payload_api_key_equals_env": True,
                      "shim_accepts_the_key_it_gives_agora": True, "shim_rejects_another_key": True}
    assert key not in c.stdout and key not in c.stderr
