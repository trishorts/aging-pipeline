"""Stage 2b - spectra QC: keep only high-res HCD with MS2 read in the Orbitrap (user rule, v1).

The instrument name is not enough. Hybrids (Velos, Elite, Fusion, Lumos, Eclipse) can read MS2 in the
ion trap with CID, so we check the file itself. Scan HEADERS are read through mzLib (pymzlib.readers),
never re-parsed here. The QC also reports the facts that caught PXD048658 early (MS2 count, run
length, charge states).

A file PASSES when at least `min_fraction` of its MS2 scans are HCD with an Orbitrap analyzer, and it
has at least `min_ms2` MS2 scans (the smallest file is often a blank). search_mm.py refuses a file
that failed.

usage: qc_spectra.py <params.json> <spectra_dir> <out_dir>
"""
import collections, json, sys
from pathlib import Path

import pymzlib.readers as readers
from provenance import Provenance, pymzlib_tool


def main(params_path: str, spectra_dir: str, out_dir: str) -> None:
    q = json.loads(Path(params_path).read_text(encoding="utf-8"))["qc"]
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    prov = Provenance("qc_spectra", params_path, "qc"); pymzlib_tool(prov)
    prov.upstream(Path(spectra_dir).parent / "provenance.json")          # fetch

    report = {}
    for f in sorted(Path(spectra_dir).glob("*.raw")):
        prov.command(["pymzlib.readers.read_spectra", f.name, "peaks=False"])
        s = readers.read_spectra(f, timeout=q["timeout_s"]); c = s.columns
        ms2 = [i for i, o in enumerate(c["ms_order"]) if o == 2]
        pair = collections.Counter((c["mz_analyzer"][i], c["dissociation_type"][i]) for i in ms2)
        hi = pair.get(("Orbitrap", "HCD"), 0)
        frac = hi / len(ms2) if ms2 else 0.0
        passed = frac >= q["min_fraction_orbitrap_hcd"] and len(ms2) >= q["min_ms2"]
        report[f.name] = {
            "pass": passed, "scans": s.scan_count, "ms2": len(ms2),
            "fraction_orbitrap_hcd": round(frac, 4),
            "ms2_analyzer_dissociation": {f"{a}/{d}": n for (a, d), n in pair.most_common()},
            "run_minutes": round(max(c["retention_time"]) if c["retention_time"] else 0, 2),
            "charge_states": dict(collections.Counter(c["selected_ion_charge_state_guess"][i] for i in ms2).most_common(6)),
        }
        prov.inputs(f)

    (out / "qc_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    prov.rec["all_pass"] = all(r["pass"] for r in report.values()) and bool(report)
    prov.outputs(out / "qc_report.json"); prov.write(out)
    print(json.dumps(report, indent=2))
    sys.exit(0 if prov.rec["all_pass"] else 2)


if __name__ == "__main__":
    main(*sys.argv[1:4])
