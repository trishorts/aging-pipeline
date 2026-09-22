# Configuration reference

All settings live in a single JSON file, `params.json` by default. Each stage reads its own section
(stage 4 reads `search` and `database`). Every provenance record copies the stage's main section and
stores the whole file's SHA-256. To record a run exactly, **copy the file, change the copy, and keep
the copy**, because the hash identifies the file but can't reconstruct it.

Keys that start with `_` (`_comment`, `_run`, `_rule`) are free-text notes that the code ignores. A note
*inside* a section (such as `qc._rule`) is copied into that stage's provenance. A top-level note (`_run`,
`_comment`) survives only in the file itself, identified by its hash.

Paths may use forward slashes on every OS.

## Top level

| Key | Type | Example | Meaning |
|---|---|---|---|
| `run_date` | string | `"2026-09-18"` | The run label. Outputs go to `<work_root>/run_<run_date>/`, and the frozen list is `candidates_<run_date>.tsv` |
| `work_root` | path | `"/data/aging"` | The root of all data. Provenance stores paths under it **relative** to it, so records stay valid when the tree moves to another machine. Put it on a large disk, away from the code |

## `discover` (stage 1)

| Key | Type | Default in `params.json` | Meaning |
|---|---|---|---|
| `keywords` | list of strings | `["aging", "ageing", "senescence", "longevity"]` | One PRIDE search per keyword; hits are unioned |
| `organism` | string **or list of strings** | `"Homo sapiens (human)"` | Each name must match an entry in the project's PRIDE organism list **exactly**; a project is kept if it matches any of them. Use a list where PRIDE carries one species under several spellings — `["Mus musculus (mouse)", "Mus musculus"]` keeps 47 aging deposits that the first name alone drops. Do **not** be tempted by substring matching: `"Rattus norvegicus"` as a substring also matches nothing useful, while a loose rule would pull in `Rattus rattus (black rat)` |
| `require_sdrf_file` | bool | `false` | When `true`, drop projects without an SDRF file (`no_sdrf`) |
| `thermo_instrument_patterns` | list of strings | Q Exactive, Orbitrap, Exploris, Fusion, Lumos, Eclipse, LTQ, Velos, Elite | Literal, case-insensitive substrings; one must match an instrument, or the project is dropped as `not_thermo` |
| `orbitrap_ms2_only_patterns` | list of strings | Q Exactive, Exploris | Instruments that can only read MS2 in the Orbitrap → `ms2_class = orbitrap_hcd_only` |
| `hybrid_patterns` | list of strings | Velos, Elite, Fusion, Lumos, Eclipse, Orbitrap XL, Orbitrap Tribrid | Hybrids that may read MS2 in the ion trap → `ms2_class = check_ms2` (stage 2b decides). Any instrument name containing "orbitrap" is also treated as `check_ms2` |
| `dia_patterns` | list of **regexes** | `data-independent`, `DIA-NN`, `SWATH`, `\bDIA\b`, `Astral`, … | Matched case-insensitively against the experiment types and the protocol text → `dia` |
| `label_patterns` | list of **regexes** | `\bTMT`, `tandem mass tag`, `iTRAQ`, `isobaric`, `SILAC`, `TMTpro`, `dimethyl label` | Matched against the same text → `labelled` |
| `metabolic_label_patterns` | list of **regexes** | heavy water, `\bD2O\b`, deuterium, `\b15N\b`, pulsed SILAC, … | Metabolic labelling → `labelled` |
| `enrichment_patterns` | list of **regexes** | immunoprecipitation, pull-down, streptavidin, BioID, TurboID, APEX2, kinobead, TiO2/IMAC, K-GG, … | → `enriched`. Patterns are written so that words shared with ordinary proteomics do not match: the chromatographic "apex" is not APEX2, and "without affinity enrichment" is not an enrichment |
| `timeout_s` | int | `300` | The timeout for each PRIDE search call |

`dia_patterns` and `label_patterns` are regular expressions (write `\\b` in JSON for a word boundary).
The instrument lists are literal text.

## `fetch` (stage 2)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `accession` | string or null | `null` | **Not read by `fetch.py`.** The accession is its command-line argument. It's kept as a record |
| `max_files` | int | `1` | How many files to take (ignored when `pick` is `all`) |
| `pick` | `all` · `median_size` · `first_by_name` | `all` | Which files ([details](stages.md#stage-2-fetchpy-download-one-accession)). `all` takes the whole deposit and is the only choice for results you will interpret; the others are for probing, and any subset is flagged `subset_of_deposit` in provenance. With `median_size`, the smallest file is chosen only if it's the only candidate left. Any other value is rejected |
| `parallel_downloads` | int | `4` | Concurrent downloads |
| `max_file_mb` | int | `1500` | Skip files larger than this (in MB, 10⁶ bytes). **Check it against the deposit before a run**: PXD027318's three largest files are 1.62–1.63 GB, so the default would have silently dropped half of one experimental arm |
| `max_attempts` | int | `3` | Attempts per file before the stage fails. Only a `ServiceUnavailableError` (a dropped connection) is retried; any other error fails at once |
| `retry_backoff_s` | float | `10` | Seconds to wait before a retry, multiplied by the attempt number (10 s, then 20 s). No sleep after the final attempt |
| `extension` | string | `".raw"` | The spectra file extension to fetch. Leave it as `.raw`: stages 2b, 4 and 9 look only for `*.raw` |
| `timeout_s` | int | `3600` | The timeout per file download |

## `qc` (stage 2b)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `min_fraction_orbitrap_hcd` | float 0–1 | `0.9` | The minimum fraction of MS2 scans that are HCD read in the Orbitrap |
| `min_ms2` | int | `5000` | The minimum number of MS2 scans (catches blanks and failed runs) |
| `timeout_s` | int | `1800` | The timeout for reading one file's scan headers |
| `acquisition_exception` | object | absent | A scoped waiver that lets a named QC failure through. See below |

Each file's `qc_report.json` entry carries a `fail_reasons` list naming every condition it failed —
`low_res_ms2` (below `min_fraction_orbitrap_hcd`) and `too_few_ms2` (below `min_ms2`) — so a waiver can
forgive one without forgiving the other. A passing file has an empty list.

### `acquisition_exception`

Stage 4 refuses to search a file that failed QC. An `acquisition_exception` is the only way past that,
and it is deliberately awkward: it must **name the conditions it waives**, and a file that fails
anything it does not name still fails. A waiver cannot quietly become a blanket override.

```json
"acquisition_exception": {
  "reason":       "why this deposit fails, in enough detail to re-check",
  "granted_by":   "user",
  "granted_date": "2026-09-20",
  "waives":       ["low_res_ms2"],
  "restricts_to": ["abundance"],
  "bars":         ["ptm_stoichiometry", "ptm_site_localization"],
  "rationale":    "why the restricted use is still sound"
}
```

| Key | Meaning |
|---|---|
| `waives` | The `fail_reasons` this exception forgives. **`too_few_ms2` and `unreadable` cannot be waived — naming either one is refused with an error**, because a file with almost no spectra, or one the reader cannot open, is not an acquisition choice. (Before 2026-09-22 this was advice that nothing enforced.) `too_few_ms2` should never be waived — a file with almost no spectra is not an acquisition choice |
| `restricts_to` / `bars` | What the results may and may not be used for. Not enforced by the pipeline; they are recorded so a downstream consumer can enforce them |
| the rest | Free text, copied verbatim into provenance |

When it fires, the run is **not** silently normal. `provenance.json` gains an `acquisition_exception`
block holding the whole object plus the files and reasons it applied to, and `flags` gains a line
naming the restriction. Both travel with the results into the repository, so a query cannot reach the
data without the restriction being visible beside it.

**Worked example — PXD060431 (`params_PXD060431.json`).** Its deposited SDRF says *Orbitrap Exploris
480*, but all 30 files are high-low: MS1 in the Orbitrap and every MS2 read out in the ion trap
(`ITMS + c NSI r d Full ms2 <mz>@hcd33.00`), so `fraction_orbitrap_hcd` is exactly `0.0000`
everywhere. An Exploris has no ion trap, so the instrument metadata could not be trusted and only the
file-level check caught it. The waiver admits the dataset for **abundance** — quantification reads MS1
peaks, which are Orbitrap here, and identification from ion-trap HCD is sound — and bars it from
**site-level PTM claims**, which is what low-resolution fragments cannot support.

## `database` (stages 0 and 4)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `uniprot_xml` | path | — | The source proteome, UniProt XML, `.xml` or `.xml.gz` |
| `prepared` | path | — | **Must be** `<work_root>/db/<uniprot_xml's name without .gz>`, which is where stage 0 writes it. Stage 4 searches this file |
| `include_contaminants` | bool | `true` | Also search MetaMorpheus's shipped `Contaminants/MetaMorpheusContaminants.xml`. Turning it off is for controlled experiments only, and a note records it |
| `contaminants` | path | *(optional)* | Use this contaminant database instead of the shipped one. Needed when re-running only the Search task against a GPTMD database: the GPTMD task writes its own augmented `MetaMorpheusContaminantsGPTMD.xml`, and searching the shipped file instead would drop every contaminant modification GPTMD found and make the contamination metrics incomparable with a full-chain run |

## `search` (stage 4)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `metamorpheus_cmd` | path | — | The MetaMorpheus command-line tool: `CMD.exe` (Windows), or `CMD.dll`, which is run as `<dotnet> CMD.dll` on any OS. Either way, `CMD.dll` (it's hashed) and the `Contaminants/` folder must be in the same folder |
| `dotnet` | path | `"dotnet"` (optional key) | The .NET host used when `metamorpheus_cmd` is a `.dll`. It needs the runtime the release targets (1.1.11: .NET 10) |
| `metamorpheus_version` | string | `"1.1.11"` | The release this run requires. Stage 4 **refuses** when the binary reports anything else. Change it deliberately, together with the binary |
| `accept_thermo_licence` | bool | — | `true` passes `--acceptThermoLicence`: your explicit acceptance of Thermo's RawFileReader licence. Required to read `.raw` non-interactively |
| `tasks` | list | `["Calibration", "Gptmd", "Search"]` | The MetaMorpheus tasks, in order. The allowed names are exactly these three |
| `max_threads` | int | `32` | Written into every task's `MaxThreadsToUsePerFile`. The provenance flags `low_core_use` when the average is under half of this |
| `match_between_runs` | bool | `true` | Written into the search task's `MatchBetweenRuns`. It's within one dataset only, because each run is one accession |
| `product_mass_tolerance` | string | *(optional; MetaMorpheus's default)* | Written into **every** task's `ProductMassTolerance`, in MetaMorpheus's own spelling — `"±0.5000 Absolute"` or `"±20.0000 PPM"`. See below |
| `search_type` | `Classic` · `Modern` · `NonSpecific` | *(optional; MetaMorpheus's default, `Classic`)* | Written into the search task's `SearchType`. An unknown value is **refused**. See below |
| `precursor_mass_tolerance` | string | *(optional; MetaMorpheus's default)* | The same, for `PrecursorMassTolerance` |
| `exclude_files` | list of names | *(optional)* | Spectra file names to leave out of the search. Every name must exist in the spectra directory, or the stage **refuses** — a typo would otherwise silently search everything. Pair it with `exclude_files_why` |
| `exclude_files_why` | string | *(optional)* | Why those files are excluded. Copied into `provenance.json` beside the names |
| `flag_min_id_rate` | float 0–1 | `0.15` (optional key) | Flag `low_id_rate` when PSMs at 1% FDR ÷ MS2 scans fall below this |
| `flag_max_contaminant_intensity_share` | float 0–1 | `0.05` (optional key) | Flag `high_contamination` when any file's contaminant share of protein intensity exceeds this |
| `timeout_s` | int | `21600` | Intended as the maximum wall time for MetaMorpheus, but **not enforced yet**: a hung process isn't killed. Under Nextflow, set a process `time` limit |

Settings not listed here aren't changed: they're MetaMorpheus's own defaults for the pinned release.
To change a search setting that isn't a key here, extend `search_mm.py` so the change is named in
the parameters and so recorded. Don't hand-edit generated TOMLs.

### `spectral_library` (stage 4)

One spectral library per organism, built up across searches. **Optional and off by default**, so a
params file written before this existed behaves exactly as it did.

| key | type | default | meaning |
|---|---|---|---|
| `enabled` | bool | `false` | Turn the library on for this run |
| `organism` | string | *(required when enabled)* | The library this run belongs to — `human`, `mouse`, `rat`, … Lower-cased, spaces become underscores. **Refused if missing**: it is never inferred from `discover.organism`, which is a PRIDE facet string |
| `root` | path | `<work_root>/spectral_libraries` | Where libraries and `registry.json` live. Under `work_root`, so `cleanup.py` deleting a run's spectra never touches them |

```json
"search": {
  "spectral_library": { "enabled": true, "organism": "human" }
}
```

The first search of an organism writes the library; every search after that updates it and consumes
the current one. See [stages](stages.md#the-spectral-library) for the mechanics, the registry format,
how to roll back, and the cases that are refused or flagged.

**Note on `search_type`:** only `Classic` consults a spectral library. With `Modern` or
`NonSpecific` the library is loaded, ignored during the search, and still updated afterwards — the
stage flags that combination rather than letting it pass.

### Mass-tolerance overrides, and when you need one

Both tolerance keys are optional, and leaving them out is right for almost every dataset: MetaMorpheus's
defaults are chosen for the high-resolution data this pipeline normally admits.

They exist because a **low-resolution MS2** dataset is not merely searched *less well* by the default —
it is searched almost not at all, and quietly. MetaMorpheus's default `ProductMassTolerance` is
`±20.0000 PPM`. Ion-trap fragments are accurate to a few tenths of a dalton, which is *hundreds* of ppm,
so a 20 ppm window matches only the small and biased subset of fragments that happen to land inside it.
On PXD060431 that produced a 2.27% identification rate, a calibration failure on all 30 files, and more
quantified peaks coming from match-between-runs than from MS/MS — none of which looks like a tolerance
problem from the outside.

The way to see it is to measure the errors the search actually accepted:

```
1st–99th percentile of matched-ion error: −19.4 … +19.7 ppm
matched ions beyond ±20 ppm:              0.0% of 39,579
```

A hard cutoff at exactly the configured tolerance means the tolerance, not the data, set the limit.

**A conventional ion-trap fragment tolerance is `"±0.5000 Absolute"`.** Note that MetaMorpheus already
carries a `ProductMassTolerance_LowRes` of `±0.3500 Absolute` in the same file and did **not** select it
for this data; these keys do not touch that line, and whether the engine should choose it on its own is
an upstream question.

**A wide tolerance can outgrow `Classic` search.** `Classic` scores every candidate peptide against
every spectrum, which is fine at a high-resolution tolerance and can be intractable at a wide one. On
PXD060431's ion-trap spectra (~1,000 peaks per MS2) at `±0.5000 Absolute`, it searched 22 files in
192 seconds and then spent hours on the 23rd at 52 sustained cores — three times over, with GPTMD and
without it, on a different file each time, where each of those files had searched in ~7 seconds in
another run. The cost is the candidate space, not any one file. `search_type = "Modern"` indexes
fragments instead and is the mode meant for this case.

**Override only what is wrong.** On PXD060431 the MS1 is Orbitrap and the measured precursor error is
tight (median −3.11 ppm), so `precursor_mass_tolerance` is left alone and only the product tolerance is
widened. Widening a tolerance that was already correct costs sensitivity and specificity for nothing.

An override is **a deviation from the pinned engine's defaults**, so it is recorded rather than left in
a params file: `provenance.json` gains a `tolerance_overrides` block naming each task and key that was
changed. If exactly one matching line is not found in a task's generated TOML, the stage **fails** —
silently changing nothing would be worse than stopping.

## Nextflow parameters (`main.nf`, untested)

| Parameter | Default | Meaning |
|---|---|---|
| `--params_file` | `<projectDir>/params.json` | The parameters file above |
| `--accession` | none | The accession to fetch and search (prototype: one at a time) |
| `--outdir` | `results` | The publish directory; `pipeline_info/` holds the trace, report and timeline |
