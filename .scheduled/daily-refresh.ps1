# Daily /refresh runner — invoked by Windows Task Scheduler "frontier-refresh"
#
# Steps:
#   1. cd into the project root
#   2. ensure the Docker backend is up (idempotent — no-op if already running)
#   3. invoke `claude -p "/refresh"` headlessly; output appended to refresh.log
#
# Re-run setup with .scheduled\register-task.ps1 to (re)create the scheduled task.

$ErrorActionPreference = 'Continue'
# Resolve paths relative to this script's location so the task works for any user/install path.
$projectDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$logFile    = Join-Path $projectDir 'refresh.log'
$claudeExe  = if (Get-Command claude.exe -ErrorAction SilentlyContinue) {
    (Get-Command claude.exe).Source
} else {
    Join-Path $env:USERPROFILE '.local\bin\claude.exe'
}

Set-Location $projectDir

$ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz')
"`n=== $ts  starting daily-refresh ===" | Out-File -Append -Encoding utf8 $logFile

# Ensure backend is up. `up -d` is idempotent — exits fast if container already healthy.
try {
    docker compose up -d backend 2>&1 | Out-File -Append -Encoding utf8 $logFile
} catch {
    "docker compose up failed: $_" | Out-File -Append -Encoding utf8 $logFile
    exit 1
}

# Wait for backend to respond (max 60s)
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        # Use 127.0.0.1, NOT localhost: the scheduled task runs under Windows PowerShell 5.1,
        # whose Invoke-RestMethod resolves localhost to IPv6 ::1 with no IPv4 fallback, while
        # Docker publishes the port on IPv4 127.0.0.1 only. localhost here times out for 60s.
        Invoke-RestMethod 'http://127.0.0.1:8765/api/sources' -TimeoutSec 2 | Out-Null
        $ready = $true; break
    } catch { Start-Sleep -Seconds 1 }
}
if (-not $ready) {
    "backend did not respond after 60s; aborting" | Out-File -Append -Encoding utf8 $logFile
    exit 1
}

# Run /refresh non-interactively. -p prints the response and exits.
& $claudeExe -p '/refresh' 2>&1 | Out-File -Append -Encoding utf8 $logFile

"=== finished at $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz')) ===" | Out-File -Append -Encoding utf8 $logFile
