"""Stage 9 - delete the bulky, re-obtainable intermediates once a search has succeeded (aging D3).

Raw spectra are disposable after search: PRIDE is the source of truth, and fetch's provenance.json
already records each file's name, size and SHA-256, so the exact inputs stay identifiable and can be
fetched again. This stage deletes, for ONE dataset run directory:
  * 02_fetch/spectra/*.raw (every hard link; a file is freed only when its last link goes),
  * *-calib.mzML written by the calibration task,
  * any *.raw MetaMorpheus copied into a task folder (it does so when calibration fails).
Results, the GPTMD database, logs and provenance are kept.

It refuses unless 04_search/provenance.json says success, and it records what it deleted (with the
hashes from fetch's provenance) in 09_cleanup/provenance.json. `--dry-run` lists without deleting.

usage: cleanup.py <params.json> <dataset_run_dir> [--dry-run]
"""
import json, sys
from pathlib import Path

from provenance import Provenance


def main(params_path: str, run_dir: str, *flags: str) -> None:
    dry = "--dry-run" in flags
    run = Path(run_dir)
    search_prov = run / "04_search" / "provenance.json"
    if not search_prov.exists() or not json.loads(search_prov.read_text(encoding="utf-8")).get("success"):
        sys.exit(f"refusing: {search_prov} missing or not successful")

    fetched = {}
    fp = run / "02_fetch" / "provenance.json"
    if fp.exists():
        fetched = {Path(o["path"]).name: o["sha256"]
                   for o in json.loads(fp.read_text(encoding="utf-8"))["outputs"]}

    targets = sorted({*run.glob("02_fetch/spectra/*.raw"),
                      *run.glob("04_search/mm/**/*-calib.mzML"),
                      *run.glob("04_search/mm/Task*/*.raw")})
    out = run / "09_cleanup"; out.mkdir(exist_ok=True)
    # A second run finds nothing to delete and would write a fresh "0 files, 0 bytes" record over the
    # only evidence of what the first one removed. Re-running looks idempotent; the record is not.
    # A DRY run is exempt in both directions: it deletes nothing, so its record is not evidence of
    # anything and must not block the real run that follows it.
    prior = out / "provenance.json"
    if prior.exists() and not dry and "--force" not in flags:
        was_dry = json.loads(prior.read_text(encoding="utf-8")).get("dry_run", False)
        if not was_dry:
            sys.exit(f"refusing: {prior} already records a real cleanup of this run. Pass --force to "
                     f"overwrite it (the deleted files are already gone either way).")
    prov = Provenance("cleanup", params_path, "cleanup")
    prov.upstream(search_prov, fp)
    freed = 0
    kept = []
    try:
        for t in targets:
            size = t.stat().st_size
            if not dry:
                try:
                    t.unlink()
                except OSError as e:
                    # A locked file is a fact to report, not grounds to abandon the record of the
                    # files already deleted. Windows holds .raw handles open more readily than Linux.
                    kept.append({"path": str(t), "size_bytes": size, "error": str(e)})
                    continue
            prov.rec.setdefault("deleted", []).append(
                {"path": str(t), "size_bytes": size, "sha256_at_fetch": fetched.get(t.name)})
            freed += size
    finally:
        # ALWAYS write the record, even if the loop died. This stage's output IS the evidence of a
        # destructive act; losing it in the failure case is the one outcome that cannot be repaired,
        # because the files are already gone.
        prov.rec["dry_run"] = dry
        prov.rec["bytes_freed"] = freed
        if kept:
            prov.rec["not_deleted"] = kept
            prov.note(f"{len(kept)} file(s) could not be deleted and are listed in `not_deleted`.")
        prov.note("Linked copies of the same .raw elsewhere are freed only when their last link goes.")
        prov.write(out)
    print(json.dumps({"dry_run": dry, "files": len(prov.rec.get("deleted") or []),
                      "not_deleted": len(kept), "gb": round(freed / 1e9, 2)}))


if __name__ == "__main__":
    main(*sys.argv[1:])
