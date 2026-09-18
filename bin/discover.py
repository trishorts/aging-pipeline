"""Stage 1 - discover candidate aging datasets in PRIDE and FREEZE the list.

Glue only (aging D1): every PRIDE call goes through pymzlib (mzLib's PrideArchiveClient).
The recipe comes from pride thread 002: search several keywords, take the union, then filter
client-side. PRIDE search has no reliable server-side filter yet (REQ-PRIDE-4), and results drift
between runs, so the output is a dated, frozen TSV. Later stages read only that file (pride 003 D5-b).

The relevance and DDA filters are aging-owned rules (pride 003 D4-1). They are deliberately simple
and conservative. Each dropped project records its reason, so an operator can audit the filter.

usage: discover.py <params.json> <out_dir>
"""
import csv, json, re, sys
from pathlib import Path

import pymzlib.pride as pride
from provenance import Provenance, pymzlib_tool


def main(params_path: str, out_dir: str) -> None:
    params = json.loads(Path(params_path).read_text(encoding="utf-8"))
    p = params["discover"]
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    prov = Provenance("discover", params_path, "discover"); pymzlib_tool(prov)
    prov.note("PRIDE search is a live index: re-running this stage is NOT reproducible; later stages read the frozen TSV only.")

    hits = {}
    for kw in p["keywords"]:
        prov.command(["pymzlib.pride.search", kw])
        for h in pride.search(kw, timeout=p["timeout_s"]):
            hits.setdefault(h.accession, (h, set()))[1].add(kw)

    dia = re.compile("|".join(p["dia_patterns"]), re.IGNORECASE)
    label = re.compile("|".join(p["label_patterns"]), re.IGNORECASE)
    only = re.compile("|".join(map(re.escape, p["orbitrap_ms2_only_patterns"])), re.IGNORECASE)
    hybrid = re.compile("|".join(map(re.escape, p["hybrid_patterns"])), re.IGNORECASE)

    def ms2_class(instruments):
        """User rule: prefer high-res HCD read in the Orbitrap. Q Exactive/Exploris can only do that;
        hybrids MAY read MS2 in the ion trap, so qc_spectra.py checks the file; a plain LTQ can't."""
        if any(only.search(i) for i in instruments):
            return "orbitrap_hcd_only"
        if any(hybrid.search(i) or "orbitrap" in i.lower() for i in instruments):
            return "check_ms2"
        return "low_res"
    thermo = re.compile("|".join(re.escape(s) for s in p["thermo_instrument_patterns"]), re.IGNORECASE)

    rows = []
    for acc, (h, kws) in sorted(hits.items()):
        text = " ".join([*h.experiment_types, h.sample_processing_protocol or "", h.data_processing_protocol or ""])
        raw_files = [f for f in h.project_file_names if f.lower().endswith(".raw")]
        has_sdrf = any("sdrf" in f.lower() for f in h.project_file_names)
        reason = ""
        if p["organism"] not in h.organisms:
            reason = "organism"
        elif dia.search(text):
            reason = "dia"
        elif label.search(text):
            # v1 is label-free. PRIDE's quantification field and even curated SDRFs can miss labelling
            # (PXD048658: TMT in the protocol, 'label free' in the community SDRF), so read the protocol.
            reason = "labelled"
        elif not any(thermo.search(i) for i in h.instruments):
            reason = "not_thermo"
        elif ms2_class(h.instruments) == "low_res":
            reason = "low_res_instrument"
        elif not raw_files:
            reason = "no_raw_listed"
        elif p["require_sdrf_file"] and not has_sdrf:
            reason = "no_sdrf"
        rows.append({
            "accession": acc, "keep": "yes" if not reason else "no", "drop_reason": reason,
            "keywords_hit": ";".join(sorted(kws)), "has_sdrf_file": has_sdrf,
            "n_raw_listed": len(raw_files), "ms2_class": ms2_class(h.instruments),
            "instruments": ";".join(h.instruments),
            "organism_parts": ";".join(h.organism_parts), "experiment_types": ";".join(h.experiment_types),
            "submission_type": h.submission_type, "title": h.title.replace("\t", " "),
        })

    frozen = out / f"candidates_{params['run_date']}.tsv"
    with frozen.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter="\t"); w.writeheader(); w.writerows(rows)

    kept = [r for r in rows if r["keep"] == "yes"]
    summary = {
        "run_date": params["run_date"], "keywords": p["keywords"], "union_hits": len(rows),
        "kept": len(kept), "kept_with_sdrf_file": sum(1 for r in kept if r["has_sdrf_file"]),
        "dropped_by_reason": {k: sum(1 for r in rows if r["drop_reason"] == k)
                              for k in sorted({r["drop_reason"] for r in rows if r["drop_reason"]})},
        "bridge_version": __import__("pymzlib").bridge_version(),
    }
    (out / "discover_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    prov.outputs(frozen, out / "discover_summary.json"); prov.write(out)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main(*sys.argv[1:3])
