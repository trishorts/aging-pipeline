"""Re-derive an existing search stage's provenance under today's metric definitions.

A `provenance.json` records two different kinds of thing:

  * **what happened** - the commands, the tool versions and hashes, the wall clock, the CPU and memory,
    the input and output digests. This is history. It is true forever and nothing may rewrite it.
  * **what the numbers mean** - `id_rate`, `mbr`, `contamination`, and the suspicion `flags` derived from
    them. These are *interpretations* of files that are still sitting on disk, under definitions that
    carry version numbers and do move.

When a definition is corrected, only the second kind is stale. The 18-file PXD036557 run is the case
this tool was written for: its provenance is `aging-provenance/2`, where `id_rate.psms_1pct` held the
FDR engine's count (27,958) rather than `aging DEF-PSM-1PCT v1` (26,582) - see S21 and S23 - and the
contamination block did not exist at all, although the numbers behind it were already in
`AllPSMs.psmtsv` and `AllQuantifiedProteinGroups.tsv`.

The alternative to this tool is re-running MetaMorpheus over 18 raw files for 21 minutes to change a
label on numbers it would recompute identically. That is compute spent to avoid writing a tool, and
D4 is explicit that a step the pipeline cannot do is a gap to close, not a manual workaround to
tolerate. So: re-derive, never re-search.

**What this tool will not do.** It never edits the historical half of the record, never invents a value
it cannot recompute from files on disk, and never rewrites silently - every run appends a `rederived`
entry naming what changed, from what, to what, and under which pipeline commit. A provenance file that
had been quietly corrected would be worse than one that was quietly wrong.

usage: reprovenance.py <params.json> <stage_out_dir> [<spectra_dir>]
"""
import datetime, json, sys
from pathlib import Path

from provenance import pipeline_commit, pipeline_version
from search_mm import derive_metrics

SCHEMA = "aging-provenance/3"
# The blocks this tool owns. Everything else in the record is history and is left untouched.
DERIVED = ("id_rate", "mbr", "contamination", "flags")


def spectra_from_record(rec: dict) -> list:
    """Recover the searched raw files from the record's own inputs, so the caller need not remember."""
    root = Path(rec.get("roots", {}).get("work_root") or ".")
    out = []
    for e in rec.get("inputs", []):
        path = e.get("path", "")
        if path.lower().endswith((".raw", ".mzml")):
            out.append((root / path) if e.get("root") == "work_root" else Path(path))
    return sorted(out)


def changes(before: dict, after: dict) -> dict:
    """What actually moved, block by block, so the appended note is specific rather than 'recomputed'."""
    diff = {}
    for k in DERIVED:
        b, a = before.get(k), after.get(k)
        if b == a:
            continue
        if k == "flags":
            diff["flags"] = {"removed": [f for f in (b or []) if f not in (a or [])],
                             "added": [f for f in (a or []) if f not in (b or [])]}
        elif b is None:
            diff[k] = "added (absent before)"
        elif isinstance(b, dict) and isinstance(a, dict):
            diff[k] = {f: {"was": b.get(f), "now": a.get(f)}
                       for f in sorted(set(b) | set(a)) if b.get(f) != a.get(f)}
        else:
            diff[k] = {"was": b, "now": a}
    return diff


def main(params_path: str, out_dir: str, spectra: str = None) -> None:
    out = Path(out_dir)
    pj = out / "provenance.json"
    if not pj.exists():
        sys.exit(f"no provenance.json in {out}: nothing to re-derive")
    rec = json.loads(pj.read_text(encoding="utf-8"))
    if rec.get("stage") != "search_metamorpheus":
        sys.exit(f"{pj} is stage {rec.get('stage')!r}; reprovenance only handles search_metamorpheus")
    if not rec.get("success"):
        sys.exit(f"{pj} records an unsuccessful run: re-derive nothing from it")

    params = json.loads(Path(params_path).read_text(encoding="utf-8"))
    files = sorted(Path(spectra).glob("*.raw")) if spectra else spectra_from_record(rec)
    if not files:
        sys.exit("could not determine the searched spectra: pass <spectra_dir> explicitly")
    missing = [f for f in files if not f.exists()]
    # The raw files may well have been cleaned up (G14). Only the design check needs them, so say what
    # that costs instead of refusing: a re-derived record must never look more certain than it is.
    qc = out.parent / "02b_qc" / "qc_report.json"
    if not qc.exists():
        sys.exit(f"no QC report at {qc}: its ms2 totals are the denominator of the id rate")

    before = {k: rec.get(k) for k in DERIVED}
    blocks, flags, notes = derive_metrics(out, params, files, qc)
    after = {**blocks, "flags": flags}
    diff = changes(before, after)

    entry = {
        "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "tool": "reprovenance.py",
        "from_schema": rec.get("schema"),
        "to_schema": SCHEMA,
        "pipeline": {"version": pipeline_version(), "commit": pipeline_commit()},
        "params_file": str(Path(params_path)),
        "rederived": list(DERIVED),
        "changes": diff or "none",
    }
    if missing:
        entry["spectra_absent"] = [f.name for f in missing]
        notes.append(f"re-derived with {len(missing)} of {len(files)} spectra files no longer on disk; "
                     "flags that depend on files beside the spectra (no_design_file) reflect that")

    rec.update(blocks)
    rec["flags"] = flags
    rec["schema"] = SCHEMA
    rec.setdefault("notes", []).extend(notes)
    rec.setdefault("rederived", []).append(entry)
    pj.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    print(json.dumps({"provenance": str(pj), "from_schema": entry["from_schema"], "to_schema": SCHEMA,
                      "changes": diff or "none"}, indent=2))


if __name__ == "__main__":
    main(*sys.argv[1:4])
