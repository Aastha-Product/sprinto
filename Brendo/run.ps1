# Brendo launcher --- runs the CLI from the project with PYTHONPATH set, so you can
# call it from anywhere and don't have to remember the setup.
#
#   .\run.ps1 --offline              # mock demo on the bundled sample sites
#   .\run.ps1 --check                # real-mode readiness report
#   .\run.ps1 --why acme.example     # decision log for one domain
#   .\run.ps1 --domains inputs\domains.csv
#
# It finds a working Python: the py launcher or a real python.exe if you have one,
# otherwise the interpreter set up for this session. If none exists it tells you how
# to install Python.

$ErrorActionPreference = "Stop"

function Find-Python {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { return , @("py", "-3") }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python -and $python.Source -and ($python.Source -notlike "*WindowsApps*")) {
        return , @($python.Source)
    }
    $venv = Join-Path $env:LOCALAPPDATA "hermes\hermes-agent\venv\Scripts\python.exe"
    if (Test-Path $venv) { return , @($venv) }
    return $null
}

$pyCmd = Find-Python
if (-not $pyCmd) {
    Write-Host "No usable Python found."
    Write-Host "Install Python 3 from https://www.python.org/downloads (tick 'Add python.exe to PATH'),"
    Write-Host "reopen PowerShell, then run this again."
    exit 1
}

Set-Location $PSScriptRoot
$env:PYTHONPATH = "src"

$extra = @()
if ($pyCmd.Count -gt 1) { $extra = $pyCmd[1..($pyCmd.Count - 1)] }
& $pyCmd[0] @extra -m brendo.cli @args
