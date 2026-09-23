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
- [The definition register](#the-definition-register)
- [Re-deriving an old record](#re-deriving-an-old-record)
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

### Reproducing `psms_1pct` from `AllPSMs.psmtsv`

A reader that wants to rebuild the canonical count from the PSM table needs **four** conditions, not
three — and even then it is an **approximation**, for a reason given under
[the register](#the-definition-register) and measured below:

| Condition | Why |
|---|---|
| `Decoy/Contaminant/Target` is exactly `T` | MetaMorpheus counts `!IsDecoy && !IsContaminant`. An ambiguous value such as `T\|C` means at least one candidate protein is a contaminant, and the PSM does not count |
| `QValue <= 0.01` | |
| `QValue Notch <= 0.01` | |
| **`Notch` is a single value** (no `\|`) | The one that is easy to miss |

Without the last condition the 18-file PXD036557 run gives 26,594 against the 26,582 that
`results.txt` prints. The 12 extra rows are exactly the PSMs whose notch never resolved, and the two
numbers disagree **by construction**, not by accident:

- the count (`FilteredPsms.TargetPsmsAboveThreshold`) reads the PSM's in-memory `QValueNotch`, which
  `SpectralMatch.ResolveAllAmbiguities` leaves at its unresolved value (`> 1`) when the candidate
  hypotheses disagree about it, so the PSM fails the threshold;
- the TSV writer (`PsmTsvWriter.AddMatchScoreData`) handles that same case deliberately — *"ambiguous
  notch, has never been resolved by our disambiguation, so take the best of the notches for the fdr
  columns"* — and writes the **minimum** notch q-value across the hypotheses, so the row looks like it
  passes.

Two related rules for the other two headline numbers in `results.txt`:

- **Peptides** (`All target peptides with q-value <= 0.01`, `aging DEF-PEPTIDE-1PCT v1`): the same
  predicate against `AllPeptides.psmtsv`, at peptide-level FDR, collapsed to one row per full sequence.
  The notch rule does not bite there — the collapse leaves no ambiguous-notch rows.
- **Protein groups** (`aging DEF-PROTEINGROUP-1PCT v1`): the predicate is `Protein QValue <= 0.01` and
  **not decoy**, so contaminant groups *are* counted (1,652 with them, 1,623 without). The PSM and
  peptide lines exclude contaminants; the protein-group line does not.

Per-file lines in `results.txt`, and the tables under `Individual File Results/`, come from a **separate
FDR calculation per file** (`WriteIndividualPsmResults` re-runs `CalculatePsmAndPeptideFdr` on each
file's PSMs), so they are not a partition of the dataset-level count and must not be summed: the 18
per-file lines here total 26,746.

### How close the reproduction actually gets

The four-condition predicate reproduces the canonical PSM count **exactly on the run it was derived
from**, and not on every run. Measured on three datasets, all MetaMorpheus 1.1.11, against the
`results.txt` summary lines:

| Dataset | PSMs (predicate − canonical) | Peptides (predicate − canonical) | Protein groups |
|---|---:|---:|---:|
| PXD036557 (18 files) | **0** | **0** | 0 |
| PXD027318 (18 files) | **−14** | **+2** | 0 |
| PXD032202 (21 files) | **−6** | **−3** | 0 |

So the error is small, but it is **not one-directional**, and a predicate that is short on one dataset
can be over on another. Two separate mechanisms are at work and they pull opposite ways:

- **Deficits** come from the ambiguous-notch family described above: MetaMorpheus counts the
  unresolved in-memory value while the writer deliberately writes the best hypothesis's, so rows that
  the producer rejected look like they pass, and rows it accepted can look like they fail.
- **Overshoots** come from **printing**. `QValue` is written to six decimals, so a true q-value
  anywhere in (0.01, 0.0100005) prints as `0.010000` — it satisfies any file-side `<= 0.01` test while
  the producer's count, reading the unrounded double, rejects it. This can only ever overshoot.

The prediction that follows was checked and held on all three datasets: counting rows that pass the
predicate and print `QValue` as exactly `0.010000` gives **0, 4 and 0** respectively — an overshoot
occurred only where such rows exist, and never exceeded their count.

The general statement, which is not specific to MetaMorpheus:

> **No file-side predicate can reproduce a count whose threshold coincides with a printable value.**
> The writer rounds; the counter does not.

**So `aging:DEF-PSM-1PCT` is defined as the `results.txt` summary line, and the predicate is an
approximation of it.** The canonical number is always the producer's. A consumer that selects rows
with the predicate should expect a small disagreement with the headline and report it rather than
reconcile it away.

All of the above was checked against MetaMorpheus 1.1.11's source and against these runs' output.

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
| `intensity_share_median`, `intensity_share_min`, `intensity_share_max` | The spread across files. **Read these before the dataset total.** On PXD036557 the dataset-level share is 7.0% while the per-file values run **2.6% to 18.9%**, and the high files are one cell line — a single total hides that completely |
| `intensity_share_definition` | `QuantProject DEF-QC-9 v2`. Intensity metrics are QuantProject's; this is their contaminant intensity fraction (PSI MS:4000177) |
| `top` | The five contaminant protein groups with the most summed intensity, named "protein name (organism)". Groups sharing a name and organism are merged |

## The definition register

Every number this pipeline reports carries a **definition ID**, namespaced `<owner>:<ID> v<n>`. The
owner is whoever gets to change the meaning: `aging` for the counts below, `QuantProject` for the
quantitative ones, and a consumer never redefines someone else's number.

**Every definition states its grain**, and that is not decoration. The rule, which a repository
consumer asked us to make explicit after finding it only in prose:

> **A number is stored at the grain at which it was measured. Never coarser, and never finer.**

*Never coarser*, because aggregating destroys structure that cannot be recovered: PXD036557's
dataset-level contaminant intensity share is 7.0%, while its per-file values run 2.6% to 18.9% and the
high ones are a single cell line. Storing only the total hides that completely.

*Never finer*, because splitting invents a number the producer never computed. `results.txt` prints
per-file PSM and peptide lines, and the tables under `Individual File Results/` come from a **separate
FDR calculation per file** — they are not a partition of the dataset-level count and must never be
summed. On the 18-file PXD036557 run they total 26,746 against a dataset figure of 26,582.

It follows that **two grains of one quantity are two definitions**, not one metric queried
differently. A dataset-level contaminant intensity share is not `DEF-QC-9` rolled up; it is a
different quantity, and it would need its own ID and a stated weighting — note that the unweighted
mean of ratios is not the ratio of sums.

| Definition | Grain | What it counts |
|---|---|---|
| `aging:DEF-PSM-1PCT v1` | **dataset** | `results.txt`'s summary line `All target PSMs with q-value <= 0.01`. Target only, contaminants excluded. Reproducing it from `AllPSMs.psmtsv` needs four conditions — see [ID rate](#id-rate) — and that reproduction is an **approximation**, not a definition |
| `aging:DEF-PSM-FDRENGINE v1` | **dataset** | The FDR engine's `PSMs within 1% FDR` log line. Higher, and appears to include contaminants. Recorded for comparison; never reported |
| `aging:DEF-PEPTIDE-1PCT v1` | **dataset** | `All target peptides with q-value <= 0.01`, at peptide-level FDR, one row per full sequence |
| `aging:DEF-PROTEINGROUP-1PCT v1` | **dataset** | `Protein QValue <= 0.01` and not decoy. **Contaminant groups are counted**, unlike the PSM and peptide lines |
| `aging:DEF-MS2 v1` | **run**, summed to dataset for reporting | MS2 scans, from the QC report. Verified equal to MetaMorpheus's own `All MS2 Scans` line |
| `aging:DEF-RUN-MINUTES v1` | **run** | The largest retention time in the file. Never a dataset figure |
| `aging:DEF-PRECURSORS v1` | **run**, summed to dataset for reporting | Precursor envelopes, **not** precursor scans |
| `aging:DEF-CONTAM-PSM v1` | **dataset** | Contaminant PSMs ÷ (target + contaminant) PSMs at q ≤ 0.01, decoys excluded. An ambiguous `C\|T` counts as not-contaminant and stays in the denominator |
| ~~`aging:DEF-NONLEADING-ACCESSION v1`~~ | — | **RETIRED 2026-09-23. It measured alphabetical rank.** MetaMorpheus has no leading, razor or representative protein: parsimony decides a group's members and never ranks them, and the member list is sorted by accession (`mzLib/Omics/BioPolymerGroup/BioPolymerGroup.cs:51-52`). "First in `protein_accessions`" is therefore "sorts first". Its figures (28 of 816, 56 of 3,840, 53 of 1,717) are withdrawn; do not quote them |
| ~~`aging:DEF-RAZOR-INSTABILITY v1`~~ | — | **RETIRED 2026-09-23, for the same reason.** "Leadership changes" meant "alphabetical rank within the group changes because a member sorting ahead joined or left". The 2026-09-21 figures (131 / 50 / 181 of 2,608, **6.9%**) and the worked examples are withdrawn: CALM1 `P0DP23` sorts first in every group it appears in and never changed. Replaced by `DEF-COMPOSITION-INSTABILITY v1`. Filed upstream as MetaMorpheus#2849 (protein groups have no leading protein) |
| `aging:DEF-COMPOSITION-INSTABILITY v1` | **corpus** | Over a named set of datasets, of the target accessions present in **two or more** of them, how many belong to a protein group whose **member set** is not the same in every dataset (compared as sorted member lists). Reported with the sub-count **alone somewhere**: accessions that form a single-member group in at least one dataset and share a group in at least one other. **Always reported as the counts plus the corpus**, never as a bare percentage: the value rises with the number and diversity of datasets. Measured 2026-09-23 on catalog `72449bdd25c36df2`. Three whole proteomes (PXD036557, PXD027318, PXD032202): denominator 2,608, identical 2,462, **varies 146**, alone somewhere 131. All ten datasets, seven of them enrichments (S44): 7,029 / 6,313 / **716** / 631. Worked example: ARF1 `P84077`, alone in PXD027318 and PXD067622, grouped with ARF3 `P61204` in the other eight. After dataRepo's query (their 003 §1.5) |
| `aging:DEF-CONTAM-PSM-RUN v1` | **run** | Contaminant PSMs ÷ (target + contaminant) PSMs at q ≤ 0.01, decoys excluded, **within one file**. An ambiguous `C\|T` counts as not-contaminant and stays in the denominator. This is **not** `DEF-CONTAM-PSM v1` measured per file: that one is a dataset figure, and the register's own rule is that a number is stored at the grain it was measured at. A per-file share is a different quantity and carries a different ID, for the same reason a dataset-level contaminant intensity share is not `DEF-QC-9` rolled up |
| `QuantProject:DEF-QC-9 v2` | **run** | Contaminant ÷ (target + contaminant) protein-group apex intensity, **per file**. The median/min/max this pipeline also records are named as *summaries of the per-run values*, and are not a dataset-level measurement of the same quantity |
| `QuantProject:DEF-QC-MBR v1` | **dataset** | The MBR block's counting rule; its "kept" rule is `DEF-MBR-KEPT v1` (count `QuantProject` kept peaks only — the peaks table is unfiltered) |

**There is no "leading" accession in MetaMorpheus output, so no definition here may use one.** A MetaMorpheus
protein group is an unordered set whose `Protein Accession` string is sorted alphabetically. Any quantity built on
"the first member" measures sort order. Both definitions that did so are retired above. The per-run question
they were circling, *how many groups are ambiguous*, is still valid and is measured on group size, not on position.
The per-corpus question is `DEF-COMPOSITION-INSTABILITY v1`.

**Why `DEF-MS2` and `DEF-PRECURSORS` say "run, summed to dataset"** and `DEF-PSM-1PCT` does not: a scan
count *is* a partition. Every MS2 scan belongs to exactly one file, so the dataset figure is the sum
and nothing is invented. A 1% FDR count is not a partition, because the FDR is recomputed on each
subset. The distinction is the whole rule in one line.

## Re-deriving an old record

A provenance record holds two different kinds of thing, and only one of them may ever be rewritten:

- **What happened** — commands, tool versions and hashes, wall clock, CPU and memory, input and output
  digests. This is history. `reprovenance.py` never touches it, and a test asserts that.
- **What the numbers mean** — `id_rate`, `mbr`, `contamination` and the `flags` derived from them. These
  are interpretations of files still sitting on disk, under definitions that carry version numbers and
  do move.

When a definition is corrected, only the second kind goes stale. Re-running a search over 18 raw files
for 21 minutes to change a label on numbers it would recompute identically is compute spent to avoid
writing a tool, so the pipeline has the tool:

```
python bin/reprovenance.py <params.json> <stage_out_dir> [<spectra_dir>]
```

It recomputes the derived blocks with `search_mm.derive_metrics` — the same function the search stage
itself calls, so there is one implementation and not two — bumps `schema` to the current version, and
**appends a `rederived` entry** naming what changed, from what value to what value, under which pipeline
commit. It never rewrites silently: a provenance file that had been quietly corrected would be worse
than one that was quietly wrong.

```json
"rederived": [{
  "utc": "2026-09-20T...", "tool": "reprovenance.py",
  "from_schema": "aging-provenance/2", "to_schema": "aging-provenance/3",
  "pipeline": {"version": "0.1.0", "commit": "..."},
  "rederived": ["id_rate", "mbr", "contamination", "flags"],
  "changes": {
    "id_rate": {"psms_1pct": {"was": 27958, "now": 26582},
                "rate": {"was": 0.1049, "now": 0.0998}},
    "contamination": "added (absent before)",
    "flags": {"removed": ["low_id_rate: 27958/266402 = 10.5% ..."],
              "added":   ["low_id_rate: 26582/266402 = 9.98% ...",
                          "high_contamination: 18.92% ... 5.50% median across 18 files ..."]}
  }
}]
```

The spectra are recovered from the record's own `inputs`, so the caller need not remember them. If the
raw files have since been deleted, that is fine and the run is still re-derivable — but the entry lists
`spectra_absent`, because a flag that depends on a file beside the spectra (`no_design_file`) cannot be
re-checked without them, and a re-derived record must never look more certain than it is.

It refuses a stage it does not own, and refuses a record whose run did not succeed.

## Automatic flags

Flags mark results to follow up. **A flag never fails a stage.** It says "look at this", and it names
the issue in words.

| Flag | Raised when | What to check |
|---|---|---|
| `low_id_rate` | PSMs at 1% FDR (`aging DEF-PSM-1PCT v1`) ÷ MS2 scans < `search.flag_min_id_rate`. Printed to **two decimals** on purpose: this dataset's canonical rate is 9.98% and the superseded FDR-engine rate was 10.49%, which round to 10.0% and 10.5% and make a definition change look like a rounding wobble | MS2 quality, unassigned charges, unexpected modifications or organisms, a wrong database |
| `calibration_failed` | The MetaMorpheus log reports a calibration failure | GPTMD and search ran on uncalibrated spectra: inspect the file |
| `high_contamination` | Any file's contaminant intensity share > `search.flag_max_contaminant_intensity_share`. The message carries the **worst file and the median**, because the spread is the informative part | `contamination.top`; sample handling; serum in culture media. On PXD036557 the contaminants are bovine serum proteins and the share tracks the cell line |
| `mbr_kept_exceeds_msms` | `mbr_kept` > `msms_peaks` | MBR dominating quantification is implausible: inspect the peaks table |
| `low_core_use` | Average cores < half of `search.max_threads` | I/O limits, thread contention, or an under-parallel task |
| `no_design_file` | No `ExperimentalDesign.tsv` beside the spectra | **Always raised today** (stage 3 is planned). Each file is its own sample with no normalization: don't compare conditions |
| `no_output_sdrf` | No reanalysis SDRF written | **Always raised today** (it waits on MetaMorpheus's `WriteSdrf`) |
