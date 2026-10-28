$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDir
$env:PYTHONUTF8 = '1'
try {
    if (Get-NetTCPConnection -LocalPort 5031 -State Listen -ErrorAction SilentlyContinue) {
        Write-Host 'Reader is already running on 5031; no duplicate started.'
        exit 0
    }
    if (-not (Get-NetTCPConnection -LocalPort 30001 -State Listen -ErrorAction SilentlyContinue)) { throw 'Run Open-WeChat.cmd and finish login first.' }
    & (Join-Path $projectDir '.venv/Scripts/python.exe') -u scripts/wechat_reader.py --config config/wechatagent.local.toml
    exit $LASTEXITCODE
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
