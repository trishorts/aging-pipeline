# Local prototype runner: the same stage scripts main.nf wires, run in sequence on this machine.
# Usage: powershell -NoProfile -File run_local.ps1 -Accession PXD048658
param([Parameter(Mandatory)][string]$Accession,
      [string]$Python = 'F:\ClaudeTestBuilds\aging\venv\Scripts\python.exe',
      [string]$Params = "$PSScriptRoot\params.json")

$p = Get-Content $Params -Raw | ConvertFrom-Json
$run = Join-Path $p.work_root "run_$($p.run_date)"
$ErrorActionPreference = 'Stop'

& $Python "$PSScriptRoot\bin\db_prepare.py" $Params (Join-Path $p.work_root "db")
& $Python "$PSScriptRoot\bin\discover.py" $Params "$run\01_discover"
& $Python "$PSScriptRoot\bin\fetch.py" $Params $Accession "$run\$Accession\02_fetch"
& $Python "$PSScriptRoot\bin\qc_spectra.py" $Params "$run\$Accession\02_fetch\spectra" "$run\$Accession\02b_qc"
& $Python "$PSScriptRoot\bin\search_mm.py" $Params "$run\$Accession\02_fetch\spectra" "$run\$Accession\04_search"
