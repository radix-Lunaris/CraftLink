# MC-Link panel diagnostic v2.
#
# The v1 script tried to regex Chinese text out of the page, which PowerShell
# 5.1 mangles (the script file is read as ANSI). Saving the raw HTML as UTF-8
# files and inspecting them with proper tools avoids the encoding problem
# entirely.

param(
    [string]$Base = "https://mc.example.com",
    [string]$User = "char",
    [string]$Pass = "75UMjShQ6TwjhFZW",
    [string]$OutDir = "panel_dump"
)

$ErrorActionPreference = "Continue"

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }

$sess = New-Object Microsoft.PowerShell.Commands.WebRequestSession

function Save-Page {
    param($Content, $Name)
    $path = Join-Path $OutDir "$Name.html"
    [System.IO.File]::WriteAllText($path, $Content, [System.Text.Encoding]::UTF8)
    Write-Host "  saved $path ($($Content.Length) bytes)"
}

# ---- 1. login page ----
$login = Invoke-WebRequest -Uri "$Base/index.php?action=login" -WebSession $sess -TimeoutSec 25 -UseBasicParsing
Save-Page $login.Content "01-login"

$csrf = [regex]::Match($login.Content, 'name="csrf"\s+value="([^"]+)"').Groups[1].Value
$fieldNames = [regex]::Matches($login.Content, 'name="([^"]+)"') | ForEach-Object { $_.Groups[1].Value }

Write-Host "=== login form fields: $($fieldNames -join ', ') ==="
Write-Host "=== csrf: $csrf ==="

# ---- 2. POST login ----
$body = @{ csrf = $csrf; username = $User; password = $Pass }

try {
    $resp = Invoke-WebRequest -Uri "$Base/index.php?action=login" -Method POST `
        -Body $body -WebSession $sess -TimeoutSec 25 -UseBasicParsing
    Write-Host "=== login POST status=$($resp.StatusCode) ==="
    Save-Page $resp.Content "02-login-response"
} catch {
    $r = $_.Exception.Response
    Write-Host "=== login POST status=$([int]$r.StatusCode) location=$($r.Headers['Location']) ==="
}

Write-Host "=== cookies: ==="
foreach ($c in $sess.Cookies.GetCookies($Base)) {
    Write-Host "  $($c.Name) = $($c.Value)"
}

# ---- 3. pages ----
$index = 3

foreach ($page in @("dashboard", "rooms", "users", "events")) {
    try {
        $p = Invoke-WebRequest -Uri "$Base/index.php?action=$page" -WebSession $sess -TimeoutSec 30 -UseBasicParsing
        Write-Host "=== $page status=$($p.StatusCode) bytes=$($p.Content.Length) ==="
        Save-Page $p.Content ("{0:D2}-{1}" -f $index, $page)
    } catch {
        $r = $_.Exception.Response
        Write-Host "=== $page status=$([int]$r.StatusCode) ==="
        try {
            $reader = New-Object System.IO.StreamReader($r.GetResponseStream())
            Save-Page $reader.ReadToEnd() ("{0:D2}-{1}-error" -f $index, $page)
        } catch {}
    }
    $index++
}

Write-Host "=== done ==="
