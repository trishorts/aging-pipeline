"""Stage 5 - build qc's `qc-payload/1` from a finished MetaMorpheus search.

qc owns the templates and the contract (`qc/design/CONTRACT.md`); this stage only supplies the
numbers. It writes ONE payload per dataset, which qc renders into tables, figures and a report:

    python -m qctemplates validate <payload.json>
    python -m qctemplates render   <payload.json> <out_dir>

**We supply values, never verdicts.** Gates, outliers and derived ratios (`id_rate`,
`mbr_msms_ratio`) are qc's to compute - the contract says so explicitly and supplying them here would
put the same rule in two places. Every value carries the definition ID it was computed under, so a
template renders a number it did not define and can say where the meaning came from.

Two shapes in the contract drive the code:

* `pg_missing_frac` is a per-file metric that needs the DATASET first (the share of the dataset's
  quantified protein groups absent from this file), so the build is two-pass.
* `id_rt_coverage` divides by run minutes, which is a stage-2b fact rather than a search fact, so the
  stage takes BOTH the search directory and the qc directory.

usage: qc_payload.py <params.json> <search_dir> <qc_dir> <out_dir> [accession]

`accession` may be omitted when the params file sets `fetch.accession`; older params leave it
null and pass it on the command line, exactly as fetch.py takes it.
"""
import bisect, csv, json, re, statistics, sys
from collections import Counter, defaultdict
from pathlib import Path

from provenance import Provenance
from search_mm import MBR_FDR_THRESHOLD, classify_peak

csv.field_size_limit(10_000_000)

SCHEMA = "qc-payload/1"

# The bin edges are qc's RULE, not our copy of a number (qc 007 §4, answering our 006). They own
# `qctemplates.spec.canonical_edges(key, run_minutes)`, so we call it when qctemplates is installed
# and fall back to a vendored copy when it is not.
#
# Why a fallback at all, rather than a hard dependency: qc has no git remote yet, so the only install
# source is a checkout or a hand-passed wheel, and D5 says an operator who is not us must be able to
# run this pipeline. A hard import would make a public pipeline undeployable to satisfy a histogram.
# A payload binned by the fallback still validates and still renders - qc said so explicitly - it
# just stops lining up with everyone else's figures, so the fallback ANNOUNCES ITSELF in
# `dataset.notes` and in provenance rather than passing silently.
_VENDORED_EDGES = {"precursor_ppm": (-10.0, 10.0, 0.5), "fragment_ppm": (-30.0, 30.0, 1.0)}
IDS_OVER_RT_BINS = 36

try:  # pragma: no cover - exercised by whichever half is installed
    from qctemplates.spec import canonical_edges as _canonical_edges
    EDGES_SOURCE = "qctemplates.spec.canonical_edges"
except ImportError:
    _canonical_edges = None
    EDGES_SOURCE = "vendored copy (qctemplates not installed)"


def _linear_edges(low, high, width):
    n = int(round((high - low) / width))
    return [round(low + i * width, 9) for i in range(n + 1)]


def edges_for(key: str, run_minutes=None):
    """qc's canonical edges for one distribution, or None when they are not defined."""
    if _canonical_edges is not None:
        return _canonical_edges(key, run_minutes)
    if key in _VENDORED_EDGES:
        return _linear_edges(*_VENDORED_EDGES[key])
    if key == "ids_over_rt":
        return _linear_edges(0.0, run_minutes, run_minutes / IDS_OVER_RT_BINS) if run_minutes else None
    raise KeyError(key)
# A metric is only as good as the definition it was computed under (D20, dataRepo U7).
#
# The three counts are MetaMorpheus's PER-FILE `results.txt` lines, and each of those comes from an
# FDR recomputed on that file's PSMs alone (PostSearchAnalysisTask at 1.1.11). They are therefore
# the `-RUN` definitions, never the dataset ones: a dataset count is not their sum (qc 010 QC-Q13).
DEFINITIONS = {
    "psms": {"id": "aging:DEF-PSM-1PCT-RUN", "version": "v1", "source": "pipeline/docs/provenance.md"},
    "peptides": {"id": "aging:DEF-PEPTIDE-1PCT-RUN", "version": "v1", "source": "pipeline/docs/provenance.md"},
    "protein_groups": {"id": "aging:DEF-PROTEINGROUP-1PCT-RUN", "version": "v1", "source": "pipeline/docs/provenance.md"},
    "ms2_scans": {"id": "aging:DEF-MS2", "version": "v1", "source": "pipeline/docs/provenance.md"},
    "msms_peaks": {"id": "QuantProject:DEF-QC-MBR", "version": "v1", "source": "QuantProject/design/DATA-DEFINITIONS.md"},
    "mbr_kept": {"id": "QuantProject:DEF-MBR-KEPT", "version": "v1", "source": "QuantProject/design/DATA-DEFINITIONS.md"},
    # `pg_missing_frac` is DEF-QC-13's `_msms` variant (see parse_protein_groups). qc's schema forbids
    # extra keys on a definition entry, so the variant is named in `source` - the one free field whose
    # job is already "where the meaning is written" - and stated again in `dataset.notes`, which their
    # report prints verbatim. A number whose variant is not on the page is a number nobody can check.
    "pg_missing_frac": {"id": "QuantProject:DEF-QC-13", "version": "v1",
                        "source": "QuantProject/design/DATA-DEFINITIONS.md - the _msms variant "
                                  "(SpectralCount_ > 0), per file"},
    # NOT `aging:DEF-CONTAM-PSM v1`, which our own register defines at DATASET grain. A per-file
    # contaminant share is a different quantity at a different grain, and the register's own rule is
    # that a number is stored at the grain it was measured at, never coarser and never finer. Pushing
    # a dataset definition down to a file is the same error as rolling a run definition up, which we
    # have already told qc and QuantProject we would not do. Raised with qc in our 009.
    "contaminant_psm_share": {"id": "aging:DEF-CONTAM-PSM-RUN", "version": "v2",
                              "source": "pipeline/docs/provenance.md"},
    "contaminant_intensity_frac": {"id": "QuantProject:DEF-QC-9", "version": "v2",
                                   "source": "QuantProject/design/DATA-DEFINITIONS.md"},
}


# Every extension the search outputs name a run by. `.toml` matters as much as `.raw`: the
# calibration report is `<file>-calib.toml`, and leaving it on makes `calibration_ok` false for a
# run that calibrated perfectly well, because the key never matches the one from `results.txt`.
# `Path.stem` is deliberately not used - a run name may contain a dot, and stem would eat it.
_EXTS = (".raw", ".mzml", ".toml", ".mzxml")


def stem(name: str) -> str:
    """The contract's file key: no extension, no MetaMorpheus `-calib` suffix."""
    n = Path(str(name)).name
    for ext in _EXTS:
        if n.lower().endswith(ext):
            n = n[: -len(ext)]
            break
    return n[: -len("-calib")] if n.endswith("-calib") else n


def num(v):
    """A float, or None. MetaMorpheus writes ambiguous cells as `a|b`; there is no single value for
    those, so they are dropped rather than guessed (the same rule that S22 turned on)."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or "|" in s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return None if f != f else f


def histogram(values, edges):
    """`{"edges": [...], "counts": [...]}` with len(edges) == len(counts) + 1, values CLIPPED into
    the range as the contract's fixtures do - a tail outside the axis is still a count, and dropping
    it would quietly change the total.

    Takes the edges rather than (lo, hi, width) so that qc's `canonical_edges` is the only place the
    binning rule lives. `bisect` rather than arithmetic because edges we did not compute need not be
    uniform, and a future non-uniform rule should not silently mis-bin here.
    """
    lo, hi = edges[0], edges[-1]
    counts = [0] * (len(edges) - 1)
    for v in values:
        i = bisect.bisect_right(edges, min(max(v, lo), hi)) - 1
        counts[min(max(i, 0), len(counts) - 1)] += 1
    return {"edges": list(edges), "counts": counts}


def iqr(xs):
    if len(xs) < 2:
        return None
    q = statistics.quantiles(xs, n=4, method="inclusive")
    return round(q[2] - q[0], 4)


def parse_results_txt(path: Path):
    """Per-file counts from `results.txt`. These lines come from a SEPARATE per-file FDR calculation
    and must never be summed to a dataset figure (our 008 to dataRepo, their 018 §3)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    out = defaultdict(dict)
    for key, pat in (("ms2_scans", r"^(.+?) - MS2 Scans: (\d+)$"),
                     ("psms", r"^(.+?) - Target PSMs with q-value <= 0\.01: (\d+)$"),
                     ("peptides", r"^(.+?) - Target peptides with q-value <= 0\.01: (\d+)$"),
                     ("protein_groups", r"^(.+?) - Target protein groups with q-value <= 0\.01: (\d+)$")):
        for name, n in re.findall(pat, text, re.M):
            out[stem(name)][key] = int(n)
    return out


def parse_calibration(cal_dir: Path):
    """`calibration_ok` keys on the `-calib.toml` existing, which MetaMorpheus writes only on success.

    The tolerances are NOT numbers: the value is the string `"±3.1000 PPM"` - a U+00B1, the number, a
    space, a unit. The file is UTF-8, and on Windows a read without an explicit encoding turns the
    sign into mojibake, so the encoding is stated and the number is matched rather than the sign
    (reported to qc as 006 §3).
    """
    out = {}
    if not cal_dir.is_dir():
        return out
    for toml in cal_dir.glob("*-calib.toml"):
        body = toml.read_text(encoding="utf-8", errors="replace")
        rec = {"calibration_ok": True}
        for key, field in (("cal_precursor_tol_ppm", "PrecursorMassTolerance"),
                           ("cal_product_tol_ppm", "ProductMassTolerance")):
            m = re.search(rf"^{field}\s*=\s*\"[^0-9-]*(-?[0-9.]+)", body, re.M)
            if m:
                rec[key] = float(m.group(1))
        out[stem(toml.name)] = rec
    return out


def accepted_1pct(row) -> bool:
    """The whole-search 1% set, as far as a file-side test can reproduce it (`aging:DEF-PSM-1PCT-INFILE v1`).

    MetaMorpheus's q-value filter is `QValue <= t AND QValueNotch <= t` (`FilteredPsms` at 1.1.11), and
    an ambiguous `Notch` fails in memory while the TSV prints the best hypothesis's notch q-value (S22),
    so those rows are excluded too. `AllPSMs.psmtsv` is written BEFORE the per-file FDR recalculation,
    so these are whole-search q-values: the population is the dataset's accepted PSMs that came from
    this file, not the file's own per-file FDR set (which is what the `psms` count line reports).
    """
    q, qn = num(row.get("QValue")), num(row.get("QValue Notch"))
    notch = (row.get("Notch") or "").strip()
    return q is not None and qn is not None and q <= 0.01 and qn <= 0.01 and "|" not in notch


def parse_psms(path: Path, run_minutes: dict):
    """Per-file PSM metrics and distributions, over `accepted_1pct` target PSMs.

    The contract restricts the mass-error metrics to `Notch 0` - an isotope-error PSM's precursor
    error is offset by a neutron and would smear the distribution that exists to show calibration.
    """
    per = defaultdict(lambda: {"charges": Counter(), "missed": [0, 0], "notch": [0, 0],
                               "prec": [], "frag": [], "rts": [], "contam": [0, 0]})
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        if "QValue Notch" not in (reader.fieldnames or []):
            # Without it the population silently widens to QValue alone, which is the v1 behaviour
            # qc 010 QC-Q12 caught. Refuse rather than describe a different set of PSMs.
            sys.exit(f"{path} has no `QValue Notch` column: cannot apply the 1% filter MetaMorpheus uses")
        for row in reader:
            td = (row.get("Decoy/Contaminant/Target") or "").strip()
            if not accepted_1pct(row):
                continue
            # M13 counts the contaminant share BEFORE the target-only filter, over the same
            # population, because its denominator is target + contaminant. An ambiguous `C|T`
            # counts as not-contaminant and stays in the denominator.
            if td and "D" not in td.upper():
                cf = per[stem(row.get("File Name"))]
                cf["contam"][0] += td == "C"
                cf["contam"][1] += 1
            if td != "T":
                continue
            f = per[stem(row.get("File Name"))]
            ch = num(row.get("Precursor Charge"))
            if ch is not None:
                f["charges"][int(ch)] += 1
            mc = num(row.get("Missed Cleavages"))
            if mc is not None:
                f["missed"][0] += mc >= 1
                f["missed"][1] += 1
            notch = (row.get("Notch") or "").strip()
            if notch and "|" not in notch:
                f["notch"][0] += notch != "0"
                f["notch"][1] += 1
            rt = num(row.get("Scan Retention Time"))
            if rt is not None:
                f["rts"].append(rt)
            if notch != "0":
                continue
            p = num(row.get("Mass Diff (ppm)"))
            if p is not None:
                f["prec"].append(p)
            # "[b2+1:1.03, b3+1:-2.01, ...]" - every matched ion of this PSM.
            f["frag"].extend(float(x) for x in re.findall(r":\s*(-?\d+\.?\d*)", row.get("Matched Ion Mass Diff (Ppm)") or ""))

    metrics, dists = {}, {}
    for name, f in per.items():
        total = sum(f["charges"].values())
        m = {}
        if total:
            m["charge_1_frac"] = round(f["charges"][1] / total, 4)
            m["charge_2_frac"] = round(f["charges"][2] / total, 4)
            m["charge_3_frac"] = round(f["charges"][3] / total, 4)
            m["charge_4plus_frac"] = round(sum(n for c, n in f["charges"].items() if c >= 4) / total, 4)
        if f["missed"][1]:
            m["missed_cleavage_frac"] = round(f["missed"][0] / f["missed"][1], 4)
        if f["notch"][1]:
            m["notch_frac"] = round(f["notch"][0] / f["notch"][1], 4)
        if f["contam"][1]:
            m["contaminant_psm_share"] = round(f["contam"][0] / f["contam"][1], 4)
        if f["prec"]:
            m["precursor_ppm_median"] = round(statistics.median(f["prec"]), 4)
            m["precursor_ppm_iqr"] = iqr(f["prec"])
        if f["frag"]:
            m["fragment_ppm_median"] = round(statistics.median(f["frag"]), 4)
            m["fragment_ppm_iqr"] = iqr(f["frag"])
        minutes = run_minutes.get(name)
        if f["rts"] and minutes:
            # Nearest-rank percentiles. Truncating `int(0.99 * (n - 1))` collapses towards the
            # median on a small n - on three IDs it returns the middle one as the "99th", which
            # understated the coverage of a two-ID span by a factor of nine in testing.
            rts = sorted(f["rts"])
            n = len(rts)
            lo = rts[max(0, -(-1 * n // 100) - 1)]
            hi = rts[min(n - 1, -(-99 * n // 100) - 1)]
            m["id_rt_coverage"] = round(max(0.0, (hi - lo) / minutes), 4)
        metrics[name] = m
        d = {"precursor_ppm": histogram(f["prec"], edges_for("precursor_ppm")),
             "fragment_ppm": histogram(f["frag"], edges_for("fragment_ppm"))}
        if minutes:
            d["ids_over_rt"] = histogram(f["rts"], edges_for("ids_over_rt", minutes))
            d["run_minutes"] = minutes
        dists[name] = d
    return metrics, dists


def parse_peaks(path: Path):
    """`msms_peaks` and `mbr_kept` per file, through the one shared DEF-MBR-KEPT implementation."""
    per = defaultdict(lambda: {"msms_peaks": 0, "mbr_kept": 0})
    if not path.exists():
        return per
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            kind, _ = classify_peak(row, MBR_FDR_THRESHOLD)
            if kind is None:
                continue
            # Touch the file even for a peak that is neither MS/MS nor a kept transfer. A file whose
            # every MBR candidate was rejected has `mbr_kept` of ZERO, and qc renders an absent
            # value as a dash - "no transfers survived" and "not measured" are different facts.
            f = per[stem(row.get("File Name"))]
            if kind == "msms":
                f["msms_peaks"] += 1
            elif kind == "mbr_kept":
                f["mbr_kept"] += 1
    return per


def parse_protein_groups(path: Path):
    """Pass one of two: the dataset's quantified protein groups, which files each appears in, and
    the per-file contaminant intensity fraction.

    **Presence is the `_msms` variant of `DEF-QC-13`**: a group counts as present in a file when its
    `SpectralCount_<file>` is > 0, i.e. the file identified it itself. The `_any` variant (an
    intensity, which MBR can transfer from a neighbouring file) is deliberately NOT what we send:
    MBR is on in every run by user rule, so `_any` for one file is a function of the OTHER files in
    the run, and a number like that is not a property of the file it is filed under. qc 008 §QC-Q9
    carries the argument; `mbr_kept` (M10) is where the transfer contribution is visible instead.

    For presence, only groups at 1% FDR and not decoy are counted (the contaminant intensity fraction
    is not filtered by FDR; see DEF-QC-9 below) - the same predicate as `DEF-PROTEINGROUP-1PCT`,
    which INCLUDES contaminant groups, so this is a completeness measure of what the search
    reported, not a biological one. The file is written UNFILTERED (decoys, contaminants and
    q > 0.01 are all in it), so the predicate is load-bearing rather than defensive: on the 18-file
    PXD036557 run it is 1,652 groups out of 2,229 rows.
    """
    if not path.exists():
        return None, {}, {}, {}
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        fields = reader.fieldnames or []
        counts = {c: stem(c[len("SpectralCount_"):]) for c in fields if c.startswith("SpectralCount_")}
        inten = {c: stem(c[len("Intensity_"):]) for c in fields if c.startswith("Intensity_")}
        present = defaultdict(int)
        runs_per = Counter()
        contam_int = defaultdict(float)
        total_int = defaultdict(float)
        quantified = 0
        # A sample group's column block is 2, 3 or 4 columns wide depending on four different
        # conditions (QuantProject via qc 007 §7.3), so `SpectralCount_` can be absent from a real
        # file. When it is, the `_msms` variant is NOT MEASURABLE - and qc's QC-Q8 rule is that
        # absent means "not measured" while 0 means "measured, and it was zero". Returning a
        # quantified count of 0 here would make every file look 100% complete against an empty
        # denominator, or 100% missing, depending on which way the arithmetic fell. Refuse instead.
        if not counts:
            return None, {}, {}, {}
        for row in reader:
            td = (row.get("Protein Decoy/Contaminant/Target") or "").strip().upper()
            if td.startswith("D"):
                continue
            # QuantProject:DEF-QC-9 v2, run grain: contaminant / (target + contaminant) apex
            # intensity, per file, over EVERY C and T row of the protein table. The definition states
            # no protein-FDR filter, so none is applied here: filtering to 1% first made this 19.1%
            # where the provenance block, which follows the text, said 18.92% for the same file
            # (qc 010 QC-Q15). Whether a filter belongs in it is QuantProject's call.
            # A not-quantified protein cell is BLANK at MM 1.1.9+, not `0` (QuantProject via qc 007
            # §7.2), so `num()` returning None reads as absent and contributes nothing.
            for c, f in inten.items():
                v = num(row.get(c))
                if v is None or v <= 0:
                    continue
                total_int[f] += v
                if td == "C":
                    contam_int[f] += v
            q = num(row.get("Protein QValue"))
            if q is None or q > 0.01:
                continue
            hits = [f for c, f in counts.items() if (num(row.get(c)) or 0) > 0]
            if not hits:
                continue
            quantified += 1
            runs_per[len(hits)] += 1
            for f in hits:
                present[f] += 1
        contam = {f: round(contam_int.get(f, 0.0) / t, 4) for f, t in total_int.items() if t > 0}
        return quantified, present, {str(k): v for k, v in sorted(runs_per.items())}, contam


def main(params_path: str, search_dir: str, qc_dir: str, out_dir: str, accession: str = "") -> None:
    params = json.loads(Path(params_path).read_text(encoding="utf-8"))
    search, out = Path(search_dir), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    prov = Provenance("qc_payload", params_path, "qc")

    task = sorted((search / "mm").glob("Task*SearchTask"))
    if not task:
        sys.exit(f"no Task*SearchTask under {search / 'mm'}: run search_mm.py first")
    task = task[-1]
    prov.upstream(search / "provenance.json")

    qc_report = Path(qc_dir) / "qc_report.json"
    if not qc_report.exists():
        sys.exit(f"no QC report at {qc_report}: run qc_spectra.py first (it carries run_minutes)")
    report = json.loads(qc_report.read_text(encoding="utf-8"))
    run_minutes = {stem(k): v.get("run_minutes") for k, v in report.items()}
    prov.inputs(qc_report)

    results = task / "results.txt"
    prov.inputs(results)
    counts = parse_results_txt(results)
    calib = parse_calibration(search / "mm" / "Task1CalibrationTask")
    psm_file = task / "AllPSMs.psmtsv"
    prov.inputs(psm_file)
    psm_metrics, dists = parse_psms(psm_file, run_minutes)
    peaks = parse_peaks(task / "AllQuantifiedPeaks.tsv")
    quantified, present, runs_per, contam_int = parse_protein_groups(task / "AllQuantifiedProteinGroups.tsv")

    names = sorted(set(counts) | set(psm_metrics) | set(run_minutes))
    files = []
    for name in names:
        m = {**counts.get(name, {}), **calib.get(name, {"calibration_ok": False}),
             **psm_metrics.get(name, {}), **peaks.get(name, {})}
        # Pass two: per-file metrics that needed the dataset first.
        if quantified:
            m["pg_missing_frac"] = round((quantified - present.get(name, 0)) / quantified, 4)
        # else: omitted, not zeroed. `pg_missing_frac` absent means the run could not measure it.
        if name in contam_int:
            m["contaminant_intensity_frac"] = contam_int[name]
        files.append({"file": name, "metrics": m, "distributions": dists.get(name, {})})

    sp = params.get("search", {})
    accession = accession or params.get("fetch", {}).get("accession")
    if not accession:
        # qc's schema requires a string, and an unnamed payload is useless the moment two datasets
        # exist. Fail here rather than emit a null and have their validator explain it. Older params
        # files leave `fetch.accession` null on purpose and pass it on the command line, as fetch.py
        # does, so the argument is how those runs are described.
        sys.exit(f"no accession: pass it as the 5th argument, or set fetch.accession in {params_path}")
    payload = {
        "schema": SCHEMA,
        "dataset": {
            "accession": accession,
            "run_label": f"{params.get('run_date', '')}/{params.get('fetch', {}).get('accession', '')}".strip("/"),
            "search_engine": {"name": "MetaMorpheus", "version": sp.get("metamorpheus_version")},
            "notes": [],
        },
        "files": files,
        "definitions": DEFINITIONS,
        "dataset_metrics": {"protein_groups_quantified": quantified,
                            "runs_per_protein_group": runs_per},
    }
    # Facts a reader of the rendered report cannot recover from the numbers, so they are stated
    # rather than left to be inferred. They go in `notes`, which qc prints verbatim above the tiles.
    payload["dataset"]["notes"].append(
        "Two PSM populations appear per file, on purpose (qc 010 QC-Q12). The psms / peptides / "
        "protein_groups counts are MetaMorpheus's per-file lines, each from an FDR recomputed on that "
        "file alone (the -RUN definitions). The PSM-derived metrics and distributions describe the "
        "WHOLE-SEARCH 1% set restricted to the file: QValue <= 0.01 and QValue Notch <= 0.01 at "
        "whole-search q, unambiguous notch (aging:DEF-PSM-1PCT-INFILE v1). Neither is a sum of the other.")
    if quantified:
        payload["dataset"]["notes"].append(
            "pg_missing_frac is DEF-QC-13's _msms variant (SpectralCount_ > 0), not _any: MBR is on "
            "in every run, so an intensity-based completeness for one file would depend on the "
            "other files in the run. See mbr_kept for the transfer contribution.")
    else:
        payload["dataset"]["notes"].append(
            "pg_missing_frac is NOT REPORTED for this run: the protein-group table carries no "
            "SpectralCount_ columns, so the _msms variant is not measurable. Absent means not "
            "measured, not zero.")
    if _canonical_edges is None:
        payload["dataset"]["notes"].append(
            "Histogram bins came from a VENDORED copy of qc's canonical edges, because qctemplates "
            "is not installed here. The payload is valid and renders, but its figures may not line "
            "up bin-for-bin with reports built where qctemplates is installed.")

    # An acquisition exception is a limit on what the numbers may be used for, so it travels with
    # them rather than living in the run that produced them (D24).
    exc = params.get("qc", {}).get("acquisition_exception")
    if exc:
        payload["dataset"]["notes"].append(
            f"Acquisition exception ({exc.get('granted_by', '?')}, {exc.get('granted_date', '?')}): "
            f"waives {exc.get('waives')}; results restricted to {exc.get('restricts_to')}; "
            f"NOT for {exc.get('bars')}.")

    dest = out / "qc_payload.json"
    dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    prov.outputs(dest)
    prov.rec["files_described"] = len(files)
    prov.rec["bin_edges_source"] = EDGES_SOURCE
    prov.write(out)
    print(json.dumps({"payload": str(dest), "files": len(files),
                      "protein_groups_quantified": quantified}, indent=2))


if __name__ == "__main__":
    main(*sys.argv[1:6])
