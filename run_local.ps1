# Local prototype runner: the same stage scripts main.nf wires, run in sequence on this machine.
# Usage: powershell -NoProfile -File run_local.ps1 -Accession PXD036557 -Python .venv\Scripts\python.exe
# Stops at the first stage that exits non-zero (e.g. spectra QC exits 2 when a file fails) and returns its exit code.
param([Parameter(Mandatory)][string]$Accession,
      [string]$Python = 'F:\ClaudeTestBuilds\aging\venv\Scripts\python.exe',
      [string]$Params)

# $PSScriptRoot is empty inside a param() default in Windows PowerShell 5.1, so the default is set here.
if (-not $Params) { $Params = Join-Path $PSScriptRoot 'params.json' }
$p = Get-Content $Params -Raw | ConvertFrom-Json
$run = Join-Path $p.work_root "run_$($p.run_date)"
$ErrorActionPreference = 'Stop'

# $ErrorActionPreference does not react to a native command's exit code in Windows PowerShell 5.1,
# so every stage is checked explicitly.
function Invoke-Stage([string]$Script, [string[]]$StageArgs) {
    & $Python "$PSScriptRoot\bin\$Script" @StageArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Error "$Script exited with code $LASTEXITCODE; stopping." -ErrorAction Continue
        exit $LASTEXITCODE
    }
}

Invoke-Stage 'db_prepare.py' @($Params, (Join-Path $p.work_root "db"))
Invoke-Stage 'discover.py'   @($Params, "$run\01_discover")
Invoke-Stage 'fetch.py'      @($Params, $Accession, "$run\$Accession\02_fetch")
Invoke-Stage 'qc_spectra.py' @($Params, "$run\$Accession\02_fetch\spectra", "$run\$Accession\02b_qc")
Invoke-Stage 'search_mm.py'  @($Params, "$run\$Accession\02_fetch\spectra", "$run\$Accession\04_search")
