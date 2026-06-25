# Register the PiholeGPG-DNS logon task (runs as the logged-on user, highest privileges).
$script = Join-Path $env:USERPROFILE '.pihole\set-dns.ps1'
$action    = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $script + '"')
$trigger   = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'PiholeGPG-DNS' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
New-Item -ItemType Directory -Force C:\Temp | Out-Null
'TASK_CREATED' | Out-File C:\Temp\task_done.txt -Encoding ASCII
