$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
try {
    $metadataPath = Join-Path $projectDir 'var/qq/setup/runtime.local.json'
    $metadata = Get-Content -LiteralPath $metadataPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $runtimeDir = Join-Path $projectDir $metadata.runtime_dir
    $node = Join-Path $runtimeDir 'node.exe'
    $entry = Join-Path $runtimeDir 'index.js'
    if (-not (Test-Path -LiteralPath $node) -or -not (Test-Path -LiteralPath $entry)) {
        throw 'Local NapCat Node runtime is missing; see docs/QQ渠道接入_NapCat_OneBot11.md.'
    }
    if ([string]$metadata.bot_id -notmatch '^\d+$') { throw 'Robot QQ account is missing.' }
    $running = @(Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Where-Object { $_.ExecutablePath -eq $node })
    if ($running.Count) {
        Write-Host 'Local NapCat is already running. Start-Dual.cmd starts the bot.'
        exit 0
    }
    $logs = Join-Path $projectDir 'var/qq/logs'
    New-Item -ItemType Directory -Path $logs -Force | Out-Null
    $process = Start-Process -FilePath $node -ArgumentList @('index.js','-q',([string]$metadata.bot_id)) `
        -WorkingDirectory $runtimeDir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logs 'napcat-node.stdout.log') `
        -RedirectStandardError (Join-Path $logs 'napcat-node.stderr.log')
    $metadata.pid = $process.Id
    $metadata.status = 'starting'
    $metadata | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $metadataPath -Encoding UTF8
    Write-Host 'NapCat started. Login status: http://127.0.0.1:6099/ ; then run Start-Dual.cmd.'
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
