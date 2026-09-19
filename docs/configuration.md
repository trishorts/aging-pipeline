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
| `organism` | string | `"Homo sapiens (human)"` | Must exactly match an entry in the project's PRIDE organism list |
| `require_sdrf_file` | bool | `false` | When `true`, drop projects without an SDRF file (`no_sdrf`) |
| `thermo_instrument_patterns` | list of strings | Q Exactive, Orbitrap, Exploris, Fusion, Lumos, Eclipse, LTQ, Velos, Elite | Literal, case-insensitive substrings; one must match an instrument, or the project is dropped as `not_thermo` |
| `orbitrap_ms2_only_patterns` | list of strings | Q Exactive, Exploris | Instruments that can only read MS2 in the Orbitrap → `ms2_class = orbitrap_hcd_only` |
| `hybrid_patterns` | list of strings | Velos, Elite, Fusion, Lumos, Eclipse, Orbitrap XL, Orbitrap Tribrid | Hybrids that may read MS2 in the ion trap → `ms2_class = check_ms2` (stage 2b decides). Any instrument name containing "orbitrap" is also treated as `check_ms2` |
| `dia_patterns` | list of **regexes** | `data-independent`, `DIA-NN`, `SWATH`, `\bDIA\b`, `Astral`, … | Matched case-insensitively against the experiment types and the protocol text → `dia` |
| `label_patterns` | list of **regexes** | `\bTMT`, `tandem mass tag`, `iTRAQ`, `isobaric`, `SILAC`, `TMTpro`, `dimethyl label` | Matched against the same text → `labelled` |
| `timeout_s` | int | `300` | The timeout for each PRIDE search call |

`dia_patterns` and `label_patterns` are regular expressions (write `\\b` in JSON for a word boundary).
The instrument lists are literal text.

## `fetch` (stage 2)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `accession` | string or null | `null` | **Not read by `fetch.py`.** The accession is its command-line argument. It's kept as a record |
| `max_files` | int | `1` | How many files to take (ignored when `pick` is `all`) |
| `pick` | `median_size` · `first_by_name` · `all` | `median_size` | Which files ([details](stages.md#stage-2-fetchpy-download-one-accession)). With `median_size`, the smallest file is chosen only if it's the only candidate left. Any other value is rejected |
| `parallel_downloads` | int | `4` | Concurrent downloads |
| `max_file_mb` | int | `1500` | Skip files larger than this (in MB, 10⁶ bytes) |
| `extension` | string | `".raw"` | The spectra file extension to fetch. Leave it as `.raw`: stages 2b, 4 and 9 look only for `*.raw` |
| `timeout_s` | int | `3600` | The timeout per file download |

## `qc` (stage 2b)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `min_fraction_orbitrap_hcd` | float 0–1 | `0.9` | The minimum fraction of MS2 scans that are HCD read in the Orbitrap |
| `min_ms2` | int | `5000` | The minimum number of MS2 scans (catches blanks and failed runs) |
| `timeout_s` | int | `1800` | The timeout for reading one file's scan headers |

## `database` (stages 0 and 4)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `uniprot_xml` | path | — | The source proteome, UniProt XML, `.xml` or `.xml.gz` |
| `prepared` | path | — | **Must be** `<work_root>/db/<uniprot_xml's name without .gz>`, which is where stage 0 writes it. Stage 4 searches this file |
| `include_contaminants` | bool | `true` | Also search MetaMorpheus's shipped `Contaminants/MetaMorpheusContaminants.xml`. Turning it off is for controlled experiments only, and a note records it |

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
| `flag_min_id_rate` | float 0–1 | `0.15` (optional key) | Flag `low_id_rate` when PSMs at 1% FDR ÷ MS2 scans fall below this |
| `flag_max_contaminant_intensity_share` | float 0–1 | `0.05` (optional key) | Flag `high_contamination` when any file's contaminant share of protein intensity exceeds this |
| `timeout_s` | int | `21600` | Intended as the maximum wall time for MetaMorpheus, but **not enforced yet**: a hung process isn't killed. Under Nextflow, set a process `time` limit |

Settings not listed here aren't changed: they're MetaMorpheus's own defaults for the pinned release.
To change a search setting that isn't a key here, extend `search_mm.py` so the change is named in
the parameters and so recorded. Don't hand-edit generated TOMLs.

## Nextflow parameters (`main.nf`, untested)

| Parameter | Default | Meaning |
|---|---|---|
| `--params_file` | `<projectDir>/params.json` | The parameters file above |
| `--accession` | none | The accession to fetch and search (prototype: one at a time) |
| `--outdir` | `results` | The publish directory; `pipeline_info/` holds the trace, report and timeline |
