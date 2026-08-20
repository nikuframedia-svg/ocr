# Regista a tarefa agendada do Windows que corre o drive_pull 2x/dia
# (06:45 e 12:45 locais — meia hora antes do sync do servidor, para os scans
# entrarem na fila da GPU antes do pico). Correr UMA vez, como o utilizador
# que opera a app (nao precisa de admin para tarefas do proprio utilizador):
#
#   powershell -ExecutionPolicy Bypass -File scripts\ops\register_drive_pull.ps1
#
$ErrorActionPreference = "Stop"

$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$script = "$root\scripts\ops\drive_pull.ps1"
$taskName = "OCR Drive Pull"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$triggers = @(
    (New-ScheduledTaskTrigger -Daily -At 06:45),
    (New-ScheduledTaskTrigger -Daily -At 12:45)
)
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers `
    -Settings $settings | Out-Null
Write-Host "Tarefa '$taskName' registada (06:45 e 12:45, diaria)."
Write-Host "Testar agora:  powershell -ExecutionPolicy Bypass -File `"$script`" -- --dry-run"
