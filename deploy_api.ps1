# deploy_api.ps1 — Deploy backend to Heroku via Platform API (no git push needed)
# Run from the backend directory:
#   cd "...\backend"
#   powershell -ExecutionPolicy Bypass -File deploy_api.ps1

$ErrorActionPreference = "Continue"
$app = "atifixia-api"
$tarFile = "$env:TEMP\atifixia_deploy.tar.gz"

Write-Host "=== Heroku API Deploy: $app ===" -ForegroundColor Cyan

# ── Step 1: Create tar.gz with Python ────────────────────────────────────────
Write-Host "`n[1/4] Creating source archive..." -ForegroundColor Yellow

$pyScript = @"
import tarfile, os, sys
from pathlib import Path

exclude = {'.git', '__pycache__', '.env', 'venv', '.venv', '_deploy.tar.gz'}

def should_exclude(path):
    parts = Path(path).parts
    for p in parts:
        if p in exclude or p.endswith('.pyc'):
            return True
    return False

out = r'$tarFile'
with tarfile.open(out, 'w:gz') as tar:
    for root, dirs, files in os.walk('.'):
        dirs[:] = [d for d in dirs if not should_exclude(os.path.join(root, d))]
        for f in files:
            fpath = os.path.join(root, f)
            if not should_exclude(fpath):
                tar.add(fpath)

size = os.path.getsize(out)
print(f'Archive created: {out} ({size/1024:.1f} KB)')
"@

python -c $pyScript
if ($LASTEXITCODE -ne 0) {
    Write-Host "Python failed. Trying with python3..." -ForegroundColor Yellow
    python3 -c $pyScript
}
Write-Host "  Archive ready: $tarFile" -ForegroundColor Green

# ── Step 2: Get Heroku API token ─────────────────────────────────────────────
Write-Host "`n[2/4] Getting Heroku auth token..." -ForegroundColor Yellow
# Read from .netrc — avoids encoding issues with 'heroku auth:token' stderr warnings
$token = $null
$netrcFile = "$env:USERPROFILE\.netrc"
if (Test-Path $netrcFile) {
    $netrcRaw = Get-Content $netrcFile -Raw
    $m = [regex]::Match($netrcRaw, 'machine api\.heroku\.com[\s\S]*?password\s+(\S+)')
    if ($m.Success) { $token = $m.Groups[1].Value.Trim() }
}
# Fallback: try heroku auth:token but strip control chars aggressively
if (-not $token -or $token.Length -lt 10) {
    $rawToken = & cmd /c "heroku auth:token 2>nul"
    $token = ($rawToken -replace '[^\x20-\x7E]','').Trim()
}
if (-not $token -or $token.Length -lt 10) {
    Write-Host "ERROR: Could not get Heroku token. Run 'heroku login' first." -ForegroundColor Red
    exit 1
}
Write-Host "  Token obtained (length: $($token.Length))" -ForegroundColor Green

$headers = @{
    "Authorization" = "Bearer $token"
    "Accept"        = "application/vnd.heroku+json; version=3"
    "Content-Type"  = "application/json"
}

# ── Step 3: Get source blob upload URL ───────────────────────────────────────
Write-Host "`n[3/4] Requesting Heroku source blob URL..." -ForegroundColor Yellow
$sourceResp = Invoke-RestMethod `
    -Uri "https://api.heroku.com/apps/$app/sources" `
    -Method POST `
    -Headers $headers `
    -Body "{}"

$putUrl = $sourceResp.source_blob.put_url
$getUrl = $sourceResp.source_blob.get_url
Write-Host "  Source blob URLs acquired" -ForegroundColor Green

# Upload tar.gz to Heroku's S3
Write-Host "  Uploading archive to Heroku..." -ForegroundColor Yellow
$bytes = [System.IO.File]::ReadAllBytes($tarFile)
Invoke-RestMethod -Uri $putUrl -Method PUT -Body $bytes -ContentType ""
Write-Host "  Upload complete" -ForegroundColor Green

# ── Step 4: Trigger build ────────────────────────────────────────────────────
Write-Host "`n[4/4] Triggering Heroku build..." -ForegroundColor Yellow
$buildBody = @{
    source_blob = @{
        url     = $getUrl
        version = "0e4e03e-api-deploy"
    }
} | ConvertTo-Json

$buildResp = Invoke-RestMethod `
    -Uri "https://api.heroku.com/apps/$app/builds" `
    -Method POST `
    -Headers $headers `
    -Body $buildBody

Write-Host ""
Write-Host "=== BUILD STARTED ===" -ForegroundColor Green
Write-Host "  Build ID:  $($buildResp.id)"
Write-Host "  Status:    $($buildResp.status)"
Write-Host ""
Write-Host "Watch the build log:" -ForegroundColor Cyan
Write-Host "  heroku logs --tail --app $app"
Write-Host ""
Write-Host "Or stream build output URL:" -ForegroundColor Cyan
Write-Host "  $($buildResp.output_stream_url)"

# Optionally stream logs right away
Write-Host "`nStreaming logs (Ctrl+C to stop)..." -ForegroundColor Cyan
Start-Sleep -Seconds 5
heroku logs --tail --app $app
