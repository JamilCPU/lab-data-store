# Register a weekly Windows scheduled task (Sunday 04:00, current user) that runs pull-backup.ps1.
# Only registers; it does not run the task. LAB_DATA_STORE_SSH (user@host) must be set in the user's
# environment (e.g. [Environment]::SetEnvironmentVariable('LAB_DATA_STORE_SSH','user@host','User')).
# PowerShell 5.1 compatible.
$ErrorActionPreference = 'Stop'

$script = Join-Path $PSScriptRoot 'pull-backup.ps1'
if (-not (Test-Path $script)) {
    Write-Error "pull-backup.ps1 not found next to this script: $script"
    exit 1
}
if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable('LAB_DATA_STORE_SSH', 'User'))) {
    Write-Warning 'LAB_DATA_STORE_SSH is not set in the user environment; the task will fail until it is.'
}

$action = New-ScheduledTaskAction -Execute 'powershell' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '04:00'
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName 'LabDataStore-BackupPull' -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Write-Host 'Registered LabDataStore-BackupPull (weekly, Sunday 04:00). Not run.'
