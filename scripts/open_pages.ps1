param(
    [ValidateSet('All','Monitor','Context')][string]$Page = 'All',
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectDir '.venv/Scripts/python.exe'
try {
    if (-not (Test-Path -LiteralPath $python)) { throw 'Run Setup.cmd first.' }
    $logs = Join-Path $projectDir 'var/logs'
    New-Item -ItemType Directory -Path $logs -Force | Out-Null
    $pages = @(
        @{ Name='Monitor'; Module='monitor'; Port=8767 },
        @{ Name='Context'; Module='context_viewer'; Port=8768 }
    )
    foreach ($item in $pages) {
        if ($Page -ne 'All' -and $Page -ne $item.Name) { continue }
        if (-not (Get-NetTCPConnection -LocalPort $item.Port -State Listen -ErrorAction SilentlyContinue)) {
            $arguments = @('-c', ('"from warmagent.' + $item.Module + ' import main; main()"'), '--project', ('"' + $projectDir + '"'))
            $process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $projectDir -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput (Join-Path $logs ($item.Module + '.stdout.log')) `
                -RedirectStandardError (Join-Path $logs ($item.Module + '.stderr.log'))
            $deadline = (Get-Date).AddSeconds(15)
            while (-not (Get-NetTCPConnection -LocalPort $item.Port -State Listen -ErrorAction SilentlyContinue)) {
                if ($process.HasExited -or (Get-Date) -gt $deadline) { throw ($item.Name + ' did not start. Check var/logs/' + $item.Module + '.stderr.log.') }
                Start-Sleep -Milliseconds 250
            }
        }
        $url = 'http://127.0.0.1:' + $item.Port + '/'
        if (-not $NoBrowser) { Start-Process $url | Out-Null }
        Write-Host ($item.Name + ': ' + $url)
    }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
