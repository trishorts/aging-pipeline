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


def screen_text(h) -> str:
    """Every free-text field PRIDE gives for a project. The protocols alone are not enough: PXD015928's
    only mention of heavy-water labelling is in its project description (S46)."""
    parts = [getattr(h, "title", "") or "", getattr(h, "project_description", "") or "",
             getattr(h, "sample_processing_protocol", "") or "", getattr(h, "data_processing_protocol", "") or ""]
    for field in ("keywords", "experiment_types", "quantification_methods"):
        parts.extend(getattr(h, field, None) or [])
    return " ".join(parts)


# dataRepo's Enrichment vocabulary (0.16.0 added the four capture values; our dataRepo 046). ORDER MATTERS:
# the first kind whose pattern appears anywhere in the record wins. PTM enrichments come first because
# they are named for what is enriched. Proximity labelling precedes affinity purification because
# TurboID/BioID captures ARE streptavidin pulldowns, and a chemical probe precedes it for the same
# reason (PXD058611 captures its persulfide probe on streptavidin).
_ENRICHMENT_KINDS = (
    ("phospho", r"phospho(peptide)?[- ]?enrich|\btio2\b|\bimac\b|fe-nta"),
    ("ubiquitin_GG", r"\bk-?gg\b|di-?gly(cine)? remnant|ubiquitin remnant"),
    ("glyco", r"glyco(peptide)?[- ]?enrich|lectin"),
    ("proximity_labelling", r"\bturboid\b|\bbioid\b|\bapex2\b|proximity[- ]labell?ing"),
    ("chemical_probe", r"kinobead|chemical probe|activity-based probe|\bdcp-?bio"),
    ("immunoprecipitation", r"immunoprecipitat|\bco-?ip\b|\bip-ms\b|\blyso-?ip\b"),
    ("affinity_purification", r"affinity (purif|enrich|capture)|pull-?down|gfp-?trap|streptavidin|\bflag\b"),
)


def enrichment_kind(text: str) -> str:
    """Map a PRIDE record's text (`screen_text`, not the short evidence snippet) onto dataRepo's
    Enrichment vocabulary. The snippet is only 40 characters either side of the first match, which is
    too little: PXD058611's snippet says "streptavidin" while its probe is named elsewhere in the
    protocol. Returns `other` when the record is enriched in a way the vocabulary has no value for
    (an interactome, XL-MS, organelle isolation)."""
    t = text.lower()
    for kind, pattern in _ENRICHMENT_KINDS:
        if re.search(pattern, t):
            return kind
    return "other"


def screen(h, p: dict) -> tuple:
    """(reason, evidence) for a project, or ("", "") if nothing matched. `dia` and `labelled` EXCLUDE a
    project under v1 (label-free DDA only). `enriched` does NOT: an enrichment is searched and annotated
    (user, 2026-09-22: "I don't see why enrichment blocks organelle" -- a LAMP1-TurboID pulldown is a
    lysosome proteome). What an annotation prevents is reading an enrichment's intensities as whole-cell
    abundance or pooling them with whole proteomes, so it has to travel with the data (S44). The evidence
    is the matched text with context, so every decision can be audited and reversed."""
    text = screen_text(h)
    checks = [("dia", p.get("dia_patterns", [])),
              ("labelled", [*p.get("label_patterns", []), *p.get("metabolic_label_patterns", [])]),
              ("enriched", p.get("enrichment_patterns", []))]
    for reason, patterns in checks:
        if not patterns:
            continue
        m = re.search("|".join(patterns), text, re.IGNORECASE)
        if m:
            a, b = max(0, m.start() - 40), min(len(text), m.end() + 40)
            return reason, " ".join(text[a:b].split())
    return "", ""


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

    # `organism` accepts one name or several, and the match stays EXACT against the project's own
    # organism list. PRIDE carries the same species under more than one spelling -- "Mus musculus
    # (mouse)" on 448 aging hits and a bare "Mus musculus" on 47 more -- so a single string quietly
    # drops a tenth of the mouse corpus, and a substring rule would sweep in "Rattus rattus (black
    # rat)" alongside "Rattus norvegicus (rat)". A list of exact names is the only form that is both
    # complete and safe.
    wanted = p["organism"]
    wanted = [wanted] if isinstance(wanted, str) else list(wanted)

    rows = []
    for acc, (h, kws) in sorted(hits.items()):
        screened, evidence = screen(h, p)
        enriched = screened == "enriched"
        raw_files = [f for f in h.project_file_names if f.lower().endswith(".raw")]
        has_sdrf = any("sdrf" in f.lower() for f in h.project_file_names)
        reason = ""
        if not any(w in h.organisms for w in wanted):
            reason = "organism"
        elif screened and not enriched:
            # v1 is label-free whole-proteome DDA. PRIDE's quantification field and even curated SDRFs
            # miss labelling (PXD048658: TMT in the protocol, 'label free' in the SDRF), and a project
            # description can be the only place it is stated (PXD015928, heavy water), so every text
            # field is read.
            reason = screened
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
            "screen_evidence": evidence if (reason == screened or enriched) else "",
            "enrichment": enrichment_kind(screen_text(h)) if enriched else "none",
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
        "run_date": params["run_date"], "keywords": p["keywords"], "organism": wanted,
        "union_hits": len(rows),
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
