# Register the "frontier-refresh" Windows Scheduled Task.
# Re-run any time the schedule changes — Register-ScheduledTask -Force overwrites.
#
# Usage (from project root):
#   pwsh -File .\.scheduled\register-task.ps1
#
# Default: twice weekly, Monday & Thursday at 14:00 local time.
# To change days/cadence, edit the -DaysOfWeek list (or swap to -Daily) on the trigger below.

$projectDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$scriptPath = Join-Path $PSScriptRoot 'daily-refresh.ps1'

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`"" `
    -WorkingDirectory $projectDir

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Thursday -At '14:00'

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

# Run only when the user is logged in. No stored credentials needed.
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName 'frontier-refresh' `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Twice-weekly (Mon & Thu) Anthropic-signal /refresh for the Frontier Monitor dashboard.' `
    -Force | Out-Null

Write-Host "Registered scheduled task 'frontier-refresh' (Mon & Thu at 14:00 local)."
Write-Host "Next run: $((Get-ScheduledTaskInfo -TaskName 'frontier-refresh').NextRunTime)"
Write-Host "Inspect via: Get-ScheduledTask -TaskName 'frontier-refresh' | Get-ScheduledTaskInfo"
Write-Host "Manually trigger: Start-ScheduledTask -TaskName 'frontier-refresh'"
Write-Host "Logs at: $projectDir\refresh.log"
