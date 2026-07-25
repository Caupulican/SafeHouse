<#
.SYNOPSIS
    SafeHouse — one-click install. Sets up every layer on a fresh Windows + WSL2 machine.

.DESCRIPTION
    Right-click -> "Run with PowerShell", or:

        powershell -ExecutionPolicy Bypass -File .\Install-SafeHouse.ps1

    Self-elevates. Re-runnable: every step is idempotent, so this is also the upgrade
    and the repair path, not just the first install.

    WHY A SINGLE SCRIPT
    The stack spans two operating systems. The DNS layer lives in WSL2 (Docker), and
    the enforcement layer lives in Windows and needs UAC. Neither half is useful alone:
    DNS filtering without the Windows side cannot meter or block YouTube by time, and
    the Windows side without DNS filtering is trivially bypassed. Splitting the install
    across two manuals is how machines end up half-configured — which is the worst
    state for a parental control, because it still *looks* installed.

    WHAT IT DOES, IN ORDER
      1. Checks WSL2 is present and the distro is running.
      2. Runs scripts/deploy.sh inside WSL: installs Docker if missing, brings up
         Pi-hole and AdGuard Home, loads the blocklists, configures AdGuard
         non-interactively (upstream, parental settings, ad-SDK filter lists).
      3. Detects the WSL IP and points the Windows resolver at it — adapter DNS plus
         a global NRPT rule, because the adapter setting alone is ignored when the
         router advertises an IPv6 resolver.
      4. Stages the Windows scripts to C:\SafeHouse.
      5. Vendors an embeddable Python runtime (no system Python required).
      6. Installs the YouTube watch-time budget, DEFAULT 3 HOURS/DAY, as a SYSTEM
         scheduled task that starts at boot.
      7. Applies the Opera GX policy that closes its built-in VPN and DoH — both of
         which bypass every DNS-layer control here.
      8. Arms the Windows Firewall ad-block group for the Google Play Games VM.
      9. Adds Defender exclusions, without which the watcher gets quarantined as
         malware (it runs as SYSTEM, rewrites hosts and drives pktmon).
     10. Verifies the result and prints what to do next.

.PARAMETER LimitMinutes
    Daily YouTube budget in minutes. Default 180 (3 hours).

.PARAMETER SkipWsl
    Skip the WSL/Docker half. For repairing only the Windows side.

.PARAMETER SkipFirewall
    Skip the Play Games firewall step (it needs the VM running to capture anything).

.PARAMETER WhatIf
    Print each step without changing anything.
#>
[CmdletBinding()]
param(
    [int]$LimitMinutes = 180,
    [switch]$SkipWsl,
    [switch]$SkipFirewall,
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$script:Failures = @()
$script:Warnings = @()

# --- self-elevate ------------------------------------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    Write-Host 'Not elevated. Relaunching with admin rights (approve the UAC prompt)...' -ForegroundColor Yellow
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoExit', '-File', ('"{0}"' -f $PSCommandPath),
                 '-LimitMinutes', $LimitMinutes)
    if ($SkipWsl)      { $argList += '-SkipWsl' }
    if ($SkipFirewall) { $argList += '-SkipFirewall' }
    if ($WhatIf)       { $argList += '-WhatIf' }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argList
    return
}

$RepoDir  = Split-Path -Parent $PSCommandPath
$StageDir = 'C:\SafeHouse'
$DataDir  = 'C:\ProgramData\SafeHouse'
$step     = 0

function Write-Step([string]$Text) {
    $script:step++
    Write-Host ''
    Write-Host ("[{0}] {1}" -f $script:step, $Text) -ForegroundColor Cyan
}
function Write-Ok([string]$Text)   { Write-Host "    $Text" -ForegroundColor Green }
function Write-Info([string]$Text) { Write-Host "    $Text" }
function Write-Warn([string]$Text) {
    Write-Host "    ! $Text" -ForegroundColor Yellow
    $script:Warnings += $Text
}
function Write-Fail([string]$Text) {
    Write-Host "    X $Text" -ForegroundColor Red
    $script:Failures += $Text
}

Write-Host ''
Write-Host '=============================================================' -ForegroundColor White
Write-Host '  SafeHouse - network-level ad blocking + parental controls' -ForegroundColor White
Write-Host ("  YouTube budget: {0} minutes/day" -f $LimitMinutes) -ForegroundColor White
if ($WhatIf) { Write-Host '  *** -WhatIf: nothing will be changed ***' -ForegroundColor Yellow }
Write-Host '=============================================================' -ForegroundColor White

# --- 1. WSL / DNS stack ------------------------------------------------------
if ($SkipWsl) {
    Write-Step 'WSL stack - SKIPPED (-SkipWsl)'
} else {
    Write-Step 'Checking WSL2...'
    $wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
    if (-not $wsl) {
        Write-Fail 'WSL is not installed. Install it first:  wsl --install'
    } else {
        $distro = (& wsl.exe -l -q 2>$null | Where-Object { $_.Trim() } | Select-Object -First 1)
        if (-not $distro) {
            Write-Fail 'No WSL distro found. Install one:  wsl --install -d Debian'
        } else {
            $distro = $distro.Trim()
            Write-Ok "distro: $distro"

            Write-Step 'Deploying the DNS stack inside WSL (Pi-hole + AdGuard Home)...'
            # Translate the repo path so the same checkout is used from inside WSL,
            # rather than requiring a second copy that can drift.
            $repoWsl = (& wsl.exe -d $distro wslpath -a ("'" + $RepoDir.Replace('\','/') + "'") 2>$null)
            if (-not $repoWsl) { $repoWsl = (& wsl.exe -d $distro wslpath -a $RepoDir 2>$null) }
            $repoWsl = "$repoWsl".Trim()
            if (-not $repoWsl) {
                Write-Fail 'Could not translate the repo path into WSL.'
            } elseif ($WhatIf) {
                Write-Info "would run: $repoWsl/scripts/deploy.sh --with-adguard"
            } else {
                Write-Info "running $repoWsl/scripts/deploy.sh --with-adguard (this can take several minutes)"
                & wsl.exe -d $distro bash -lc "cd '$repoWsl' && chmod +x scripts/*.sh && ./scripts/deploy.sh --with-adguard"
                if ($LASTEXITCODE -ne 0) { Write-Fail "deploy.sh exited $LASTEXITCODE" }
                else { Write-Ok 'DNS stack deployed' }
            }
        }
    }
}

# --- 2. Point Windows at the WSL resolver ------------------------------------
Write-Step 'Pointing Windows DNS at the WSL resolver...'
if ($WhatIf) {
    Write-Info 'would configure DNS via safehouse_setup.dns (adapter + NRPT, with rollback)'
} else {
    # DNS configuration lives in Python now (safehouse_setup.dns), which verifies
    # resolution afterwards and rolls its own NRPT rule back if the machine cannot
    # resolve. Run after the runtime is vendored.
    Write-Info 'deferred until the Python runtime is in place (step 4)'
    $script:DeferredDns = $true
}

# --- 3. Stage the Windows scripts -------------------------------------------
Write-Step "Staging Windows scripts to $StageDir..."
if ($WhatIf) {
    Write-Info "would copy $RepoDir\windows -> $StageDir\windows"
} else {
    New-Item -ItemType Directory -Force -Path $StageDir | Out-Null
    Copy-Item -Path (Join-Path $RepoDir 'windows') -Destination $StageDir -Recurse -Force
    Copy-Item -Path (Join-Path $RepoDir 'blocklists') -Destination $StageDir -Recurse -Force -ErrorAction SilentlyContinue
    # The Python watcher package lives beside the PowerShell one it will replace.
    $pyPkg = Join-Path $RepoDir 'python\safehouse_ytbudget'
    if (Test-Path $pyPkg) {
        Copy-Item -Path $pyPkg -Destination (Join-Path $StageDir 'windows\youtube-budget') -Recurse -Force
        $setupPkg = Join-Path $RepoDir 'python\safehouse_setup'
        if (Test-Path $setupPkg) {
            Copy-Item -Path $setupPkg -Destination (Join-Path $StageDir 'windows\youtube-budget') -Recurse -Force
        }
        foreach ($f in @('install.py', 'adguard_daily_reset.py')) {
            $src = Join-Path $RepoDir "python\$f"
            if (Test-Path $src) { Copy-Item $src (Join-Path $StageDir 'windows\youtube-budget') -Force }
        }
        $tools = Join-Path $RepoDir 'python\tools'
        if (Test-Path $tools) { Copy-Item $tools (Join-Path $StageDir 'windows\youtube-budget') -Recurse -Force }
    }
    Write-Ok "staged to $StageDir"
}

# --- 4. Vendor an embeddable Python -----------------------------------------
Write-Step 'Vendoring the Python runtime...'
$pyDir = Join-Path $StageDir 'python'
$pyExe = Join-Path $pyDir 'python.exe'
$pyVer = '3.13.5'
if (Test-Path $pyExe) {
    Write-Ok "already present: $pyExe"
} elseif ($WhatIf) {
    Write-Info "would download embeddable Python $pyVer to $pyDir"
} else {
    try {
        # Embeddable: self-contained, no installer, no registry, no PATH dependency.
        # The watcher runs as SYSTEM at boot, so it cannot rely on a Python installed
        # in some user's profile.
        $url = "https://www.python.org/ftp/python/$pyVer/python-$pyVer-embed-amd64.zip"
        $zip = Join-Path $env:TEMP "python-embed-$pyVer.zip"
        Write-Info "downloading $url"
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        New-Item -ItemType Directory -Force -Path $pyDir | Out-Null
        Expand-Archive -Path $zip -DestinationPath $pyDir -Force
        Remove-Item $zip -Force -ErrorAction SilentlyContinue
        # The embeddable distribution ignores PYTHONPATH by design, so the package
        # search path has to be declared in the ._pth file.
        $pth = Get-ChildItem $pyDir -Filter 'python*._pth' | Select-Object -First 1
        if ($pth) {
            @('python313.zip', '.', '', '# SafeHouse: the watcher package.',
              "$StageDir\windows\youtube-budget") | Set-Content -Path $pth.FullName -Encoding ASCII
        }
        Write-Ok "vendored Python $pyVer"
    } catch { Write-Fail "Python download failed: $($_.Exception.Message)" }
}

# --- 4b. DNS (deferred until the runtime exists) -----------------------------
if ($script:DeferredDns -and -not $WhatIf) {
    Write-Step 'Configuring Windows DNS (Python)...'
    if (Test-Path $pyExe) {
        $out = & $pyExe -c "from safehouse_setup import dns; import json; print(json.dumps(dns.apply()))" 2>&1
        if ($LASTEXITCODE -eq 0) { Write-Ok "DNS configured: $out" }
        else { Write-Fail "DNS configuration failed: $out" }
    } else { Write-Fail 'Python runtime missing; cannot configure DNS' }
}

# --- 5. Defender exclusions --------------------------------------------------
Write-Step 'Adding Defender exclusions...'
if ($WhatIf) {
    Write-Info "would exclude $StageDir and $DataDir"
} else {
    # Without these the watcher is quarantined: it runs as SYSTEM in an infinite loop,
    # rewrites the hosts file, creates firewall rules and drives pktmon. That profile
    # reads as malware to heuristic engines, and enforcement then dies silently.
    foreach ($p in @($StageDir, $DataDir)) {
        try { Add-MpPreference -ExclusionPath $p -ErrorAction Stop; Write-Ok "excluded $p" }
        catch { Write-Warn "could not exclude ${p}: $($_.Exception.Message)" }
    }
    Write-Info 'third-party AV needs its own exclusion, added in that product'
}

# --- 6. YouTube budget -------------------------------------------------------
Write-Step ("Installing the YouTube watch-time budget ({0} min/day)..." -f $LimitMinutes)
$ybDir = Join-Path $StageDir 'windows\youtube-budget'
if ($WhatIf) {
    Write-Info "would write $ybDir\.env with YB_LIMIT_MIN=$LimitMinutes and run install-task.ps1"
} else {
    $envPath = Join-Path $ybDir '.env'
    @(
        '# SafeHouse - YouTube watch-time budget.',
        '# Edit, then re-run install-task.ps1 to apply. See .env.example for every key.',
        '',
        "YB_LIMIT_MIN=$LimitMinutes",
        'YB_SAMPLE_SEC=20',
        'YB_WINDOW_SEC=240',
        'YB_MIN_THROUGHPUT_KBPS=64',
        'YB_PIN_IDLE_EXPIRE_MIN=30'
    ) | Set-Content -Path $envPath -Encoding UTF8
    Write-Ok "wrote $envPath (YB_LIMIT_MIN=$LimitMinutes)"

    $installTask = Join-Path $ybDir 'install-task.ps1'
    if (Test-Path $installTask) {
        try {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installTask
            Write-Ok 'watcher armed (SYSTEM, at boot, restart-on-failure)'
        } catch { Write-Fail "install-task.ps1 failed: $($_.Exception.Message)" }
    } else { Write-Fail "not found: $installTask" }
}

# --- 7. Opera GX policy ------------------------------------------------------
Write-Step 'Applying the Opera GX lockdown policy...'
if ($WhatIf) {
    Write-Info 'would apply browser policy via safehouse_setup.browser'
} else {
    if (Test-Path $pyExe) {
        $rc = & $pyExe -c "from safehouse_setup import browser; browser.apply(); browser.forbid_vpn_in_profiles(); print('ok')" 2>&1
        if ($LASTEXITCODE -eq 0) { Write-Ok 'Opera GX VPN + DoH pinned off by machine policy' }
        else { Write-Warn "browser policy failed: $rc" }
    } else { Write-Warn 'Python runtime missing; skipped' }
}

# --- 8. Play Games firewall --------------------------------------------------
if ($SkipFirewall) {
    Write-Step 'Play Games ad firewall - SKIPPED (-SkipFirewall)'
} else {
    Write-Step 'Arming the Play Games ad firewall...'
    if ($WhatIf) {
        Write-Info 'would run windows\safehouse-adblock.ps1 -RebuildOnly'
    } else {
        $adblock = Join-Path $StageDir 'windows\safehouse-adblock.ps1'
        if (Test-Path $adblock) {
            try {
                & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $adblock -RebuildOnly
                Write-Ok 'firewall group rebuilt from the ruleset'
            } catch { Write-Warn "safehouse-adblock.ps1 failed: $($_.Exception.Message)" }
        } else { Write-Warn "not found: $adblock" }
    }
}

# --- 9. Verify ---------------------------------------------------------------
Write-Step 'Verifying...'
if (-not $WhatIf) {
    Clear-DnsClientCache -ErrorAction SilentlyContinue

    $ad = (Resolve-DnsName 'doubleclick.net' -Type A -ErrorAction SilentlyContinue |
           Select-Object -Expand IPAddress -First 1)
    if ($ad -eq '0.0.0.0') { Write-Ok 'DNS filtering active (doubleclick.net -> 0.0.0.0)' }
    else { Write-Warn "ad domain resolved to '$ad' - DNS filtering may not be active yet" }

    $ok = (Resolve-DnsName 'github.com' -Type A -ErrorAction SilentlyContinue |
           Select-Object -Expand IPAddress -First 1)
    if ($ok -and $ok -ne '0.0.0.0') { Write-Ok "normal resolution works (github.com -> $ok)" }
    else { Write-Fail 'github.com does not resolve - DNS is broken, investigate before leaving this machine' }

    $task = Get-ScheduledTask -TaskName 'SafeHouse-YouTubeBudget' -ErrorAction SilentlyContinue
    if ($task) { Write-Ok "watcher task: $($task.State)" } else { Write-Fail 'watcher task not registered' }
}

# --- summary -----------------------------------------------------------------
Write-Host ''
Write-Host '=============================================================' -ForegroundColor White
if ($script:Failures.Count -eq 0) {
    Write-Host '  INSTALL COMPLETE' -ForegroundColor Green
} else {
    Write-Host '  INSTALL FINISHED WITH ERRORS' -ForegroundColor Red
    foreach ($f in $script:Failures) { Write-Host "   X $f" -ForegroundColor Red }
}
foreach ($w in $script:Warnings) { Write-Host "   ! $w" -ForegroundColor Yellow }
Write-Host '=============================================================' -ForegroundColor White
Write-Host @"

  Daily YouTube budget : $LimitMinutes minutes
  Status               : C:\SafeHouse\windows\youtube-budget\youtube-budget-ctl.ps1 status
  Grant more time      : ...\youtube-budget-ctl.ps1 grant 30
  Block now            : ...\youtube-budget-ctl.ps1 block
  Pi-hole dashboard    : http://localhost:8053/admin
  AdGuard dashboard    : http://localhost:3000

  STILL WORTH DOING BY HAND:
   * Restart Google Play Games fully (tray -> Quit). The crosvm VM reads host DNS
     only at launch, so it keeps using the old resolver until it is restarted.
   * Add an adult-content blocklist if you want one - none ships with this repo.
     See blocklists/adlists.txt.
   * If you use a third-party antivirus, exclude C:\SafeHouse in that product too.

"@
