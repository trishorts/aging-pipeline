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
import csv, json, re, statistics, sys
from collections import Counter, defaultdict
from pathlib import Path

from provenance import Provenance
from search_mm import MBR_FDR_THRESHOLD, classify_peak

csv.field_size_limit(10_000_000)

SCHEMA = "qc-payload/1"
# Bin edges copied from qc's own fixtures so our reports are comparable with theirs (qc 006 asks
# whether these are a rule or an example; until they say, we match them exactly).
PRECURSOR_BINS = [(-10.0, 10.0), 0.5]
FRAGMENT_BINS = [(-30.0, 30.0), 1.0]
IDS_OVER_RT_BINS = 36
# A metric is only as good as the definition it was computed under (D20, dataRepo U7).
DEFINITIONS = {
    "psms": {"id": "aging:DEF-PSM-1PCT", "version": "v1", "source": "pipeline/docs/provenance.md"},
    "peptides": {"id": "aging:DEF-PEPTIDE-1PCT", "version": "v1", "source": "pipeline/docs/provenance.md"},
    "protein_groups": {"id": "aging:DEF-PROTEINGROUP-1PCT", "version": "v1", "source": "pipeline/docs/provenance.md"},
    "ms2_scans": {"id": "aging:DEF-MS2", "version": "v1", "source": "pipeline/docs/provenance.md"},
    "msms_peaks": {"id": "QuantProject:DEF-QC-MBR", "version": "v1", "source": "QuantProject/design/DATA-DEFINITIONS.md"},
    "mbr_kept": {"id": "QuantProject:DEF-MBR-KEPT", "version": "v1", "source": "QuantProject/design/DATA-DEFINITIONS.md"},
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


def histogram(values, lo, hi, width):
    """`{"edges": [...], "counts": [...]}` with len(edges) == len(counts) + 1, values CLIPPED into
    the range as the contract's fixtures do - a tail outside the axis is still a count, and dropping
    it would quietly change the total."""
    n = int(round((hi - lo) / width))
    edges = [round(lo + i * width, 10) for i in range(n + 1)]
    counts = [0] * n
    for v in values:
        i = int((min(max(v, lo), hi) - lo) / width)
        counts[min(i, n - 1)] += 1
    return {"edges": edges, "counts": counts}


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


def parse_psms(path: Path, run_minutes: dict):
    """Per-file PSM metrics and distributions, over target PSMs at 1% FDR.

    The contract restricts the mass-error metrics to `Notch 0` - an isotope-error PSM's precursor
    error is offset by a neutron and would smear the distribution that exists to show calibration.
    """
    per = defaultdict(lambda: {"charges": Counter(), "missed": [0, 0], "notch": [0, 0],
                               "prec": [], "frag": [], "rts": []})
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            if (row.get("Decoy/Contaminant/Target") or "").strip() != "T":
                continue
            q = num(row.get("QValue"))
            if q is None or q > 0.01:
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
        d = {"precursor_ppm": histogram(f["prec"], *PRECURSOR_BINS[0], PRECURSOR_BINS[1]),
             "fragment_ppm": histogram(f["frag"], *FRAGMENT_BINS[0], FRAGMENT_BINS[1])}
        if minutes:
            d["ids_over_rt"] = histogram(f["rts"], 0.0, minutes, minutes / IDS_OVER_RT_BINS)
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
    """Pass one of two: the dataset's quantified protein groups, and which files each appears in.

    A group counts as quantified in a file when its `Intensity_<file>` is > 0. Only groups at 1% FDR
    and not decoy are counted - the same predicate as `DEF-PROTEINGROUP-1PCT`, which INCLUDES
    contaminant groups, so this is a completeness measure of what the search reported, not a
    biological one.
    """
    if not path.exists():
        return None, {}, {}
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        cols = {c: stem(c[len("Intensity_"):]) for c in (reader.fieldnames or []) if c.startswith("Intensity_")}
        present = defaultdict(int)
        runs_per = Counter()
        quantified = 0
        for row in reader:
            if (row.get("Protein Decoy/Contaminant/Target") or "").strip().upper().startswith("D"):
                continue
            q = num(row.get("Protein QValue"))
            if q is None or q > 0.01:
                continue
            hits = [f for c, f in cols.items() if (num(row.get(c)) or 0) > 0]
            if not hits:
                continue
            quantified += 1
            runs_per[len(hits)] += 1
            for f in hits:
                present[f] += 1
        return quantified, present, {str(k): v for k, v in sorted(runs_per.items())}


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
    quantified, present, runs_per = parse_protein_groups(task / "AllQuantifiedProteinGroups.tsv")

    names = sorted(set(counts) | set(psm_metrics) | set(run_minutes))
    files = []
    for name in names:
        m = {**counts.get(name, {}), **calib.get(name, {"calibration_ok": False}),
             **psm_metrics.get(name, {}), **peaks.get(name, {})}
        # Pass two: a per-file metric that needed the dataset first.
        if quantified:
            m["pg_missing_frac"] = round((quantified - present.get(name, 0)) / quantified, 4)
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
    prov.write(out)
    print(json.dumps({"payload": str(dest), "files": len(files),
                      "protein_groups_quantified": quantified}, indent=2))


if __name__ == "__main__":
    main(*sys.argv[1:6])
