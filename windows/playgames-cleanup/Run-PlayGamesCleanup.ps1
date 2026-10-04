<#
Run-PlayGamesCleanup.ps1 - bootstrap and run the Play Games cleanup tool.

  powershell -ExecutionPolicy Bypass -File .\Run-PlayGamesCleanup.ps1 [--dry-run] [--list-only] [--yes-per-step]

Every argument is passed through to `python -m playgames_cleanup` untouched.

What it does, in order:
  1. Finds a REAL Python >= 3.11. Skips the Microsoft Store stub under WindowsApps (it
     opens the Store instead of running) and SafeHouse's vendored embeddable runtime
     (C:\SafeHouse\python: no venv, no pip, and stdlib-only by policy).
  2. If there is none, offers `winget install Python.Python.3.13` (per-user) behind a
     y/N prompt. Nothing is installed without a "y".
  3. Creates .venv next to this script and installs requirements.txt into it - only
     when requirements.txt changed since the last install (a hash stamp in .venv).
  4. Runs the tool from this folder and returns its exit code.

Run it as the normal logged-on user (NOT elevated): it drives that user's Play Games
window. Unlocking the guard is a separate, elevated step the tool prints for you.
This script is ASCII on purpose - Windows PowerShell 5.1 misreads BOM-less UTF-8.
#>

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$venv = Join-Path $here '.venv'
$venvPy = Join-Path $venv 'Scripts\python.exe'
$reqs = Join-Path $here 'requirements.txt'
$stamp = Join-Path $venv '.requirements.sha256'

function Test-RealPython([string]$exe) {
    # Returns $true when $exe is a usable CPython >= 3.11 with venv + ensurepip.
    if (-not $exe -or -not (Test-Path -LiteralPath $exe)) { return $false }
    if ($exe -like '*\WindowsApps\*') { return $false }          # Store stub / alias
    if ($exe -like 'C:\SafeHouse\python\*') { return $false }    # vendored embeddable
    try {
        $out = & $exe -c "import sys, venv, ensurepip; print(int(sys.version_info >= (3, 11)))" 2>$null
        return ($LASTEXITCODE -eq 0 -and "$out".Trim() -eq '1')
    } catch { return $false }
}

function Find-Python {
    $cands = New-Object System.Collections.Generic.List[string]
    # The py launcher knows every registered install; ask it for the newest 3.x.
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        try {
            $p = & $launcher.Source -3 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $p) { $cands.Add("$p".Trim()) }
        } catch { }
    }
    foreach ($c in (Get-Command python.exe, python3.exe -All -ErrorAction SilentlyContinue)) {
        $cands.Add($c.Source)
    }
    # Default per-user and all-users install dirs (also catches a fresh winget install
    # that is not on this session's PATH yet). Newest version first.
    $roots = @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles", "${env:ProgramFiles(x86)}")
    foreach ($r in $roots) {
        if (-not $r -or -not (Test-Path -LiteralPath $r)) { continue }
        Get-ChildItem -LiteralPath $r -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            ForEach-Object { $cands.Add((Join-Path $_.FullName 'python.exe')) }
    }
    foreach ($exe in $cands) {
        if (Test-RealPython $exe) { return $exe }
    }
    return $null
}

# --- 1/2. a real Python -------------------------------------------------------
$python = $null
if (Test-RealPython $venvPy) {
    $python = $venvPy   # venv already built; no need to look further
} else {
    $python = Find-Python
    if (-not $python) {
        Write-Host 'No usable Python 3.11+ found (the Microsoft Store stub does not count).'
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if (-not $winget) {
            Write-Host 'winget is not available either. Install Python 3.13 from python.org, then re-run.'
            exit 1
        }
        $answer = Read-Host 'Install Python 3.13 for this user with winget now? [y/N]'
        if ($answer -notmatch '^(y|yes)$') {
            Write-Host 'Not installing. Nothing was changed.'
            exit 1
        }
        & $winget.Source install --id Python.Python.3.13 --exact --scope user `
            --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) {
            Write-Host "winget failed (exit $LASTEXITCODE)."
            exit 1
        }
        $python = Find-Python
        if (-not $python) {
            Write-Host 'Python was installed but not found yet. Open a NEW PowerShell window and re-run.'
            exit 1
        }
    }
    Write-Host "Using Python: $python"
}

# --- 3. the venv ----------------------------------------------------------------
if (-not (Test-Path -LiteralPath $venvPy)) {
    Write-Host "Creating virtual environment in $venv ..."
    & $python -m venv $venv
    if ($LASTEXITCODE -ne 0) { Write-Host 'venv creation failed.'; exit 1 }
}

$want = (Get-FileHash -LiteralPath $reqs -Algorithm SHA256).Hash
$have = if (Test-Path -LiteralPath $stamp) { (Get-Content -LiteralPath $stamp -Raw).Trim() } else { '' }
if ($want -ne $have) {
    Write-Host 'Installing requirements (pywinauto, pyautogui) ...'
    & $venvPy -m pip install --disable-pip-version-check --quiet -r $reqs
    if ($LASTEXITCODE -ne 0) { Write-Host 'pip install failed.'; exit 1 }
    Set-Content -LiteralPath $stamp -Value $want -Encoding ASCII
}

# --- 4. run -----------------------------------------------------------------------
# From this folder, so `-m playgames_cleanup` resolves to the package next to us.
Push-Location -LiteralPath $here
try {
    & $venvPy -m playgames_cleanup @args
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $code
