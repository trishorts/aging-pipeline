# Stage reference

Each stage is a standalone Python script in `bin/`. Every script:

- takes the parameters file as its first argument and reads only its own section of it, plus
  `work_root` and `run_date` (stage 4 reads both `search` and `database`);
- writes its outputs into the output directory it is given, together with a `provenance.json` describing
  them and a `resources_timeseries.tsv` of CPU and memory samples (see [provenance](provenance.md));
- is non-interactive, and exits non-zero on failure with a one-line reason on stderr.

The scripts import `bin/provenance.py`, so run them from `bin/` or with `bin/` on `PYTHONPATH`
(invoking `python bin/<stage>.py` does this automatically).

Stages find each other's outputs by **directory convention**, not by arguments. Keep the layout shown
in the [README](../README.md#outputs), or the upstream provenance links and the QC check will not be
found.

---

## Stage 0: `db_prepare.py`, prepare the search database

```
python bin/db_prepare.py <params.json> <out_dir>
```

| | |
|---|---|
| **Reads** | `database.uniprot_xml` (`.xml` or `.xml.gz`) |
| **Writes** | `<out_dir>/<name without .gz>` and `<out_dir>/provenance.json`. It prints the prepared path |
| **Exit codes** | 0 success · non-zero on a read/write error |

**What it does.** It decompresses (or copies) the database once into the pipeline's own work area.
It writes to `<name>.partial` and renames that when complete, so a crash never leaves a half-written
file under the real name. If the prepared file already exists, it is reused and the reuse is noted.

**Why it exists.** Given a gzipped database, MetaMorpheus writes a fixed-name `temp.xml` *beside the
input file*. That writes into whichever folder holds the database, and concurrent searches collide on
it. Searching an uncompressed copy in the work area avoids both problems.

**Keep in step:** `database.prepared` must equal `<out_dir>/<file name without .gz>`, and `<out_dir>`
should be `<work_root>/db`. Stage 4 reads the database from `database.prepared`, and it looks for this
stage's provenance in the same folder.

---

## Stage 1: `discover.py`, find candidate datasets and freeze the list

```
python bin/discover.py <params.json> <out_dir>
```

| | |
|---|---|
| **Reads** | the `discover` section. It needs network access to the PRIDE Archive API |
| **Writes** | `candidates_<run_date>.tsv`, `discover_summary.json`, `provenance.json` |
| **Exit codes** | 0 success · non-zero on a network or API error |

**What it does.**

1. It searches PRIDE once per keyword in `discover.keywords`, and takes the union of the hits.
2. It applies these filters, in order. The **first** failing rule becomes the dataset's `drop_reason`:

   | `drop_reason` | Rule |
   |---|---|
   | `organism` | `discover.organism` is not among the project's organisms |
   | `dia` | The experiment types or the protocol text match a `dia_patterns` entry |
   | `labelled` | The experiment types or the protocol text match a `label_patterns` entry (TMT, iTRAQ, SILAC, …) |
   | `not_thermo` | No instrument matches `thermo_instrument_patterns` |
   | `low_res_instrument` | The only instruments are ion-trap-only (e.g. a plain LTQ) |
   | `no_raw_listed` | The project lists no `.raw` files |
   | `no_sdrf` | Only when `require_sdrf_file` is `true`: there's no SDRF file |

3. It writes **one row per hit, whether kept or dropped**, so the filter can be audited.

**The TSV columns:** `accession`, `keep` (`yes`/`no`), `drop_reason`, `keywords_hit`, `has_sdrf_file`,
`n_raw_listed`, `ms2_class`, `instruments`, `organism_parts`, `experiment_types`, `submission_type`,
`title`.

**`ms2_class`** is a pre-screen, not a verdict:

- `orbitrap_hcd_only`: the instrument can only read MS2 in the Orbitrap (Q Exactive, Exploris).
  **This is only as good as the deposited instrument name**, which PXD060431 showed can name an
  instrument that could not have produced the files. Stage 2b is the check that holds.
- `check_ms2`: a hybrid (Velos, Elite, Fusion, Lumos, Eclipse, …) that *may* read MS2 in the ion trap.
  Stage 2b decides by reading the file.
- `low_res`: dropped.

**Why labelled datasets are found from the protocol text.** PRIDE's quantification field, and even
curated SDRFs, miss labelling. In one dataset the SDRF says "label free" while the protocol describes
TMT. The protocol text is the most reliable signal available before download.

**Why the list is frozen.** PRIDE search is a live index: results drift between runs, and the server
offers no reliable filters. Rerunning this stage is therefore **not** reproducible. The dated TSV is
the record, and every later stage should use only it.

---

## Stage 2: `fetch.py`, download one accession

```
python bin/fetch.py <params.json> <accession> <out_dir>
```

| | |
|---|---|
| **Reads** | the `fetch` section. It needs network access to PRIDE (REST and FTP) |
| **Writes** | `spectra/*.raw`, `metadata/*sdrf*` (if the project has one), `fetch_manifest.json`, `provenance.json` |
| **Upstream** | `<out_dir>/../../01_discover/provenance.json` |
| **Exit codes** | 0 success · non-zero on a network or I/O error |

**What it does.**

1. It lists the project's files from both the PRIDE REST manifest and the FTP inventory. It reports any
   `.raw` file present on FTP but missing from the REST manifest (`raw_missing_from_rest_manifest`), which
   is a known PRIDE inconsistency.
2. It drops files larger than `max_file_mb`.
3. It chooses files according to `pick`:
   - `median_size` (the default): `max_files` files centred on the median size. The smallest file is
     often a blank or a failed run, so it isn't chosen unless it's the only candidate left.
   - `first_by_name`: the first `max_files` files by name.
   - `all`: every remaining file.

   Any other value is rejected before anything is downloaded.
4. It downloads the chosen files, `parallel_downloads` at a time, plus every SDRF file.
5. It records each file's PRIDE size and checksum (when PRIDE has one), its local size and its SHA-256.

**Safe to rerun.** Downloads go to `<name>.partial` and are renamed only when complete. On a rerun,
complete files are skipped.

**Not handled yet:**
- a failed transfer restarts from byte 0 (there's no resume);
- `fetch.extension` changes what is downloaded, but stages 2b, 4 and 9 look only for `*.raw`, so in
  practice it must stay `.raw`;
- PRIDE usually supplies no checksum, and when that happens a note says integrity rests on the local
  SHA-256 alone.

The SDRF is downloaded but **not trusted**: many are empty skeletons, and some are wrong.

---

## Stage 2b: `qc_spectra.py`, admit only high-resolution Orbitrap HCD MS2

```
python bin/qc_spectra.py <params.json> <spectra_dir> <out_dir>
```

| | |
|---|---|
| **Reads** | the `qc` section; `<spectra_dir>/*.raw` (scan headers only, through mzLib) |
| **Writes** | `qc_report.json`, `provenance.json` |
| **Upstream** | `<spectra_dir>/../provenance.json` (stage 2) |
| **Exit codes** | **0** every file passed · **2** at least one file failed (or there were no files) |

**The rule.** A file passes when **both** of these hold:
- at least `min_fraction_orbitrap_hcd` of its MS2 scans are HCD with an Orbitrap analyzer;
- it has at least `min_ms2` MS2 scans.

**Per file, the report gives:** `pass`, `fail_reasons`, `scans`, `ms2`, `fraction_orbitrap_hcd`,
`ms2_analyzer_dissociation` (e.g. `{"Orbitrap/HCD": 14790}`), `run_minutes`, and the six commonest
precursor `charge_states`.

`fail_reasons` names each failed condition separately — `low_res_ms2`, `too_few_ms2` — and is empty
for a passing file. The two are kept apart so that a `qc.acquisition_exception` can waive one
without waiving the other (see [configuration](configuration.md#acquisition_exception)).

**Why.** An instrument's name doesn't say where MS2 was read — and it can be simply wrong. Hybrids can
use the ion trap with CID, Tribrids can fragment with HCD and read the fragments out in the ion trap,
and low-resolution MS2 would change the search's behaviour silently. The MS2 count and run length also
catch blanks, and datasets whose description doesn't match their files. Stage 4 **refuses to search**
a dataset whose QC report has any failing file.

This is the stage that earns its keep on metadata nobody could have checked earlier. PXD060431's
deposited SDRF names an *Orbitrap Exploris 480* — an instrument with no ion trap, and therefore one
that discovery classifies as `orbitrap_hcd_only` and never re-examines. All 30 of its files turned out
to be `ITMS + c NSI r d Full ms2 <mz>@hcd33.00`: HCD fragmentation read out in the ion trap, with
`fraction_orbitrap_hcd` exactly `0.0000`. Only reading the files caught it.

The one way past this gate is a `qc.acquisition_exception`, which must name the conditions it waives
and records the restriction in `provenance.json` and in `flags`.

---

## Stage 4: `search_mm.py`, MetaMorpheus search and quantification

```
python bin/search_mm.py <params.json> <spectra_dir_or_file> <out_dir>
```

| | |
|---|---|
| **Reads** | the `search` and `database` sections; the spectra; `<out_dir>/../02b_qc/qc_report.json` |
| **Writes** | `tasks/`, `mm/`, `metamorpheus.log`, `resources_timeseries.tsv`, `provenance.json` |
| **Upstream** | stage 2b, stage 2 and stage 0 provenance |
| **Exit codes** | **0** success · **1** a precondition failed, or the search did not succeed (see below) |

**It refuses to start when:**
- `database.prepared` doesn't exist (run stage 0);
- `<out_dir>/mm` already exists. MetaMorpheus never cleans an existing output folder, so every run gets
  a fresh one;
- the running MetaMorpheus reports a release other than `search.metamorpheus_version`;
- there's no QC report, or a file in it failed for a reason that no `qc.acquisition_exception`
  waives (a report written before `fail_reasons` existed has no waivable reason, so it refuses);
- a `search.product_mass_tolerance` or `search.precursor_mass_tolerance` override is set but its
  key is not found exactly once in a generated task TOML — silently changing nothing would be worse;
- `search.exclude_files` names a file that is not in the spectra directory, or excludes all of them;
- `search.search_type` is not one of `Classic`, `Modern`, `NonSpecific`, or its line is not found
  exactly once in the generated search TOML;
- contaminants are on but MetaMorpheus's shipped `Contaminants/MetaMorpheusContaminants.xml` is missing.

**What it does.**

0. **It picks the launcher.** A `metamorpheus_cmd` ending in `.dll` runs as `<dotnet> CMD.dll …` (the Linux
   form); anything else runs directly. Every call below uses that launcher, and the provenance records it.
1. **It generates the default task settings on this machine** (`CMD -g`). It then changes only what the
   parameters name: `MaxThreadsToUsePerFile` in every task, and `MatchBetweenRuns` in the search task. The
   generated defaults and the edited copies that run (`1_…`, `2_…`, `3_…`) are both kept in `tasks/`, and
   the edited copies are hashed into the provenance. Defaults come from the running
   release, never from a stale copy.
2. **It records the tool's identity:** the release (from the `-g` banner), the build commit (from
   `--help`), and the SHA-256 of `CMD.dll`. This MetaMorpheus CLI's `--version` prints help text
   instead of a version.
3. **It runs calibration → GPTMD → search in one invocation.** MetaMorpheus chains the tasks, passing
   calibrated spectra and the GPTMD-augmented database forward. The databases passed are the prepared
   proteome **plus the shipped contaminant database**, unless `database.include_contaminants` is `false`.
   MetaMorpheus's settings folder is `<work_root>/mm_settings/<release>/`. The script deliberately doesn't
   create it, because MetaMorpheus crashes on an empty pre-created settings folder.
4. **It times each task.** Every log line is stamped with elapsed seconds, and each task's wall time, CPU,
   average cores and peak memory are cut from the resource time series (`per_task_resources`).

**Timeout.** `search.timeout_s` is **not enforced** yet. A hung MetaMorpheus process isn't killed, so
under Nextflow, set a process `time` limit.

**Success** requires exit code 0 **and** all four result tables in the search task folder:
`AllPSMs.psmtsv`, `AllPeptides.psmtsv`, `AllQuantifiedPeptides.tsv`, `AllQuantifiedProteinGroups.tsv`.
FlashLFQ can fail while MetaMorpheus still exits 0, so exit code 0 alone isn't enough. A note in the
provenance names that case.

**Measurements it adds to the provenance:**
- `id_rate`: target PSMs at 1% FDR (the summary line of `results.txt`) over the MS2 count from the QC
  report ([definition](provenance.md#id-rate)).
- `mbr`: match-between-runs counts ([how they are counted](provenance.md#match-between-runs)).
- `contamination`: the share of contaminant PSMs and intensity ([definition](provenance.md#contamination)).
- `flags`: [automatic flags](provenance.md#automatic-flags) for follow-up.

**Settings that are not changed from MetaMorpheus's defaults** (as of this release): protease (trypsin),
missed cleavages, fixed and variable modifications, tolerances, the GPTMD modification list, and
FDR thresholds. They are recorded exactly in `tasks/*.toml` and `mm/Task Settings/`.

---

### The spectral library

**One library per organism, written by the first search and updated by every search after it.** Off
unless `search.spectral_library.enabled` is true; see
[configuration](configuration.md#spectral_library-stage-4). It applies to the **search task only** —
calibration and GPTMD never write or update one, and a test asserts their task files never acquire
the settings.

```
first search of an organism     WriteSpectralLibrary = true    (nothing to consume yet)
every search after that         UpdateSpectralLibrary = true   + the current library passed with -d
```

**Never both.** `PostSearchAnalysisTask` acts on the two booleans in two independent `if` blocks, so
setting both writes two libraries.

**The library is used during the search, not merely produced by it** — `ClassicSearchEngine` takes it
as an argument. It is supplied as another `-d` database, because `DbForTask` decides a database is a
library by extension alone (`.msp` or `.msl`); one `-d` covers the whole chain, since the GPTMD task
forwards a library in its `NewDatabases`. `EverythingRunnerEngine` refuses a run whose databases are
*all* libraries, so the protein database is still required — which it always is here.

**An update merges, so a library only grows.** For each (full sequence, charge) MetaMorpheus keeps
whichever has more evidence — the library's spectrum when its matched-ion count exceeds the new PSM's
truncated score, otherwise the new PSM — then adds every (sequence, charge) the library did not have.
A later search cannot silently delete a peptide an earlier one contributed.

**Why there is a registry.** MetaMorpheus writes the library into the *task's own folder* under a
**timestamped** name — `SpectralLibrary_<time>.msp` for a write, `updateSpectralLibrary_<time>.msp`
for an update. The path cannot be predicted, so the stage discovers it after the run, copies it into
the library root under a stable name, and records it:

```
<root>/registry.json                  aging-spectral-library-registry/1
<root>/human/human.v001.msp           every version ever written, never overwritten
<root>/human/human.v002.msp
```

`organisms.<organism>.current` is the version the next search will consume;
`organisms.<organism>.versions[]` is the full chain, each entry carrying its parent version, its
SHA-256, its spectrum count, and the run and accession that produced it. The copy leaves
MetaMorpheus's original in the run folder, so the run stays a faithful record and the two can be
checked against each other by hash.

**Going back.** Point `current` at an earlier version:

```
python bin/spectral_library.py list     params.json
python bin/spectral_library.py rollback params.json human 1
```

Rolling back removes nothing. The next search of that organism updates *from* the version you chose
and appends a new one, so the chain records the decision instead of hiding it.

**Four refusals and a flag**, because each of these is otherwise silent:

| situation | behaviour |
|---|---|
| `enabled` with no `organism` | **refused.** The key is never derived from `discover.organism`: that is a PRIDE facet string, and string-munging it into a library key is how mouse spectra end up in the human library |
| `current` names a file that is not on disk | **refused.** Falling back to a write would start a second chain and discard everything the first accumulated — visible only as a library that got *smaller* |
| another search registered while this one ran | **refused at registration.** The run's library is left in the run folder, unregistered, so nothing is lost and the merge is deliberate rather than a race |
| the search did not succeed | not registered. A partial library must not become the next one's parent |
| `search_type` is `Modern` or `NonSpecific` | **flagged.** `ModernSearchEngine` takes no library at all, so it is loaded, never consulted, and still updated afterwards — it would grow without ever having contributed an identification |
| configured but no `.msp` was produced | **flagged**, and the library does not advance. The search itself still stands |

`provenance.json` carries a `spectral_library` block: the organism, the mode, the library consumed,
the parent version, the registry path, and — on success — the version record including the spectrum
count and the name MetaMorpheus actually used.

## Stage 5: `qc_payload.py`, build the QC payload

| | |
|---|---|
| **Runs** | after stage 4 |
| **Reads** | `<search_dir>/mm/Task*SearchTask/` (`results.txt`, `AllPSMs.psmtsv`, `AllQuantifiedPeaks.tsv`, `AllQuantifiedProteinGroups.tsv`), `Task1CalibrationTask/*-calib.toml`, and `<qc_dir>/qc_report.json` |
| **Writes** | `qc_payload.json`, `provenance.json` |
| **Usage** | `qc_payload.py <params.json> <search_dir> <qc_dir> <out_dir> [accession]` |

The `qc` project owns the QC templates and their contract (`qc-payload/1`); this stage only supplies
the numbers. The payload is then rendered by qc's own tool:

```
python -m qctemplates validate <out_dir>/qc_payload.json
python -m qctemplates render   <out_dir>/qc_payload.json <out_dir>
```

**We supply values, never verdicts.** Gates, outlier detection and the derived ratios (`id_rate`,
`mbr_msms_ratio`) are qc's to compute, and the contract says so. Supplying them here would put one
rule in two places and let the two drift. Every value that has a written definition carries its ID, so
a template renders a number it did not define and can still say where the meaning came from.

**Two shapes in the contract drive the code.** `pg_missing_frac` is a per-file metric that needs the
*dataset* first — the share of the dataset's quantified protein groups absent from this file — so the
build is two-pass. And `id_rt_coverage` divides by run minutes, which is a stage-2b fact rather than a
search fact, which is why the stage takes both directories.

**`pg_missing_frac` is `DEF-QC-13`'s `_msms` variant**, where a group counts as present in a file when
its `SpectralCount_` is above zero — never the `_any` variant, which asks whether the file has an
*intensity*. Match-between-runs is on in every run this pipeline produces, so an intensity can be
transferred from a neighbouring file: under `_any`, one file's completeness would be a function of the
*other* files in the run, and a number like that is not a property of the file it is filed under.
`mbr_kept` is where the transfer contribution is visible instead. The payload states the variant in
`dataset.notes`, because the metric's name does not.

**When a run carries no `SpectralCount_` columns, `pg_missing_frac` is omitted rather than set to
zero.** A sample group's column block in `AllQuantifiedProteinGroups.tsv` is two, three or four
columns wide depending on what the search wrote, so their absence is a real case. qc's rule is that an
absent value means *not measured* while `0` means *measured, and it was zero*; reporting zero
completeness nobody measured would trip gates and enter medians as if it were an observation.

**The protein-group table is read with a filter, and the filter is load-bearing.**
`AllQuantifiedProteinGroups.tsv` is written *unfiltered* — decoys, contaminants and rows above 1% FDR
are all in it — so the stage applies `DEF-PROTEINGROUP-1PCT` (not decoy, `Protein QValue ≤ 0.01`,
contaminants **included**). On the 18-file PXD036557 run that is 1,652 groups out of 2,229 rows; a
denominator of "rows in the file" would be wrong by about a third.

**Contamination (M13).** `contaminant_intensity_frac` is `QuantProject:DEF-QC-9 v2` — contaminant over
target-plus-contaminant apex intensity, per file. `contaminant_psm_share` is
**`aging:DEF-CONTAM-PSM-RUN v1`**, a run-grain definition, and deliberately *not* `DEF-CONTAM-PSM v1`,
which this project defines at dataset grain: a per-file share is a different quantity, not the dataset
one pushed down. A not-quantified protein intensity cell is **blank**, not `0`, at MetaMorpheus 1.1.9
and later, so a blank contributes nothing to either side of the ratio rather than reading as a zero.

**Histogram bins are qc's rule, not our copy of their numbers.** The stage calls
`qctemplates.spec.canonical_edges(key, run_minutes)` when `qctemplates` is installed. It is an
**optional** dependency: when it is absent the stage falls back to a vendored copy of the same edges,
so the pipeline still runs for an operator who does not have it, and the payload still validates and
renders. The fallback announces itself — in `dataset.notes` and as `bin_edges_source` in
`provenance.json` — because a payload binned by a stale copy would otherwise stop lining up with
everyone else's figures silently. Install it with `pip install -e <path to qc>`; there is no public
package index for it yet.

**The accession** comes from `fetch.accession`, or from the optional fifth argument for older params
files that leave it null and pass it on the command line as `fetch.py` does. A payload that cannot be
named is refused rather than written with a null, because qc's schema requires a string and an
unnamed payload is useless the moment two datasets exist.

**An acquisition exception travels with the numbers.** If `qc.acquisition_exception` is set
(see [configuration](configuration.md#acquisition_exception)), its restriction is written into the
payload's `dataset.notes`, so a rendered report states what its numbers may not be used for.

### Parsing rules worth knowing

- **File keys** drop the extension *and* MetaMorpheus's `-calib` suffix, so `results.txt`'s
  `QE-002106_GM1_a-calib` and the file `QE-002106_GM1_a-calib.toml` land on the same key. `.toml`
  counts as an extension here: leaving it on makes `calibration_ok` false for a run that calibrated
  perfectly well.
- **Ambiguous cells are dropped, not guessed.** MetaMorpheus writes `a|b` when a value is
  unresolved; there is no single number for those.
- **Mass-error metrics use `Notch 0` only**, per the contract — an isotope-error PSM's precursor error
  is offset by a neutron and would smear the distribution that exists to show calibration.
- **`msms_peaks` and `mbr_kept` use the same `classify_peak` function as stage 4**, so QuantProject's
  `DEF-MBR-KEPT v1` has one implementation. A file whose every MBR candidate was rejected gets
  `mbr_kept: 0`, not an absent value — qc renders absent as a dash, and "no transfers survived" is a
  different fact from "not measured".
- **The per-file counts in `results.txt` come from a separate per-file FDR calculation** and are not a
  partition of the dataset totals. They must never be summed into one (see
  [the definition register](provenance.md#the-definition-register)).

### Known limitation

`qctemplates` currently ships no installable package metadata, so `python -m qctemplates` runs only
from a checkout of the `qc` project rather than from anywhere on `PATH`. Building and validating a
payload is unaffected; chaining the *render* into an automated run is not yet possible without
pointing at that checkout.

---

## Stage 9: `cleanup.py`, delete re-obtainable raw spectra

```
python bin/cleanup.py <params.json> <dataset_run_dir> [--dry-run]
```

`<dataset_run_dir>` is `<work_root>/run_<run_date>/<accession>`.

| | |
|---|---|
| **Deletes** | `02_fetch/spectra/*.raw`; `04_search/mm/**/*-calib.mzML`; `04_search/mm/Task*/*.raw` (copies MetaMorpheus makes when calibration fails) |
| **Keeps** | all results, the GPTMD database, logs, and every provenance record |
| **Writes** | `09_cleanup/provenance.json`: every deleted file's **absolute** path, its size, and its SHA-256 at fetch time |
| **Exit codes** | 0 done · 1 refused, because `04_search/provenance.json` is missing or not successful |

**Run it with `--dry-run` first.** A dry run lists the files and sizes without deleting anything.

**Why deleting is safe.** PRIDE is the source of truth, and stage 2's record keeps each file's name,
size and SHA-256, so the exact inputs can be fetched again and verified. A hard-linked copy of a `.raw`
elsewhere frees disk only when its last link is deleted.

**Deletion is never automatic.** Neither `run_local.ps1` nor `main.nf` runs this stage. It's an explicit,
deliberate step.

---

## `run_local.ps1`: Windows runner for one accession

```powershell
powershell -NoProfile -File run_local.ps1 -Accession <PXD…> [-Python <python.exe>] [-Params <params.json>]
```

It runs stages 0, 1, 2, 2b and 4 in order, using the directory layout above. It stops at the first stage
that exits non-zero and returns that stage's exit code (e.g. 2 when a file fails spectra QC).
`-Params` defaults to the `params.json` beside the script. **Always pass `-Python`:** its default is the
developer's own environment.

## `main.nf`: Nextflow (untested)

The intended production entry point. It writes Nextflow's trace, report and timeline under
`<outdir>/pipeline_info/` (`nextflow.config`), and retries each process up to twice. **It has not been
run.** It lacks stages 0, 2b and 9, so `SEARCH_MM` would stop at its QC check. See the
[README's limitations](../README.md#status-and-known-limitations).
