param([switch]$Dual)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDir
$env:PYTHONUTF8 = '1'
$python = Join-Path $projectDir '.venv\Scripts\python.exe'
$channel = if ($Dual) { 'dual' } else { 'wechat' }
try {
    if (-not (Test-Path -LiteralPath $python)) { throw 'Run Setup.cmd first.' }
    if (Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue) {
        throw 'Port 8766 is already in use. Close the old bot before starting WARMAGENT.'
    }
    if (-not (Get-NetTCPConnection -LocalPort 30001 -State Listen -ErrorAction SilentlyContinue)) {
        throw 'Open and log in to the local matching client first: Open-WeChat.cmd'
    }
    if (-not (Get-NetTCPConnection -LocalPort 5031 -State Listen -ErrorAction SilentlyContinue)) {
        $logs = Join-Path $projectDir 'var\logs'
        New-Item -ItemType Directory -Path $logs -Force | Out-Null
        Start-Process -FilePath $python -ArgumentList @('-u','scripts/wechat_reader.py','--config','config/wechatagent.local.toml') `
            -WorkingDirectory $projectDir -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $logs 'reader.stdout.log') `
            -RedirectStandardError (Join-Path $logs 'reader.stderr.log') | Out-Null
        $deadline = (Get-Date).AddSeconds(30)
        while (-not (Get-NetTCPConnection -LocalPort 5031 -State Listen -ErrorAction SilentlyContinue)) {
            if ((Get-Date) -gt $deadline) { throw 'Reader did not start. Check var\logs\reader.stderr.log.' }
            Start-Sleep -Milliseconds 300
        }
    }
    & $python -m warmagent bind
    if ($LASTEXITCODE -ne 0) { throw 'WeChat identity binding failed; bot was not started.' }
    $pagesRunning = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object LocalPort -in 8767,8768)
    if ($pagesRunning.Count) {
        & (Join-Path $PSScriptRoot 'open_pages.ps1') -NoBrowser
        if ($LASTEXITCODE -ne 0) { throw 'Observation pages failed to start.' }
        & $python -u -m warmagent $channel --live
    } else {
        & $python -u -m warmagent $channel --live --ui
    }
    if ($LASTEXITCODE -ne 0) { throw 'WARMAGENT stopped with an error.' }
} catch {
    Write-Host $_ -ForegroundColor Red
    exit 1
}
