<#
Secret-generation helpers for run_all.ps1 (dot-sourced; also used by the tests). Nothing in this file prints a secret.
All randomness comes from System.Security.Cryptography.RandomNumberGenerator (never Get-Random).
#>

function New-RandomKey {
    <# At least 32 random bytes, encoded as Base64URL without padding (A-Z a-z 0-9 - _): safe in env vars, .env files and HTTP headers. #>
    param([int]$Bytes = 32)
    if ($Bytes -lt 32) { throw "New-RandomKey: at least 32 bytes are required (asked for $Bytes)" }
    $buf = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buf) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($buf).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function New-RandomString {
    <# Short human-typable value (the demo dashboard password). Unbiased selection (rejection sampling) from 57 look-alike-free characters. #>
    param([int]$Length = 16)
    if ($Length -lt 12) { throw "New-RandomString: use at least 12 characters" }
    $alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    $limit = 256 - (256 % $alphabet.Length)
    $sb = New-Object System.Text.StringBuilder
    $buf = New-Object byte[] 64
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        while ($sb.Length -lt $Length) {
            $rng.GetBytes($buf)
            foreach ($b in $buf) {
                if ($b -lt $limit -and $sb.Length -lt $Length) { [void]$sb.Append($alphabet[$b % $alphabet.Length]) }
            }
        }
    } finally { $rng.Dispose() }
    return $sb.ToString()
}

function Initialize-ShimApiKey {
    <#
    SHIM_API_KEY is the bearer Agora's cloud sends to /v1/chat/completions. The runtime reads it from its environment and puts the SAME value
    into the agent join payload (llm.api_key), so one value in $env:SHIM_API_KEY is all that has to be consistent.
    - If the caller already supplied one (shell environment, or integration/.env loaded into the environment) it is kept untouched. A short value
      only produces a warning; the placeholder "change-me" is refused when the stack is meant to be public.
    - Otherwise a new 32-byte key is generated, put into the environment AND appended to integration/.env so later runs reuse it.
    Returns "supplied" or "generated". The key is never written to the output streams.
    #>
    param([Parameter(Mandatory = $true)][string]$EnvFile, [string]$PublicBaseUrl = "")
    $existing = [Environment]::GetEnvironmentVariable("SHIM_API_KEY", "Process")
    if ($existing) {
        $isLocal = (-not $PublicBaseUrl) -or ($PublicBaseUrl -match '^http://(localhost|127\.0\.0\.1)')
        if ($existing -eq "change-me") {
            if (-not $isLocal) { throw "SHIM_API_KEY is still the placeholder 'change-me' but PUBLIC_BASE_URL is not local. Set a 32-byte random key." }
            Write-Warning "SHIM_API_KEY is the placeholder 'change-me': acceptable for local use only."
        } elseif ($existing.Length -lt 32) {
            Write-Warning ("SHIM_API_KEY you supplied has {0} characters (< 32). Keeping it as supplied; use a 32-byte random key before opening a tunnel." -f $existing.Length)
        }
        return "supplied"
    }
    $key = New-RandomKey 32
    $lines = @()
    if (Test-Path $EnvFile) { $lines = @([IO.File]::ReadAllLines($EnvFile) | Where-Object { $_ -notmatch '^\s*SHIM_API_KEY\s*=\s*$' }) }   # drop an empty placeholder line
    $lines += "SHIM_API_KEY=$key"
    [IO.File]::WriteAllLines($EnvFile, $lines, (New-Object System.Text.UTF8Encoding $false))
    [Environment]::SetEnvironmentVariable("SHIM_API_KEY", $key, "Process")
    Write-Host "SHIM_API_KEY generated (32 random bytes, Base64URL) and saved to integration/.env (value not printed)."
    return "generated"
}
