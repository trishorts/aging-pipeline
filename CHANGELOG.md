# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[Semantic Versioning](https://semver.org/). Until 1.0, any release may change parameters and output
formats. The provenance schema carries its own version (`aging-provenance/N`).

## [0.1.0] - 2026-09-19

The first public release: a working prototype.

### Added
- Public documentation: `README.md`; `docs/stages.md`, `docs/configuration.md` and
  `docs/provenance.md`; `LICENSE` (MIT); `CITATION.cff`; `requirements.txt`.

### Changed
- `provenance.json` records the clone's `origin` URL as `pipeline.repo`, instead of a fixed name. The
  commit hash then always names the repository it belongs to.
- The `+dirty` marker on `pipeline.commit` covers the whole pipeline folder (parameter files, `main.nf`,
  docs), not just `bin/`. Outside a git clone, the commit reads `"unknown"`.
- `fetch.py` rejects an unknown `fetch.pick` value. Before, it silently took the smallest files.

### Fixed
- `run_local.ps1` now stops at the first stage that exits non-zero, and returns that exit code. Windows
  PowerShell 5.1 ignores native exit codes under `$ErrorActionPreference = 'Stop'`, so a failed spectra
  QC used to continue into the search.
- `run_local.ps1`'s default `-Params` resolved to the wrong folder on Windows PowerShell 5.1.
- `search_mm.py` no longer crashes before writing `provenance.json` when MetaMorpheus leaves no
  `AllPSMs.psmtsv` or `results.txt`. The failure is recorded instead.

## Prototype history (2026-09-18, before the public release)

Condensed from the commit history, in order:

- Stages 1 (discover), 2 (fetch) and 4 (MetaMorpheus search), with a provenance record on every output.
- Stage 2b: spectra QC that admits only high-resolution HCD MS2 read in the Orbitrap.
- MetaMorpheus pinned (first 1.1.10), with its release and binary hash recorded correctly. The CLI's
  `--version` prints help text instead of a version.
- Compute and memory are recorded for every stage (the resource monitor and per-task accounting).
- Stage 0 (database preparation) and parallel downloads.
- The first label-free end-to-end run, and a scaling series: 1, 6 and 18 files of PXD036557.
- Stage 9 (cleanup), which deletes raw spectra only after a successful search.
- Automatic follow-up flags in the provenance.
- Provenance chained across stages; paths made portable (relative to `work_root`); hashes reused.
- The match-between-runs counts corrected: the peaks table is unfiltered, so only *kept* MBR peaks are
  counted.
- MetaMorpheus pinned to **1.1.11**. Apex intensity. The shipped contaminant database is always searched.
- Contamination measured (PSM and intensity shares), and a 1.1.11 baseline run on 18 files.

[0.1.0]: https://github.com/trishorts/aging-pipeline/releases/tag/v0.1.0
