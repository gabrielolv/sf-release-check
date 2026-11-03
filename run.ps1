<#
.SYNOPSIS
  Salesforce Release Impact Check - Windows launcher.
.EXAMPLE
  .\run.ps1                       # interactive (log in / pick org, pick PDF)
  .\run.ps1 --target-org MYSANDBOX   # any release_check.py argument is passed through
  .\run.ps1 --no-ai
#>
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $python) { Write-Error "Python 3.10+ is required: https://www.python.org/downloads/"; exit 1 }
if (-not (Get-Command sf -ErrorAction SilentlyContinue)) {
  Write-Error "Salesforce CLI (sf) is required: https://developer.salesforce.com/tools/salesforcecli"; exit 1
}

& $python -c "import pymupdf, openpyxl" 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Installing Python dependencies..."
  & $python -m pip install --user -q -r requirements.txt
}

& $python release_check.py @args
exit $LASTEXITCODE
