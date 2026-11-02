"""Static wiring checks for integration/run_all.ps1 (it starts services, so it is parsed and read, never executed here)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
SRC = (HERE / "run_all.ps1").read_text(encoding="utf-8")
PS = shutil.which("powershell.exe") or shutil.which("powershell")


@pytest.mark.skipif(PS is None, reason="needs Windows PowerShell")
@pytest.mark.parametrize("name", ["run_all.ps1", "keygen.ps1", "start_voice_proxy.ps1", "stop_all.ps1", "check_health.ps1"])
def test_scripts_parse_without_errors(name):
    script = (f"$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile('{HERE / name}', [ref]$null, [ref]$e); "
              f"Write-Output ('errors=' + $e.Count)")
    r = subprocess.run([PS, "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=60)
    assert "errors=0" in r.stdout, r.stdout + r.stderr


def test_run_all_uses_the_key_helper_after_public_base_url_and_no_longer_requires_a_prebuilt_key():
    assert '. (Join-Path $PSScriptRoot "keygen.ps1")' in SRC
    call = "Initialize-ShimApiKey -EnvFile $EnvFile -PublicBaseUrl $env:PUBLIC_BASE_URL"
    assert call in SRC
    assert SRC.index("PUBLIC_BASE_URL must be a bare https origin") < SRC.index(call) < SRC.index("Start-Svc \"ops\"")     # decided before any service starts
    need = [l for l in SRC.splitlines() if l.startswith("foreach ($need in")][0]
    assert "SHIM_API_KEY" not in need                                       # it is generated when absent instead of aborting


def test_run_all_never_writes_or_prints_the_key():
    for line in SRC.splitlines():
        if ("$env:SHIM_API_KEY" in line or "${env:SHIM_API_KEY}" in line) and not line.lstrip().startswith("#"):
            assert "Write-Host" not in line and "Write-Output" not in line and "echo" not in line.lower(), line   # the value is never output


def test_the_only_place_the_key_reaches_a_child_process_is_the_environment():
    assert "$env:SHIM_API_KEY" not in SRC.replace("Initialize-ShimApiKey", "")     # no argument-list or log use of the value
