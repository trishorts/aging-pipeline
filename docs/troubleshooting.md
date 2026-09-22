# Troubleshooting

Failures this pipeline has actually hit, what each one looks like, and what to do. Every entry here
was met on a real run — none is hypothetical, and where a number appears it was measured.

**First, always:** read `<stage>/provenance.json`. It records the exact command, the tool versions,
every input and output with its SHA-256, the measured resources, and a `notes` array in which the
stage explains anything unusual it did. Most of what follows is visible there before it is visible
anywhere else.

---

## Downloads

### `ResponseEnded` — "The response ended prematurely, with at least N additional bytes expected"

```
RuntimeError: <file>.raw: 3 attempts all failed; last error: The response ended
prematurely, with at least 1027690365 additional bytes expected. (ResponseEnded)
```

**What it is.** EBI dropped the connection mid-transfer. `fetch.py` retries this error and only this
error, because an outage is tolerable and a malformed request is not.

**Why retrying may not save you.** A retry is **not a resume** — it starts again from byte zero. If
the chance of completing a file in one connection is low, no number of attempts gets it, because
every attempt repeats the same gamble rather than shortening it.

**Measured, on one machine within one hour:**

| deposit | file size | outcome |
|---|---|---|
| 20 files | ~220 MB each | four parallel streams, ~5 MB/s, completed in 20 minutes |
| 10 files | ~1.15 GB each | 3 attempts each dying ~64 MB in; abandoned at 2 of 10 |
| 12 files | ~488 MB each | 8 attempts on one file, all failed |

Note the third row: size **correlates** with failure but does not explain it. A 488 MB file failed
eight times in a window where 220 MB files were fine.

**What to do.**
1. Raise `fetch.max_attempts` (default 3) and `fetch.retry_backoff_s`. Cheap, since a failed attempt
   usually dies early — but it will not rescue every deposit.
2. Re-run the stage later. `overwrite=False` means completed files are skipped, so a re-run only
   pays for what is missing. This is the single most effective thing.
3. Prefer deposits with smaller files if you are working through a list.
4. If a deposit will not come, record that and move on. A partial deposit is still usable — but see
   the warning below.

**If you accept a partial deposit, say so in your results.** Counts over 17 of 20 files are not
comparable with counts over 20, and a deposit missing a third of its files can be missing a whole
experimental arm.

### Downloads appear to stall, or fewer run in parallel than configured

Check twice, a minute apart, before concluding anything. A single short sample will mislead you: the
same directory read 25 seconds apart showed **0 MB moved**, and 60 seconds later showed **61 MB/s**
— the first sample landed in a gap between retry attempts.

If parallelism really is below `fetch.parallel_downloads`, it is usually workers failing one after
another on `ResponseEnded` rather than anything in the client. On a clean re-run all streams come up.

### `403 Forbidden` from PRIDE

```
BridgeError: PRIDE FTP directory listing failed with status 403 Forbidden
```

Observed once during sustained downloading, and transient — both the REST and FTP listings worked
normally when retried a minute later. Treat it as rate limiting: pause, then retry. It is **not** in
the tolerated-outage set, so a live test fails rather than skips on it, which is deliberate.

### `fetch.max_file_mb` silently dropped files

Files larger than the cap are excluded from the listing **before** selection, with no error. An
inherited cap of 1500 would have dropped a deposit's three largest files — half an experimental arm —
and nothing would have said so.

**Check the cap against the deposit before every run.** `fetch.py`'s output reports
`rest_raw_count`; compare it with what you expected.

---

## Spectra QC (stage 2b)

### The whole dataset is rejected

`qc_spectra.py` admits only high-resolution Orbitrap HCD MS2 by default. Each file's verdict is in
`02b_qc/qc_report.json` with `fail_reasons`:

| reason | meaning | waivable? |
|---|---|---|
| `low_res_ms2` | below `qc.min_fraction_orbitrap_hcd` | yes, by an explicit acquisition exception |
| `too_few_ms2` | below `qc.min_ms2` | **never** |
| `unreadable` | the reader could not open the file | **never** |

**Do not trust the deposit's own metadata.** One deposit's SDRF named an Orbitrap Exploris 480 and
every one of its 30 files was ion trap throughout. `qc_spectra` is the only stage that checks what the
data actually is, and it cost a 17 GB download to learn that the hard way.

**Fetch one file first.** Run `fetch.py` with `fetch.max_files: 1`, run `qc_spectra.py` on it, and
only fetch the rest if it passes. This is the cheapest guard in the pipeline.

### An acquisition exception is refused

```
acquisition_exception waives ['too_few_ms2'], which cannot be waived
```

A waiver names the exact `fail_reasons` it forgives and forgives nothing else. `too_few_ms2` and
`unreadable` cannot be waived by any waiver: a file with almost no spectra, or one that cannot be
read, is not an acquisition choice. See [configuration](configuration.md#acquisition_exception).

---

## Search (stage 4)

### The search never finishes

`search.timeout_s` bounds it. On expiry the stage kills the **process tree** — necessary because
`dotnet CMD.dll` makes the search a child of the launcher, so killing the launcher alone would leave
it running — records `timed_out_after_s`, notes that the partial outputs are not a completed search,
and fails cleanly with provenance written.

If a search is slow rather than hung, the usual cause is a **fragment tolerance that does not fit the
data**. A low-resolution MS2 dataset searched at MetaMorpheus's `±20 PPM` default identifies only the
small biased subset that happens to fall inside a high-resolution window. The tell is a matched-ion
error distribution with a hard cliff exactly at the configured value: in one case **0 of 39,579
matched ions exceeded 20 ppm**. Set `search.product_mass_tolerance` (see
[configuration](configuration.md#mass-tolerance-overrides-and-when-you-need-one)).

Note that a wide absolute tolerance makes `Classic` search very expensive — it scores every candidate
against every spectrum. `search.search_type: "Modern"` indexes fragments instead, which is the mode
built for that case.

### `exit 0` but no `AllQuantifiedProteinGroups.tsv`

FlashLFQ failed silently. The stage detects this, notes it, and reports `success: false`.

### MetaMorpheus CLI quirks

- `--version` prints the help text and exits non-zero. The release comes from the `-g` banner instead.
- An **empty, pre-created** `--mmsettings` directory crashes it. Let it create the directory itself.
- A `.gz` database makes it write `temp.xml` beside the input. `db_prepare.py` hands it an
  uncompressed copy for this reason.
- Piping its stdout through a pager or `head` kills it mid-run and looks like a crash.

---

## Spectral library (stage 4, optional)

### The library did not advance

The stage notes it and flags it; the search itself still stands. Check
`provenance.json`'s `spectral_library` block for the mode and any `error`.

### The library is being ignored

Only `Classic` consults a spectral library. With `search_type` of `Modern` or `NonSpecific` the
library is loaded, **never used during the search**, and still updated afterwards — so it grows
without having contributed an identification. The stage flags that combination rather than letting it
pass.

### The registry points at a library that is not there

The stage refuses rather than starting a new chain, because falling back to a write would silently
discard everything the first chain accumulated — visible only as a library that got *smaller*. Restore
the file, or roll back to a version that exists:

```bash
python bin/spectral_library.py list     params.json
python bin/spectral_library.py rollback params.json human 1
```

---

## Cleanup (stage 9)

### It refuses to run

- *"refusing: ... missing or not successful"* — the search did not succeed. Deliberate: a failed
  search keeps its inputs.
- *"refusing: ... already records a real cleanup"* — this directory has been cleaned. A second run
  would find nothing to delete and replace the record of what the first one removed with
  `0 files, 0 bytes`. Pass `--force` only if you accept losing that record.

### Some files could not be deleted

They are listed in `09_cleanup/provenance.json` under `not_deleted` with the error. The stage
continues past a locked file rather than abandoning the record of the ones it did delete. On Windows a
`.raw` held open by another process is the usual cause.

---

## Reading the numbers afterwards

Three traps that have each produced a wrong published number:

- **`results.txt` prints two PSM counts at 1% FDR and they differ.** The summary line
  (`All target PSMs with q-value <= 0.01`) is canonical. The FdrAnalysisEngine's line is higher and
  appears to include contaminants — and it repeats once per file, so a naive regex matches the wrong
  one. On one 18-file run: **26,582 against 27,958**, which are 9.98% and 10.5% of the same
  denominator and look like a rounding difference.
- **`AllQuantifiedProteinGroups.tsv` is written unfiltered.** Decoys, contaminants and rows above 1%
  FDR are all in it. On that same run, **2,229 rows against 1,652 groups at 1% FDR** — a row count
  overstates by about a third.
- **Per-file counts do not sum to the dataset count.** Each file's line comes from a separate per-file
  FDR calculation, so the subsets do not add up to the whole: **26,746 against 26,582**. A scan count
  *is* a partition and may be summed; a 1% FDR count is not.

Every number this pipeline reports carries a definition ID. See
[the definition register](provenance.md#the-definition-register), and when you quote a number, quote
its definition with it.
