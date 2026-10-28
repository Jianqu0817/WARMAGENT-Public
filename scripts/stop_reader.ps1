$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
try {
    if (Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue) { throw 'Close the bot console with Ctrl+C before stopping its Reader.' }
    $listeners = @(Get-NetTCPConnection -LocalPort 5031 -State Listen -ErrorAction SilentlyContinue)
    $roots = @(
        (Join-Path $projectDir 'var/wechatagent-runtime/workspace'),
        (Join-Path (Split-Path -Parent $projectDir) 'MYATRI-AstrBot/var/wechatagent-runtime/workspace')
    )
    foreach ($processId in @($listeners.OwningProcess | Select-Object -Unique)) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$processId"
        $matched = $false
        foreach ($workspace in $roots) {
            $readerPath = [IO.Path]::GetFullPath((Join-Path $workspace 'scripts/reader-api.py'))
            if ($process.CommandLine.Contains($readerPath)) { $matched = $true }
        }
        if (-not $matched -or $process.Name -ne 'python.exe') { throw 'Port 5031 belongs to another process; it was left running.' }
        Stop-Process -Id $processId -ErrorAction Stop
        Wait-Process -Id $processId -Timeout 10 -ErrorAction SilentlyContinue
    }
    Write-Host 'Reader stopped. Start-WeChat.cmd will use the configured local copy.'
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
