# aging-pipeline

[![CI](https://github.com/trishorts/aging-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/trishorts/aging-pipeline/actions/workflows/ci.yml)

**This pipeline reanalyses public aging proteomics data at the level of proteoforms and PTMs.** It
finds aging datasets in PRIDE, downloads them, checks the spectra, and searches and quantifies them
with [MetaMorpheus](https://github.com/smith-chem-wisc/MetaMorpheus). Every output carries a
provenance record, so any number can be traced to the exact files, tools and settings that made it.

It is the pipeline deliverable of an NCEMS working group on aging, whose question is: *do organelles
age at different rates, and can proteoform- and PTM-level measurements see aging signal that bulk
protein abundance misses?* It is built at the Smith lab, University of Wisconsin–Madison.

> **Status: working prototype (v0.1).** Stages 0, 1, 2, 2b, 4 and 9 run end to end on Windows. They
> have searched all 18 files of PXD036557 with MetaMorpheus 1.1.11. The Nextflow wiring
> (`main.nf`) has **not** been run yet. Stages 3, 5, 6 and 7 are planned. See
> [Status and known limitations](#status-and-known-limitations) before you rely on anything here.

## Contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Outputs](#outputs)
- [Design principles](#design-principles)
- [Status and known limitations](#status-and-known-limitations)
- [Testing](#testing)
- [Versioning](#versioning)
- [Internal references](#internal-references)
- [Repository layout](#repository-layout)
- [Citing](#citing) · [License](#license) · [Contact](#contact)

Detailed documentation:

| Document | Covers |
|---|---|
| [`docs/stages.md`](docs/stages.md) | Each stage: purpose, command line, inputs, outputs, exit codes, and the rules it enforces |
| [`docs/configuration.md`](docs/configuration.md) | Every key in `params.json`, with its default and its effect |
| [`docs/provenance.md`](docs/provenance.md) | The `provenance.json` schema (`aging-provenance/3`), resource accounting, and the automatic QC flags |
| [`CHANGELOG.md`](CHANGELOG.md) | What changed, and when |

## What it does

| # | Stage | Script | State |
|---|---|---|---|
| 0 | Prepare the search database once (decompress, record hashes) | `bin/db_prepare.py` | ✅ runs |
| 1 | **Discover** aging datasets in PRIDE, filter them, and freeze a dated candidate list | `bin/discover.py` | ✅ runs |
| 2 | **Fetch** the raw spectra and any SDRF for one accession | `bin/fetch.py` | ✅ runs |
| 2b | **Spectra QC.** Admit only high-resolution HCD MS2 read in the Orbitrap | `bin/qc_spectra.py` | ✅ runs |
| 3 | Experimental design from SDRF | — | ⏳ planned; runs are flagged `no_design_file` until then |
| 4 | **Search and quantify:** MetaMorpheus calibration → GPTMD → search + FlashLFQ, in one invocation | `bin/search_mm.py` | ✅ runs |
| 5 | Reanalysis SDRF and deposition bundle | — | ⏳ waits on MetaMorpheus `WriteSdrf` (unreleased) |
| 6 | Map proteins to GO terms and subcellular compartments | — | ⏳ planned |
| 7 | Cross-dataset age effects per protein, PTM site and organelle | — | ⏳ planned |
| 9 | **Cleanup:** delete the re-obtainable raw spectra after a successful search | `bin/cleanup.py` | ✅ runs (only on explicit request) |

Numbering leaves room for the planned stages. Search is still stage 4 even while stage 3 doesn't exist.

**Why MetaMorpheus.** GPTMD (global PTM discovery) finds a broad range of modifications in ordinary,
unenriched datasets: acetylation, phosphorylation, methylation, deamidation, ubiquitin remnants and
many more. It does this without a PTM-specific enrichment experiment
([Solntsev *et al.* 2018](https://doi.org/10.1021/acs.jproteome.7b00873)). FlashLFQ supplies label-free
quantification with match-between-runs
([Millikin *et al.* 2018](https://doi.org/10.1021/acs.jproteome.7b00608)). Proteoforms are *inferred*
from bottom-up evidence: modified peptides, PTM stoichiometry and isoform-specific peptides. The pipeline
does not do top-down proteomics.

## Requirements

| Requirement | Version | Why |
|---|---|---|
| Python | 3.11+ (developed on 3.13) | The stage scripts |
| [`mzlib`](https://pypi.org/project/mzlib/) (pyMzLib) | 0.1.1 | PRIDE search and download, and the `.raw` scan headers. No .NET install needed |
| [`psutil`](https://pypi.org/project/psutil/) | any recent | CPU and memory accounting. Optional: without it, only wall time is recorded |
| [MetaMorpheus](https://github.com/smith-chem-wisc/MetaMorpheus/releases) command-line release | **1.1.11 exactly** | Stage 4 refuses any other release (see [configuration](docs/configuration.md#search-stage-4)) |
| A UniProt proteome in XML format (`.xml` or `.xml.gz`) | — | The search database. XML carries UniProt's annotated PTMs, which GPTMD uses as its starting point |
| Git | any | The provenance record stores the pipeline commit |
| Disk | ~1–2 GB per raw file, plus the search outputs | Raw spectra are large. They are deleted only on request (stage 9) |
| Nextflow | 23+ (DSL2) | Only for `main.nf`, which is not yet tested |

Thermo `.raw` files are read with Thermo's RawFileReader. Stage 4 passes
`--acceptThermoLicence` to MetaMorpheus **only** when `search.accept_thermo_licence` is `true` in your
parameters. Setting it is your acceptance of Thermo's licence, and it is recorded in the provenance.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip install -r requirements.txt
```

## Quick start

1. **Get the tools.** Unpack a MetaMorpheus 1.1.11 CLI release. Download a UniProt proteome in XML,
   e.g. human UP000005640, reviewed entries.
2. **Edit `params.json`.** At minimum, set these (all keys are in [`docs/configuration.md`](docs/configuration.md)):
   - `work_root`: where all data goes. Put it on a large disk, never beside the code.
   - `run_date`: the label for this run. Outputs go to `<work_root>/run_<run_date>/`.
   - `database.uniprot_xml`: the downloaded proteome.
   - `database.prepared`: must be `<work_root>/db/<file name without .gz>`.
   - `search.metamorpheus_cmd`: the path to MetaMorpheus's `CMD.exe` (Windows), or to `CMD.dll`, which
     runs as `dotnet CMD.dll` on any OS with the .NET runtime the release targets (1.1.11: .NET 10).
   - `search.accept_thermo_licence`: `true` only if you accept Thermo's RawFileReader licence.
   - `fetch.max_files` and `fetch.pick`: how many files to take from the dataset.
3. **Run one accession.** On Windows, the local runner calls each stage in order:

   ```powershell
   powershell -NoProfile -File run_local.ps1 -Accession PXD036557 -Python .venv\Scripts\python.exe
   ```

   On any OS, the same stages can be run by hand. This is what `run_local.ps1` does:

   ```bash
   # W must equal work_root in params.json, and RUN must be $W/run_<run_date>
   set -e   # stop at the first stage that fails
   P=params.json; W=/data/aging; RUN=$W/run_2026-09-18; ACC=PXD036557
   python bin/db_prepare.py $P $W/db
   python bin/discover.py   $P $RUN/01_discover
   python bin/fetch.py      $P $ACC $RUN/$ACC/02_fetch
   python bin/qc_spectra.py $P $RUN/$ACC/02_fetch/spectra $RUN/$ACC/02b_qc
   python bin/search_mm.py  $P $RUN/$ACC/02_fetch/spectra $RUN/$ACC/04_search
   ```

4. **Check the result.** Open `04_search/provenance.json`. `success` must be `true`. Read the `flags`
   array: each flag names something to follow up (see [flags](docs/provenance.md#automatic-flags)).
   The results are in `04_search/mm/Task3SearchTask/`. If `success` is `false`, read the notes, then
   `04_search/metamorpheus.log`.
5. **Optionally reclaim disk.** Stage 9 deletes the raw spectra, which PRIDE can supply again. Run it
   with `--dry-run` first:

   ```bash
   python bin/cleanup.py $P $RUN/$ACC --dry-run
   ```

**Which files are picked.** By default, stage 2 takes one file of median size, not the smallest,
because the smallest file in a dataset is often a blank or a failed run. Set `fetch.pick` to `"all"`
to take the whole dataset.

**What to expect.** One measured run: all 18 files of PXD036557 (iPSC-derived cardiomyocytes from a
Hutchinson–Gilford progeria patient and a control; PRIDE lists a Q Exactive), MetaMorpheus 1.1.11,
`MaxThreadsToUsePerFile = 32`, on a 64-logical-core, 512 GB workstation.
Stage 4 took 21 min of wall time, averaged 20.6 cores, peaked at 15.2 GiB resident memory, and
wrote 60 MB of results. 10.5% of MS2 spectra were identified at 1% FDR. Yours will differ.
Every run records its own numbers (see [resources](docs/provenance.md#resources)).

## Outputs

```
<work_root>/
├── db/                                  stage 0: the prepared (uncompressed) database + provenance.json
├── mm_settings/<release>/               MetaMorpheus's own settings folder, one per release (created by MetaMorpheus)
└── run_<run_date>/
    ├── 01_discover/
    │   ├── candidates_<run_date>.tsv    the FROZEN candidate list: one row per PRIDE hit, keep/drop + reason
    │   ├── discover_summary.json        counts: hits, kept, kept with an SDRF, drops by reason
    │   └── provenance.json
    └── <accession>/
        ├── 02_fetch/
        │   ├── spectra/*.raw            the downloaded raw files
        │   ├── metadata/*sdrf*          the depositor's SDRF, if any (not trusted; see below)
        │   ├── fetch_manifest.json      per file: PRIDE size and checksum, local size, SHA-256
        │   └── provenance.json
        ├── 02b_qc/
        │   ├── qc_report.json           per file: pass/fail, MS2 count, analyzer/dissociation mix, run length, charges
        │   └── provenance.json
        ├── 04_search/
        │   ├── tasks/                   the defaults generated by this MetaMorpheus, and the edited copies that ran (1_…, 2_…, 3_…)
        │   ├── mm/                      MetaMorpheus output: Task1CalibrationTask/, Task2GptmdTask/, Task3SearchTask/, allResults.txt
        │   ├── metamorpheus.log         the full console log, each line stamped with elapsed seconds
        │   ├── resources_timeseries.tsv CPU, memory and threads sampled once per second
        │   └── provenance.json          includes success, ID rate, MBR counts, contamination and flags
        └── 09_cleanup/provenance.json   what was deleted, with each file's SHA-256 at fetch time
```

The main result tables, in `04_search/mm/Task3SearchTask/`:

| File | Contents |
|---|---|
| `AllPSMs.psmtsv` | Every peptide-spectrum match, with q-value, PEP and `Decoy/Contaminant/Target` (`D`, `C` or `T`) |
| `AllPeptides.psmtsv` | One row per peptidoform (a peptide with its modifications) |
| `AllQuantifiedPeptides.tsv` | Peptide intensities per file (FlashLFQ) |
| `AllQuantifiedProteinGroups.tsv` | Protein-group intensities per file |
| `AllQuantifiedPeaks.tsv` | Every chromatographic peak, including unfiltered match-between-runs rows (see [MBR counting](docs/provenance.md#match-between-runs)) |
| `results.txt` | MetaMorpheus's summary |

The GPTMD-augmented database (`Task2GptmdTask/*GPTMD.xml`) is kept. It records every modification
GPTMD added.

## Design principles

These rules come from the working group. The code enforces them unless a rule says otherwise.

1. **Provenance on every output.** Each stage writes `provenance.json`. It records the tools and their
   versions (including the MetaMorpheus binary's SHA-256 and the pipeline's git commit), the exact
   parameters, every command line, every input and output with its SHA-256, and the compute and memory
   used. SDRF metadata alone is not enough to reproduce a reanalysis.
2. **The discovery list is frozen.** A PRIDE search queries a live index, so it can't be rerun
   reproducibly. Stage 1 writes a dated TSV of every hit with its keep/drop decision, and later stages
   should work only from that list. Today the accession to fetch is still chosen by hand (see
   [limitations](#status-and-known-limitations)).
3. **Don't trust depositor metadata.** Stage 1 decides DIA and labelling from the experiment types and the
   protocol text, not from PRIDE's quantification field. In one dataset the curated SDRF says
   "label free", but the protocol describes TMT. Stage 2 downloads any SDRF, but nothing trusts it yet.
4. **Admit only high-resolution HCD spectra read in the Orbitrap** (v1). An instrument name isn't enough:
   hybrid instruments can read MS2 in the ion trap. Stage 2b checks every file's scan headers. The stage
   also refuses files with too few MS2 scans. Glycoproteomics datasets will be exempt, because glycan
   localization needs EThcD or ETD.
5. **Label-free, DDA, Thermo `.raw` in v1.** Labelled (TMT, iTRAQ, SILAC) and DIA datasets are
   dropped at discovery, and the reason is recorded. DIA, TMT and rodent data are in the project's scope
   and will be added.
6. **A pinned search engine.** MetaMorpheus is pinned to one release. Stage 4 checks the running
   binary's reported release, and refuses to run on a mismatch.
7. **Contaminants are searched by default.** MetaMorpheus ships a contaminant database, but its CLI
   searches only the databases passed to it. Stage 4 passes the shipped contaminant database unless
   `database.include_contaminants` is `false` (for controlled experiments only). Keratins, trypsin and
   serum albumin then match as contaminants (`C`) rather than being forced onto human proteins.
8. **Match-between-runs on within a dataset, never across datasets.** Each run is one accession, so MBR
   can't cross datasets. Apex intensity is the quant value. That's MetaMorpheus 1.1.11's default, which
   the pipeline doesn't change.
9. **Suspicious results are flagged, not hidden.** Stage 4 writes automatic flags (low ID rate, failed
   calibration, high contamination, and the known gaps) into the provenance record. A flag never fails
   the stage by itself.
10. **Raw spectra are disposable, but deleted only on request.** PRIDE is the source of truth, and the
    fetch record keeps each file's name, size and SHA-256. Stage 9 refuses to run unless the search
    succeeded.
11. **Built for someone else to operate.** Every stage is headless, non-interactive and config-driven.
    Production runs are meant for operators' servers, through Nextflow and containers.

Code comments and flag messages cite internal identifiers. They're decoded in
[Internal references](#internal-references).

## Status and known limitations

Read this before you run anything on your own data.

- **`main.nf` has not been run.** It defines only `DISCOVER`, `FETCH` and `SEARCH_MM`. It lacks the database,
  spectra-QC and cleanup stages that `search_mm.py` depends on, so as written `SEARCH_MM` would stop at
  its QC check. It also takes the accession from `--accession` rather than from the frozen list. Until
  it's completed and tested, use `run_local.ps1` or the command sequence above.
- **Tested on Windows only.** mzLib's PRIDE client has passed its test suite on Ubuntu 24.04 (reported by
  the mzLib project). On Linux, only stages 0, 2b and 4 have run, on CI's small test files (see Testing).
  They launch MetaMorpheus as `dotnet CMD.dll` and give the same counts as on Windows. On Windows,
  `dotnet CMD.dll` gave the same results as `CMD.exe` on a full-size file (same PSMs, peptides and
  protein groups). No full dataset has been run on Linux yet.
- **One accession per run.** Fetch and search take one dataset at a time.
- **No experimental design yet** (stage 3). FlashLFQ treats each file as its own sample under one
  condition, and no normalization is applied, so **don't compare conditions** from these outputs.
  Every run is flagged `no_design_file`.
- **No output SDRF yet** (stage 5). It waits on an unreleased MetaMorpheus feature, and every run is
  flagged `no_output_sdrf`.
- **Download integrity.** Downloads are atomic (written to `.partial`, then renamed) and are skipped when
  already complete. Nothing resumes a failed transfer. PRIDE usually supplies no checksum, so integrity
  rests on the local SHA-256 recorded at fetch.
- **The match-between-runs FDR threshold** used in the provenance counts is fixed at 0.01, which is
  MetaMorpheus's default. It isn't yet read from the task settings.
- **`run_local.ps1`'s default `-Python` path** is the developer's machine. Always pass `-Python`.
- **`search.timeout_s` is not enforced.** A hung MetaMorpheus process isn't killed. Under Nextflow, set a
  process `time` limit.
- **The accession is chosen by hand.** Nothing yet iterates over the frozen list's `keep = yes` rows.
- **Discovery is human-only** (`discover.organism`), with four keywords. It's deliberately simple and
  conservative, and every dropped dataset records its reason.

## Testing

```bash
pip install -r requirements.txt pytest
python -m pytest -m "not network" -rs    # offline: no network, no MetaMorpheus; ~5 s
python -m pytest -m network -v -rs       # live canaries against PRIDE and UniProt

# a real MetaMorpheus search (skips unless both variables are set; ~20 s on 32 cores)
AGING_MM_CMD=/path/to/MetaMorpheus/CMD.dll AGING_MM_DATA=/path/to/testdata \
  python -m pytest -m metamorpheus -v -rs -s
```

**Offline tests** run every stage script as it really runs, on synthetic inputs. Only the outside world
is faked:
- PRIDE: fake search results and file lists;
- the `.raw` reader: synthetic scan headers;
- MetaMorpheus: [`tests/fake_metamorpheus.py`](tests/fake_metamorpheus.py), a stand-in CLI with
  1.1.11's output shapes.

`tests/test_pipeline_offline.py` is a minimal pipeline end to end (stage 0 → 2b → 4 → 9). It checks
these contracts between stages:
- the directory layout;
- upstream provenance links;
- the refusal rules: failed QC, the wrong MetaMorpheus release, a reused output folder, and cleanup
  before a successful search;
- the success check: exit code 0 without protein groups is **not** success;
- the ID-rate, MBR and contamination measurements, and the flags.

**Live tests** (marked `network`) are canaries for the pipeline's contract with PRIDE and UniProt:
- discovery must still find PXD036557 for "progeria";
- fetch must still list its 18 raw files and download its SDRF (no raw file is downloaded);
- UniProt must still serve UniProt XML with modified residues.

The outage rule is the same as in mzLib and pyMzLib:

| What happens | Result |
|---|---|
| The service is **down**: a timeout, a refused connection, HTTP 408, 429 or 5xx, or `pymzlib.ServiceUnavailableError` | The test **skips**, and says why |
| Anything else, e.g. a changed response or a wrong answer | The test **fails** |

[`tests/test_live_guard.py`](tests/test_live_guard.py) checks that rule offline, so a guard that
skipped too much would itself go red.

**The real search** (marked `metamorpheus`, [`tests/test_real_search.py`](tests/test_real_search.py)) runs
stages 0 → 2b → 4 with nothing faked. It uses MetaMorpheus 1.1.11 and two sliced Thermo `.raw` files, plus a
pruned human UniProt XML, from mzLib's test data (`mzLib/Test/FlashLFQ/TestData`; CI pins the commit).
It checks that the search succeeds, that the release and launcher are recorded, and that all three tasks
ran with the contaminant database. It also sets a floor of 50 PSMs at 1% FDR. MetaMorpheus 1.1.11 finds 78
target PSMs from 1,155 MS2 scans, on both Windows and Linux. Running it accepts Thermo's RawFileReader licence.

**CI** ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)):
- The offline suite runs on Linux, Windows and macOS with Python 3.11 and 3.13, on every push and pull
  request.
- The live canaries run on Linux after it, and weekly.
- The live job has no `continue-on-error`: an outage already skips, so a red live job means a real
  break.
- The real search runs on Linux after the offline suite, on every push and weekly. It installs .NET 10,
  unpacks the MetaMorpheus 1.1.11 command-line release, and launches it as `dotnet CMD.dll`. The release
  and the test files are cached. A skip counts as a failure there.

Not covered by CI yet: `main.nf`.

## Versioning

Everything that determines a result is under version control, or is pinned and recorded by hash.

| What | How it's controlled |
|---|---|
| The pipeline (scripts, `main.nf`, configs, docs) | Git. Releases follow [semantic versioning](https://semver.org/). Each release is a git tag (`v0.1.0`), its number is in [`VERSION`](VERSION), and [`CHANGELOG.md`](CHANGELOG.md) says what changed |
| Parameters | The parameters file is versioned alongside the code; every run records the file's SHA-256 |
| Python dependencies | Pinned in [`requirements.txt`](requirements.txt) (`mzlib==0.1.1`) |
| MetaMorpheus | Pinned to one release. Stage 4 refuses any other and records the binary's SHA-256 |
| The protein database | Recorded by file name and SHA-256 at every search. UniProt releases are dated in the file name |
| Data | PRIDE is the source; every downloaded file's SHA-256 is recorded at fetch |

Every `provenance.json` records the pipeline's `version`, its git `commit` and its `repo`, plus `+dirty`
when the working copy had uncommitted changes. From any output, you can check out the exact code that
produced it. Until 1.0, a minor release may change parameters or output formats. The provenance
schema has its own version (`aging-provenance/N`), which changes whenever its fields change meaning.
## Internal references

Code comments and flag messages carry identifiers from the project's internal records. They're kept
so each rule traces to the decision behind it. You don't need them to run the pipeline.

| Identifier | Meaning |
|---|---|
| `D1` | Glue only: substantive algorithms belong in mzLib or MetaMorpheus, not here |
| `D3` | Raw data lives on a data disk and is disposable after search |
| `D4` | The pipeline does all the work: no manual steps |
| `D5` | Built for operators: headless, config-driven, containerizable, resumable |
| `D6` | Nextflow is the workflow engine |
| `D8` | Isoforms are targeted, not proteome-wide (future database stage) |
| `D9` | A provenance record on every output |
| `D11` | Compute and memory are recorded for every stage |
| `S3`, `S4`, `S5`, `S7`, `S15` | Entries in the project's findings ledger: low ID rate, MBR over-counting (fixed), low core use, calibration failure, and missing contaminant database (fixed) |
| `DEF-…` | Metric definitions: `DEF-MBR-KEPT`/`DEF-MBR-ROW`/`DEF-QC-MBR` ([MBR counts](docs/provenance.md#match-between-runs)), `DEF-PSM-1PCT`/`DEF-PSM-FDRENGINE` ([ID rate](docs/provenance.md#id-rate)), `DEF-CONTAM-PSM` and QuantProject's `DEF-QC-9` ([contamination](docs/provenance.md#contamination)), `DEF-PEP-INT` (apex intensity) |
| `REQ-…` | Feature requests filed with the tools this pipeline uses, e.g. download retry and checksums in mzLib's PRIDE client, and `.raw` input in pyMetaMorpheus |
| `mzLib #…`, `MetaMorpheus #…` | Issue or pull-request numbers in those GitHub repositories |
| `<project> NNN`, `Q…`, `D5-b`… | Messages and answers in coordination threads with the tools' projects (not public) |

**The `params_*.json` files** come from the PXD036557 test series:

| File | Run |
|---|---|
| `params_s15_contam.json` / `params_s15_nocontam.json` | 1 file, MetaMorpheus 1.1.11, with and without the contaminant database |
| `params_scale_n6.json` | 6 files (GM1 a–c, GM2 a–c) |
| `params_scale_n18.json` | All 18 files, MetaMorpheus 1.1.11: the baseline quoted above |

Only `params_scale_n18.json` has been checked against its run's recorded params hash. The others were
edited after their runs, so they show the setup, but they aren't byte-exact records.

## Repository layout

```
main.nf               Nextflow wiring (untested; see limitations)
nextflow.config       Nextflow trace/report/timeline and retry settings
params.json           the default parameters (a template: edit the paths)
params_*.json         parameter files from the PXD036557 test series (see Internal references)
run_local.ps1         Windows runner for one accession
requirements.txt      Python dependencies
VERSION               the pipeline release number (semantic versioning)
bin/
  provenance.py       the provenance record and resource monitor every stage uses
  db_prepare.py       stage 0
  discover.py         stage 1
  fetch.py            stage 2
  qc_spectra.py       stage 2b
  search_mm.py        stage 4
  cleanup.py          stage 9
docs/                 stage, configuration and provenance reference
tests/                offline and live tests (see Testing)
pyproject.toml        pytest configuration (the network and metamorpheus markers)
.github/workflows/    CI
```

This repository is the public mirror of the pipeline folder of the project's working repository. It
is updated from there, and its history is the pipeline's own history.

## Citing

Until there's a pipeline paper, please cite this repository (see [`CITATION.cff`](CITATION.cff)) and the
tools it runs:

- Solntsev, S. K.; Shortreed, M. R.; Frey, B. L.; Smith, L. M. Enhanced Global Post-translational
  Modification Discovery with MetaMorpheus. *J. Proteome Res.* **2018**, *17* (5), 1844–1851.
  https://doi.org/10.1021/acs.jproteome.7b00873
- Millikin, R. J.; Solntsev, S. K.; Shortreed, M. R.; Smith, L. M. Ultrafast Peptide Label-Free
  Quantification with FlashLFQ. *J. Proteome Res.* **2018**, *17* (1), 386–391.
  https://doi.org/10.1021/acs.jproteome.7b00608

## License

MIT. See [`LICENSE`](LICENSE). MetaMorpheus, mzLib and Thermo's RawFileReader carry their own licences.

## Contact

Michael Shortreed, Smith lab, University of Wisconsin–Madison. Please open an issue on this repository.
