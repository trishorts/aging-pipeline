# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[Semantic Versioning](https://semver.org/). Until 1.0, any release may change parameters and output
formats. The provenance schema carries its own version (`aging-provenance/N`).

## [Unreleased]

### Changed
- **The contaminant panel is searched without its human spike-in standard** (`database.contaminant_exclude`, default
  `data/contaminant_panel_exclude_v1.tsv`, 41 entries). MetaMorpheus's shipped panel carries a UPS1/UPS2-like
  standard (PRDX1, SOD1, CAT, CKM, MAPT and others). In every mouse and rat dataset it relabelled 2-15 native
  proteins as contaminant, including 10% of reported contamination in an old-rat muscle deposit. The reduced
  panel is content-addressed and recorded as `contaminant_panel` (both inputs' sha256, the removed accessions, the
  file searched). Salivary amylase, dermokine and lactotransferrin stay in the panel as real handling contaminants.

### Added
- **`fetch.pick = probe_spread`**: probe up to three files, the median by size and the first and last
  by name, instead of one. PXD022196 passed a one-file probe on its median QE-HF file and failed full QC
  after all 43 files had downloaded, because 11 Fusion files are ion-trap CID. Their names sort to one end.

### Fixed
- **`id_rate.ms2` counts only the files the search saw.** It summed the QC report, which covers every file
  on disk, so a file in `search.exclude_files` (D52) inflated the denominator: PXD051644's excluded blank
  added 101 MS2 scans that `results.txt` never counted (381,923 against 381,822). `reprovenance.py`
  re-derives an existing record the same way.

### Changed
- **The qc payload names the grain of every count, and its PSM metrics use MetaMorpheus's own 1% filter.**
  The per-file `psms` / `peptides` / `protein_groups` counts now carry the run-grain definitions
  `aging:DEF-PSM-1PCT-RUN`, `DEF-PEPTIDE-1PCT-RUN` and `DEF-PROTEINGROUP-1PCT-RUN` instead of the dataset
  ones, because MetaMorpheus recomputes FDR on each file alone for those lines. PSM-derived metrics and
  distributions now require `QValue Notch ≤ 0.01` and an unambiguous notch as well as `QValue ≤ 0.01`
  (`DEF-PSM-1PCT-INFILE v1`); before, they filtered on `QValue` alone, which is a slightly wider set than
  MetaMorpheus accepts. `contaminant_psm_share` moves to `DEF-CONTAM-PSM-RUN v2` for the same reason.
  `contaminant_intensity_frac` no longer applies a 1% protein filter that `DEF-QC-9` does not state, so it
  now agrees with the provenance block (19.1% was 18.92% for the same PXD036557 file).
- **Enrichments get dataRepo 0.16.0's four capture values** (`immunoprecipitation`, `proximity_labelling`,
  `affinity_purification`, `chemical_probe`) instead of `other`, read from the whole PRIDE record rather
  than the 40-character evidence snippet. Proximity labelling and chemical probes take priority over
  affinity purification, because both are captured on streptavidin.
- **`DEF-RAZOR-INSTABILITY v1` and `DEF-NONLEADING-ACCESSION v1` are retired.** MetaMorpheus has no leading
  protein; both measured alphabetical order within a group. The replacement is
  `DEF-COMPOSITION-INSTABILITY v1`.
- **GPTMD now looks for the diGly (GG) remnant on lysine** (`search.gptmd_extra_mods`). MetaMorpheus's
  default GPTMD list has no per-protease remnant category, and GG is filed under `Trypsin Digested`, so
  every search before this one could not discover a ubiquitination site. Names are validated against
  the pinned MetaMorpheus's modification files. Datasets searched earlier have no GG sites: that is a
  property of the search, not of the samples.
- **Discovery reads every text field PRIDE gives** (title, project description, both protocols,
  keywords, experiment types, quantification methods), not only the protocols. Metabolic labelling
  (`metabolic_label_patterns`) now counts as `labelled`. Affinity enrichments (`enrichment_patterns`) are
  kept and annotated with an `enrichment` column (`phospho`, `ubiquitin_GG`, `glyco`, `other`), never
  dropped: an organelle-targeted pulldown is an organelle proteome. The screen is `discover.screen()`,
  and each row carries its `screen_evidence`. On one real queue it caught a heavy-water labelling time
  course whose protocols never mentioned the label, plus 15 labelled deposits.
- **`fetch.pick` now defaults to `all`**, and taking fewer files than a deposit lists is always flagged
  `subset_of_deposit` in `provenance.json`, with `raw_files_listed` and `raw_files_chosen`. That
  includes files dropped by `max_file_mb`. The old default, `median_size`, picks files by size, and
  size tracks sample type: on real deposits a median-size window kept 1 of 3 wild-type controls and
  dropped most of one acquisition batch. `median_size` remains for probing.

### Added
- **Per-organism spectral libraries in the search task** (`bin/spectral_library.py`, user request).
  The first search of an organism sets `WriteSpectralLibrary`; every search after it sets
  `UpdateSpectralLibrary` and passes the current library as another `-d`, so the library is both used
  during the search and grown by it. Search task only. Off unless
  `search.spectral_library.enabled` is set.
  A registry (`aging-spectral-library-registry/1`) keeps every version ever written — MetaMorpheus
  names the file with a timestamp and drops it in the task folder, so the path has to be discovered
  and recorded rather than predicted — with each version's parent, SHA-256, spectrum count and
  producing run. `spectral_library.py list` / `rollback` move `current` to an earlier version without
  hand-editing the file, and rolling back appends rather than deleting.
  Refuses: `enabled` without an `organism`; a `current` library missing from disk (falling back to a
  write would silently discard the chain); a registration whose parent moved while the search ran.
  Flags: `search_type` of `Modern` or `NonSpecific`, where `ModernSearchEngine` takes no library and
  the thing is loaded, ignored and updated anyway.

### Fixed
*From the first review pass on this code (2026-09-22). Each of these was a path that only ran when
something had already gone wrong, which is why the test suite was green throughout.*

- **`search.timeout_s` now actually bounds a search.** It never could before: the stage read
  MetaMorpheus's output to end-of-file — which for a subprocess means until it exits — and only then
  called `wait(timeout=...)`, so the timeout sat *after* the only thing that could need timing out. A
  hung search blocked forever with no provenance written. The output is now read on a thread against a
  real deadline, and on expiry the **process tree** is killed: `dotnet CMD.dll` makes the search a
  child of the launcher, so killing the launcher alone left a 32-thread search running.
  **This changes behaviour if you relied on the old effectively-infinite timeout** — a run that
  previously hung now fails cleanly after `search.timeout_s` (default 21600) with
  `timed_out_after_s` in its record.
- **A locked file no longer destroys cleanup's deletion record.** `unlink` was unguarded and
  provenance was written only after the loop, so one undeletable file aborted the stage *after* it had
  already deleted others and the record of what went was never written. The record is now written in a
  `finally`, and files that could not be deleted are listed under `not_deleted` with the reason.
- **Cleanup refuses to overwrite its own record.** A second run over a cleaned directory found nothing
  to delete and replaced the record of what the first removed with `0 files, 0 bytes`. Pass `--force`
  to override. A `--dry-run` record neither blocks nor is blocked.
- **One unreadable file no longer discards a whole dataset's QC.** `read_spectra` was unguarded, so a
  corrupt or truncated `.raw` killed `qc_spectra.py` before `qc_report.json` was written, losing every
  other file's verdict. It is now a per-file verdict, `fail_reasons: ["unreadable"]`.
- **`too_few_ms2` and `unreadable` cannot be waived.** Both this README and the configuration
  reference said `too_few_ms2` must never be waived by an acquisition exception, and **nothing
  enforced it** — a waiver naming it was honoured. The scoping half of that mechanism was implemented
  and tested; the absolute half was prose only. Naming either reason in `waives` is now refused.

### Changed
- `search_mm.py`'s derived metrics are now one reusable function, `derive_metrics`, shared with
  `reprovenance.py` so a metric has one implementation and not two.
- The `low_id_rate` and `high_contamination` flag messages print **two decimals**, and
  `high_contamination` carries the median as well as the worst file. At one decimal, PXD036557's
  corrected 9.98% and its superseded 10.49% both read as "10.x%", which made a definition change look
  like rounding.
- **Provenance schema `aging-provenance/3`.** `id_rate.psms_1pct` is now the target-only summary line of `results.txt` (`aging DEF-PSM-1PCT v1`). It used to be the FDR engine's log line, which is higher; that count is kept as `psms_fdr_engine_1pct`. Contamination now names a definition per share: `aging DEF-CONTAM-PSM v1` for the PSM share and QuantProject `DEF-QC-9 v2` for the intensity share, replacing `aging DEF-CONTAM v1`.

### Added
- **Download retry in `bin/fetch.py`** (`fetch.max_attempts`, `fetch.retry_backoff_s`). EBI drops
  connections on long transfers: a 20 GB, 18-file fetch of PXD027318 died after 7.7 GB with
  *"The response ended prematurely, with at least 334864664 additional bytes expected"*, and because the
  worker raised straight out of `ThreadPoolExecutor.map`, that one drop abandoned the whole stage. Only
  `ServiceUnavailableError` is retried, with linear backoff; anything else still fails immediately. The
  attempt count per file goes into `provenance.json`, and a `download_retried` flag is raised when any
  file needed more than one, because a flaky source is worth following up. **This is a retry, not a
  resume** — a transfer that dies at 90% pays for the whole file again (REQ-PRIDE-1, pride #7c).
- **`bin/reprovenance.py`** — re-derives a finished search's `id_rate`, `mbr`, `contamination` and
  `flags` under today's metric definitions, without re-running the search. A provenance record's
  *history* (commands, tools, hashes, resources) is never touched; only the *interpretations* are
  recomputed, and every run appends a `rederived` entry naming what changed and under which commit.
  Written because the 18-file PXD036557 run carried `aging-provenance/2`, where `id_rate.psms_1pct`
  meant the FDR-engine count — re-searching 18 raw files for 21 minutes to correct a label would have
  been a manual workaround wearing a pipeline's clothes.
- **Contaminant intensity spread** in the provenance block: `intensity_share_median`,
  `intensity_share_min` and `intensity_share_max` beside the per-file map. The dataset total hid a
  2.6%–18.9% range on PXD036557 that tracks the cell line.
- **The predicate behind `aging DEF-PSM-1PCT v1`**, in `docs/provenance.md`: rebuilding the canonical
  PSM count from `AllPSMs.psmtsv` also requires the `Notch` column to be unambiguous, because
  MetaMorpheus counts an unresolved notch q-value while the TSV writer substitutes the best candidate's.
  Documented alongside the peptide and protein-group predicates (the protein-group line counts
  contaminant groups; the PSM and peptide lines do not) and the per-file-FDR caveat.
- A `VERSION` file, and `pipeline.version` in every `provenance.json`, so an output names the release
  that made it as well as the commit.
- The README's "Versioning" section: what is version-controlled, pinned or hashed.
- Tests and CI:
  - an offline suite (every stage, plus a minimal end-to-end pipeline with a fake MetaMorpheus);
  - live canaries for PRIDE and UniProt, which skip on a service outage and fail on a contract break;
  - a GitHub Actions workflow: offline on Linux, Windows and macOS; live on Linux, and weekly.
- Stage 4 runs MetaMorpheus as `dotnet CMD.dll` when `search.metamorpheus_cmd` names the `.dll`, with an
  optional `search.dotnet` host path. That is the form Linux needs. On Windows, a 1-file search gave the
  same results as `CMD.exe`.
- A real MetaMorpheus search test (`tests/test_real_search.py`, marker `metamorpheus`) and a CI job,
  `search`, that runs it on Linux with MetaMorpheus 1.1.11 and two sliced `.raw` files from mzLib's test data.

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

[Unreleased]: https://github.com/trishorts/aging-pipeline/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/trishorts/aging-pipeline/releases/tag/v0.1.0
