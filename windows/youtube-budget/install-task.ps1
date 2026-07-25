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
  2. Renders config.json into ProgramData from .env (see .env.example). The .env file is
     the ONE place to set the daily limit - edit it and re-run this script to apply.
  3. Registers antivirus exclusions for the script dir + ProgramData so the watcher is
     not quarantined (skip with -NoAvExclusions). See the note below.
  4. Registers the Scheduled Task "SafeHouse-YouTubeBudget":
       trigger AtStartup, runs youtube-budget.ps1 as SYSTEM, RunLevel Highest,
       Restart=always (restart-on-failure), no execution time limit (it is a loop).
  5. Starts it now (unless -NoStart) so it is armed without a reboot.

Why the AV exclusion step: this watcher runs as SYSTEM in an infinite loop, rewrites the
hosts file, creates firewall rules and drives pktmon. That behaviour profile is close
enough to malware that heuristic AV engines quarantine it - Avast silently deleted this
very script and unregistered the task on this machine, leaving enforcement dead while the
state file still claimed "blocked". The exclusion is what makes enforcement survive.

Setup (first time):
  copy .env.example .env      # then edit YB_LIMIT_MIN etc.

Usage (elevated PowerShell; it self-elevates if you forget):
  powershell -ExecutionPolicy Bypass -File .\install-task.ps1
  powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -NoStart          # register but don't start
  powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -NoAvExclusions   # skip step 3
#>

[CmdletBinding()]
param(
  [switch]$NoStart,
  [switch]$NoAvExclusions
)

$ErrorActionPreference = 'Stop'

# --- self-elevate ------------------------------------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
  Write-Host 'Not elevated. Relaunching with admin rights (approve the UAC prompt)...'
  $a = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoExit', '-File', ('"{0}"' -f $PSCommandPath))
  if ($NoStart)        { $a += '-NoStart' }
  if ($NoAvExclusions) { $a += '-NoAvExclusions' }
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

# --- 2) Render config.json from .env -----------------------------------------
# Precedence (last wins): built-in defaults -> existing config.json -> .env
# So .env is authoritative for anything it sets, and any key it does NOT set keeps
# whatever the live config already had (e.g. a bonus granted via youtube-budget-ctl).
$cfgDst = Join-Path $Root 'config.json'
$envSrc = Join-Path $scriptDir '.env'

# .env key -> config.json key. Every value here is a positive integer.
$EnvMap = [ordered]@{
  YB_LIMIT_MIN            = 'limit_min'
  YB_SAMPLE_SEC           = 'sample_sec'
  YB_WINDOW_SEC           = 'window_sec'
  YB_MIN_THROUGHPUT_KBPS  = 'min_throughput_kbps'
  YB_PIN_IDLE_EXPIRE_MIN  = 'pin_idle_expire_min'
}

$cfg = [ordered]@{ limit_min = 180; sample_sec = 20; window_sec = 240; min_throughput_kbps = 64; pin_idle_expire_min = 30 }

# Overlay the existing live config so unrelated keys survive a re-install.
if (Test-Path $cfgDst) {
  try {
    $live = Get-Content -LiteralPath $cfgDst -Raw | ConvertFrom-Json
    foreach ($p in $live.PSObject.Properties) { $cfg[$p.Name] = $p.Value }
  } catch { Write-Warning "Could not parse existing $cfgDst - rebuilding it from defaults + .env. ($($_.Exception.Message))" }
}

# Overlay .env.
if (Test-Path $envSrc) {
  $lineNo = 0
  foreach ($line in (Get-Content -LiteralPath $envSrc)) {
    $lineNo++
    $t = $line.Trim()
    if ($t -eq '' -or $t.StartsWith('#')) { continue }
    $i = $t.IndexOf('=')
    if ($i -lt 1) { Write-Warning "${envSrc}:${lineNo}: not KEY=VALUE, ignored: $t"; continue }
    $k = $t.Substring(0, $i).Trim()
    $v = $t.Substring($i + 1).Trim().Trim('"', "'")
    if (-not $EnvMap.Contains($k)) { Write-Warning "${envSrc}:${lineNo}: unknown key '$k', ignored."; continue }
    $n = 0
    if (-not [int]::TryParse($v, [ref]$n) -or $n -le 0) {
      Write-Warning "${envSrc}:${lineNo}: $k must be a positive integer (got '$v') - keeping $($cfg[$EnvMap[$k]])."
      continue
    }
    $cfg[$EnvMap[$k]] = $n
  }
  Write-Host "    Applied $envSrc"
} else {
  Write-Host "    No .env found (copy .env.example to .env to customise) - using defaults/live config."
}

Set-Content -LiteralPath $cfgDst -Value ($cfg | ConvertTo-Json) -Encoding UTF8
Write-Host ("    Wrote {0} (limit_min={1})" -f $cfgDst, $cfg['limit_min'])

# --- 3) Antivirus exclusions -------------------------------------------------
# A SYSTEM-level infinite loop that rewrites hosts, adds firewall rules and drives
# pktmon reads as malware to heuristic engines. Avast deleted this script outright on
# this machine. Without the exclusion, enforcement dies silently.
if ($NoAvExclusions) {
  Write-Host '[*] -NoAvExclusions: skipping antivirus exclusions.'
} else {
  Write-Host '[*] Adding Microsoft Defender exclusions...'
  if (Get-Command Add-MpPreference -ErrorAction SilentlyContinue) {
    foreach ($path in @($scriptDir, $Root)) {
      try {
        Add-MpPreference -ExclusionPath $path -ErrorAction Stop
        Write-Host "    Excluded $path"
      } catch {
        Write-Warning "Could not add Defender exclusion for ${path}: $($_.Exception.Message)"
      }
    }
  } else {
    Write-Warning 'Defender cmdlets unavailable - skipping.'
  }
  Write-Host '    NOTE: third-party AV (Avast/AVG/Norton/...) needs its OWN exclusion,'
  Write-Host '          added by hand in that product. Defender exclusions do not cover it.'
}

# --- 4) Register the Scheduled Task ------------------------------------------
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

# --- 5) Start now (unless -NoStart) ------------------------------------------
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
  Change limit: edit .env (YB_LIMIT_MIN) and re-run this script, or for a one-off:
                powershell -ExecutionPolicy Bypass -File "$scriptDir\youtube-budget-ctl.ps1" set-limit 90
  Grant time:   powershell -ExecutionPolicy Bypass -File "$scriptDir\youtube-budget-ctl.ps1" grant 30
  Pause:        Stop-ScheduledTask  -TaskName $TaskName
  Uninstall:    Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false
                # then lift any active block: ...\youtube-budget-ctl.ps1 allow

  State + config + log live under C:\ProgramData\SafeHouse (Users read-only).
"@
