<#
SafeHouse - YouTube watch-time budget: installer / ARM (Windows-native).

  *** RUNNING THIS ARMS LIVE ENFORCEMENT. ***
  From here on, once a day's measured YouTube watching crosses the limit, the watcher
  splices a YouTube block into the LIVE Windows hosts file and flushes DNS - i.e. it
  actually starts blocking YouTube on this machine. Disarm with the Unregister command
  printed at the end.

What it does (self-elevates via UAC - the USER runs this in an elevated PowerShell):
  1. Creates C:\ProgramData\SafeHouse with a locked-down ACL:
       SYSTEM + Administrators = FullControl, Users = ReadAndExecute (inheritance off).
     So a standard (non-admin) user can READ the state/log but cannot edit or reset it.
  2. Seeds config.json (limit/sample/window) into ProgramData if not already there.
  3. Registers the Scheduled Task "SafeHouse-YouTubeBudget":
       trigger AtStartup, runs youtube-budget.ps1 as SYSTEM, RunLevel Highest,
       Restart=always (restart-on-failure), no execution time limit (it is a loop).
  4. Starts it now (unless -NoStart) so it is armed without a reboot.

Usage (elevated PowerShell; it self-elevates if you forget):
  powershell -ExecutionPolicy Bypass -File .\install-task.ps1
  powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -NoStart   # register but don't start yet
#>

[CmdletBinding()]
param(
  [switch]$NoStart
)

$ErrorActionPreference = 'Stop'

# --- self-elevate ------------------------------------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
  Write-Host 'Not elevated. Relaunching with admin rights (approve the UAC prompt)...'
  $a = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoExit', '-File', ('"{0}"' -f $PSCommandPath))
  if ($NoStart) { $a += '-NoStart' }
  Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $a
  return
}

$TaskName  = 'SafeHouse-YouTubeBudget'
$scriptDir = Split-Path -Parent $PSCommandPath
$watcher   = Join-Path $scriptDir 'youtube-budget.ps1'
$Root      = 'C:\ProgramData\SafeHouse'

if (-not (Test-Path $watcher)) { Write-Error "Watcher not found: $watcher"; return }
$watcher = (Resolve-Path $watcher).Path

# --- 1) ProgramData dir + locked-down ACL ------------------------------------
Write-Host "[*] Ensuring $Root with a locked-down ACL..."
if (-not (Test-Path $Root)) { New-Item -ItemType Directory -Force -Path $Root | Out-Null }

$system = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')      # NT AUTHORITY\SYSTEM
$admins = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')  # BUILTIN\Administrators
$users  = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-545')  # BUILTIN\Users
$inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
$noProp  = [System.Security.AccessControl.PropagationFlags]::None
$allow   = [System.Security.AccessControl.AccessControlType]::Allow

$acl = Get-Acl -Path $Root
$acl.SetAccessRuleProtection($true, $false)   # disable inheritance, drop inherited ACEs
foreach ($r in @($acl.Access)) { [void]$acl.RemoveAccessRule($r) }
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($system, 'FullControl',   $inherit, $noProp, $allow)))
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($admins, 'FullControl',   $inherit, $noProp, $allow)))
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($users,  'ReadAndExecute', $inherit, $noProp, $allow)))
$acl.SetOwner($admins)
Set-Acl -Path $Root -AclObject $acl
Write-Host '    ACL set: SYSTEM + Administrators = Full, Users = Read.'

# --- 2) Seed config.json -----------------------------------------------------
$cfgDst = Join-Path $Root 'config.json'
$cfgSrc = Join-Path $scriptDir 'config.json'
if (-not (Test-Path $cfgDst)) {
  if (Test-Path $cfgSrc) { Copy-Item -LiteralPath $cfgSrc -Destination $cfgDst -Force }
  else { Set-Content -LiteralPath $cfgDst -Value (@{ limit_min = 60; sample_sec = 20; window_sec = 240; min_throughput_kbps = 64; dns_gate_min = 20 } | ConvertTo-Json) -Encoding UTF8 }
  Write-Host "    Seeded $cfgDst"
} else {
  Write-Host "    Kept existing $cfgDst"
}

# --- 3) Register the Scheduled Task ------------------------------------------
Write-Host "[*] Registering Scheduled Task '$TaskName' (AtStartup, SYSTEM, Restart=always)..."
$action    = New-ScheduledTaskAction -Execute 'powershell.exe' `
               -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $watcher)
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principalS = New-ScheduledTaskPrincipal -UserId 'S-1-5-18' -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet `
               -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
               -StartWhenAvailable `
               -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
               -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
               -MultipleInstances IgnoreNew `
               -Hidden
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Principal $principalS -Settings $settings -Force | Out-Null
Write-Host "    Registered '$TaskName'."

# --- 4) Start now (unless -NoStart) ------------------------------------------
if (-not $NoStart) {
  Start-ScheduledTask -TaskName $TaskName
  Write-Host "[*] Started '$TaskName' now (armed without a reboot)."
} else {
  Write-Host "[*] -NoStart: registered but not started (arms on next boot)."
}

# --- summary -----------------------------------------------------------------
Write-Host ''
Write-Host '[!] ARMED: live YouTube watch-time budget enforcement is active.' -ForegroundColor Yellow
Write-Host @"

  Verify:       Get-ScheduledTask -TaskName $TaskName
                Get-Content C:\ProgramData\SafeHouse\youtube-budget.log -Tail 10
  Status:       powershell -ExecutionPolicy Bypass -File "$scriptDir\youtube-budget-ctl.ps1" status
  Change limit: powershell -ExecutionPolicy Bypass -File "$scriptDir\youtube-budget-ctl.ps1" set-limit 90
  Grant time:   powershell -ExecutionPolicy Bypass -File "$scriptDir\youtube-budget-ctl.ps1" grant 30
  Pause:        Stop-ScheduledTask  -TaskName $TaskName
  Uninstall:    Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false
                # then lift any active block: ...\youtube-budget-ctl.ps1 allow

  State + config + log live under C:\ProgramData\SafeHouse (Users read-only).
"@
