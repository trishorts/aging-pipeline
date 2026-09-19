# Provenance records

Every stage writes a `provenance.json` beside its outputs. It's part of the output contract, not a
debug log. It answers four questions about any result: *what produced it, from what, with which
settings, and at what cost?* The schema identifier is `aging-provenance/3`.

## Contents

- [Common fields](#common-fields)
- [File entries](#file-entries)
- [Chaining stages](#chaining-stages)
- [Stage-specific fields](#stage-specific-fields)
- [Resources](#resources)
- [ID rate](#id-rate)
- [Match-between-runs](#match-between-runs)
- [Contamination](#contamination)
- [Automatic flags](#automatic-flags)

## Common fields

| Field | Meaning |
|---|---|
| `schema` | `"aging-provenance/3"` |
| `stage` | `db_prepare`, `discover`, `fetch`, `qc_spectra`, `search_metamorpheus` or `cleanup` |
| `started_utc`, `finished_utc` | ISO 8601 timestamps |
| `host` | `node` (hostname), `os`, `python` |
| `pipeline` | `version` (from the `VERSION` file), `repo` (the clone's `origin` URL) and `commit`. A `+dirty` suffix means something in the pipeline folder (scripts, `main.nf`, parameter files, docs) had uncommitted changes, so the commit alone doesn't reproduce the run. `"unknown"` outside a git clone |
| `params_file` | The parameters file as a [file entry](#file-entries), including its SHA-256 |
| `params` | This stage's main section of the parameters, copied verbatim (for stage 4, `search`: the `database` section is covered by `params_file`'s hash and by the database file entries) |
| `run_date` | From the parameters |
| `roots` | `{"work_root": …}`: the base for relative paths |
| `tools` | Each tool and its version. `pymzlib`: package version and the bridge/mzLib versions (the mzLib build commit included). `MetaMorpheus`: `release`, build `commit`, `expected_release`, `cmd` path and `cmd_dll_sha256` |
| `commands` | The commands and library calls that do the stage's work, as argument lists. Stage 4's `--help` probe (used only to read the build commit) isn't listed |
| `upstream` | The provenance records this stage consumed ([chaining](#chaining-stages)) |
| `inputs`, `outputs` | [File entries](#file-entries) |
| `notes` | Plain-language facts a reader needs, e.g. "PRIDE supplied no checksum…", "already prepared; reused" |
| `resources` | Compute and memory ([below](#resources)) |
| `flags` | Automatic follow-up flags, where the stage emits any ([below](#automatic-flags)) |

## File entries

An illustrative entry for a fetched file (the values are made up):

```json
{"path": "run_2026-09-18/PXD036557/02_fetch/spectra/GM2_a.raw", "root": "work_root",
 "size_bytes": 1043215360, "sha256": "…", "sha256_from": "fetch"}
```

- **Portable paths.** A file under `work_root` is stored relative to it, with `"root": "work_root"`,
  so a record reads the same after the tree moves to another machine. Tools and parameter files outside
  the root keep absolute paths.
- **Hashes are always present.** Outputs are always hashed fresh. For inputs, a hash already computed
  by an upstream stage is reused rather than re-reading multi-gigabyte `.raw` files. `sha256_from` names
  the stage that computed it. A hash is reused only when the resolved path and size match, or, for files
  of 100 MB or more, when the name and size match (a hard link elsewhere).

## Chaining stages

`upstream` lists `{stage, path, sha256}` for each provenance record the stage consumed. From any
search result, you can walk back search → spectra QC → fetch → discover, and search → database
preparation, checking each link by hash. When an expected upstream record is missing, the stage notes
it and continues.

| Stage | Looks for upstream provenance at |
|---|---|
| fetch | `<out_dir>/../../01_discover/` |
| qc_spectra | `<spectra_dir>/../` (the fetch folder) |
| search_metamorpheus | `<out_dir>/../02b_qc/`, the fetch folder, and the folder holding `database.prepared` |
| cleanup | `04_search/` and `02_fetch/` |

## Stage-specific fields

| Stage | Field | Meaning |
|---|---|---|
| fetch | `accession` | The accession fetched |
| fetch | `download_seconds` | Wall time per file |
| fetch | `parallel_downloads` | The concurrency used |
| qc_spectra | `all_pass` | `true` only if every file passed (and there was at least one) |
| search_metamorpheus | `exit_code` | MetaMorpheus's exit code |
| search_metamorpheus | `success` | Exit code 0 **and** all four result tables exist |
| search_metamorpheus | `per_task_resources` | Per MetaMorpheus task: `wall_s`, `cpu_s`, `avg_cores_used`, `peak_rss_gib`, `peak_threads`, `samples` (the number of 1-s samples in the task's window) |
| search_metamorpheus | `id_rate` | [ID rate](#id-rate): `psms_1pct`, `ms2` (from the QC report), `rate`, `psms_fdr_engine_1pct`, and their definitions |
| search_metamorpheus | `mbr` | [Match-between-runs counts](#match-between-runs) |
| search_metamorpheus | `contamination` | [Contaminant shares](#contamination) |
| search_metamorpheus | `expected_cores` | `search.max_threads`, used by the `low_core_use` flag |
| cleanup | `deleted` | `{path, size_bytes, sha256_at_fetch}` per file. These paths are **absolute**, unlike file entries |
| cleanup | `dry_run`, `bytes_freed` | What happened, or what would have happened |

## Resources

Collaborators ask what a reanalysis costs, so every stage measures itself. A background thread
samples **this process and all its descendants** (e.g. the MetaMorpheus process) once per second.

| Field | Meaning |
|---|---|
| `wall_s` | Elapsed time |
| `cpu_user_s`, `cpu_system_s` | CPU time summed over the process tree |
| `avg_cores_used` | (user + system) ÷ wall |
| `peak_rss_bytes`, `peak_rss_gib` | Peak resident memory of the whole process tree |
| `peak_threads`, `peak_processes` | Peak thread count and process count |
| `host_disk_read_bytes`, `host_disk_write_bytes` | Disk I/O over the stage. **Host-wide**, so other activity on the machine is included |
| `host` | Logical and physical CPUs, and total RAM |
| `output_bytes` | The total size of the stage's outputs |
| `sampling` | The interval, the number of samples, and the scope |
| `timeseries_file` | `resources_timeseries.tsv`: `t_s`, `cpu_s_cumulative`, `rss_bytes`, `threads` per sample |

A child process that exits between samples loses at most one interval of CPU time, which is negligible
for stages that run minutes to hours. Without `psutil`, `resources` holds only `wall_s`, `output_bytes` and a `note` saying so. Under Nextflow,
`-with-trace` records the same quantities per process, and the two should agree.

## ID rate

MetaMorpheus's `results.txt` prints two different PSM counts at 1% FDR, and the gap is large enough to
matter: 26,582 against 27,958 in an 18-file run of PXD036557, and 78 against 99 on the CI test files. The
provenance keeps both, each with its own definition:

| Field | Meaning |
|---|---|
| `psms_1pct` | **The canonical count.** The summary line `All target PSMs with q-value <= 0.01`: target PSMs only |
| `definition` | `aging DEF-PSM-1PCT v1` |
| `ms2` | MS2 scans across all files, from the QC report |
| `rate` | `psms_1pct ÷ ms2`. The `low_id_rate` flag uses it |
| `psms_fdr_engine_1pct` | The FDR engine's log line `PSMs within 1% FDR` (its first occurrence). It is higher, and it appears to include contaminant PSMs. Report `psms_1pct`, not this |
| `psms_fdr_engine_definition` | `aging DEF-PSM-FDRENGINE v1` |

## Match-between-runs

`AllQuantifiedPeaks.tsv` is written **unfiltered**: it contains every candidate match-between-runs
(MBR) peak, including ones FlashLFQ rejected. Counting its MBR rows overstates MBR. The provenance
therefore separates:

| Field | Meaning |
|---|---|
| `msms_peaks` | Peaks with `Peak Detection Type = MSMS` |
| `mbr_rows` | All rows with `Peak Detection Type = MBR` (unfiltered) |
| `mbr_random_rt_won` | MBR rows where the random-retention-time decoy won (`Random RT = True`) |
| `mbr_kept` | MBR rows with `PIP Q-Value` < `mbr_fdr_threshold`, not a random-RT win, and not a decoy peptide |
| `kept_over_msms` | `mbr_kept ÷ msms_peaks`: the headline ratio |
| `mbr_fdr_threshold` | 0.01, MetaMorpheus's default (it isn't yet read from the task settings) |
| `definition` | The ID of the counting definition (`DEF-QC-MBR v1`; its "kept" rule is `DEF-MBR-KEPT v1`) |

Use `mbr_kept`, never `mbr_rows`, when you report how much quantification came from MBR.

## Contamination

This block is written only when the contaminant database was searched (the default). A row counts as a
contaminant only when its `Decoy/Contaminant/Target` value (for PSMs) or `Protein Decoy/Contaminant/Target`
value (for protein groups) is exactly `C`. Ambiguous values such as `C|T` are handled differently in the
two shares:

- **PSM share:** an ambiguous PSM counts as not-contaminant, and it stays in the denominator.
- **Intensity share:** an ambiguous protein group is left out of both the numerator and the denominator.
  Only `T` and `C` groups are summed.

| Field | Meaning |
|---|---|
| `psm_share` | Contaminant PSMs ÷ (target + contaminant) PSMs, at q ≤ 0.01, decoys excluded |
| `psm_share_definition` | `aging DEF-CONTAM-PSM v1` |
| `contaminant_psms`, `target_plus_contaminant_psms` | The two counts behind `psm_share` |
| `intensity_share_per_file` | Per file: contaminant ÷ (target + contaminant) protein-group intensity (apex intensity, from `AllQuantifiedProteinGroups.tsv`) |
| `intensity_share_definition` | `QuantProject DEF-QC-9 v2`. Intensity metrics are QuantProject's; this is their contaminant intensity fraction (PSI MS:4000177) |
| `top` | The five contaminant protein groups with the most summed intensity, named "protein name (organism)". Groups sharing a name and organism are merged |

## Automatic flags

Flags mark results to follow up. **A flag never fails a stage.** It says "look at this", and it names
the issue in words.

| Flag | Raised when | What to check |
|---|---|---|
| `low_id_rate` | PSMs at 1% FDR ÷ MS2 scans < `search.flag_min_id_rate` | MS2 quality, unassigned charges, unexpected modifications or organisms, a wrong database |
| `calibration_failed` | The MetaMorpheus log reports a calibration failure | GPTMD and search ran on uncalibrated spectra: inspect the file |
| `high_contamination` | Any file's contaminant intensity share > `search.flag_max_contaminant_intensity_share` | `contamination.top`; sample handling; serum in culture media |
| `mbr_kept_exceeds_msms` | `mbr_kept` > `msms_peaks` | MBR dominating quantification is implausible: inspect the peaks table |
| `low_core_use` | Average cores < half of `search.max_threads` | I/O limits, thread contention, or an under-parallel task |
| `no_design_file` | No `ExperimentalDesign.tsv` beside the spectra | **Always raised today** (stage 3 is planned). Each file is its own sample with no normalization: don't compare conditions |
| `no_output_sdrf` | No reanalysis SDRF written | **Always raised today** (it waits on MetaMorpheus's `WriteSdrf`) |
