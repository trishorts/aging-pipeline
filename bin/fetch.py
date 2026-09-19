"""Stage 2 - fetch the spectra (and the SDRF, if any) for ONE accession from the frozen list.

Glue only: downloads go through pymzlib, which writes `<name>.partial` and renames on success. We
use overwrite=False, so a rerun skips files that are complete (pyMzLib 003 D5-d).

Known gaps, recorded rather than worked around:
  * no retry/resume yet (REQ-PRIDE-1, pride #7c); a failed transfer restarts from byte 0;
  * no checksum verification (REQ-PRIDE-2); we record a local SHA-256 for PROVENANCE;
  * the REST manifest (list_files) is knowingly incomplete for some projects (pride 002 Q4); the
    prototype checks it against the FTP inventory and reports any difference.

usage: fetch.py <params.json> <accession> <out_dir>
"""
import hashlib, json, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymzlib.pride as pride
from provenance import Provenance, pymzlib_tool


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main(params_path: str, accession: str, out_dir: str) -> None:
    p = json.loads(Path(params_path).read_text(encoding="utf-8"))["fetch"]
    if p["pick"] not in ("median_size", "first_by_name", "all"):
        sys.exit(f"fetch.pick = {p['pick']!r}: expected median_size, first_by_name or all")
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    prov = Provenance("fetch", params_path, "fetch"); pymzlib_tool(prov)
    prov.rec["accession"] = accession
    prov.upstream(out.parent.parent / "01_discover" / "provenance.json")
    prov.command(["pymzlib.pride.list_files", accession]); prov.command(["pymzlib.pride.list_ftp_files", accession])

    rest = pride.list_files(accession)
    ftp = pride.list_ftp_files(accession)
    ext = p["extension"].lower()
    raws = [f for f in rest if f.file_name.lower().endswith(ext)]
    ftp_raw_names = {f.file_name for f in ftp if f.file_name.lower().endswith(ext)}
    missing_from_rest = sorted(ftp_raw_names - {f.file_name for f in raws})

    raws = [f for f in raws if f.file_size_bytes <= p["max_file_mb"] * 1_000_000]
    # The smallest file is often a blank or a failed run (user), so the prototype takes the median.
    raws.sort(key=lambda f: f.file_size_bytes)
    if p["pick"] == "all":
        raws.sort(key=lambda f: f.file_name)
        chosen = raws
    elif p["pick"] == "first_by_name":
        chosen = sorted(raws, key=lambda f: f.file_name)[: p["max_files"]]
    elif p["pick"] == "median_size" and raws:
        mid = len(raws) // 2
        start = max(0, mid - p["max_files"] // 2)
        chosen = raws[start: start + p["max_files"]]
    else:
        chosen = raws[: p["max_files"]]
    sdrfs = [f for f in rest if "sdrf" in f.file_name.lower()]

    spectra_dir = out / "spectra"; meta_dir = out / "metadata"
    prov.command(["pymzlib.pride.download_files", *[f.file_name for f in chosen + sdrfs], "overwrite=False"])
    # Concurrency is the caller's policy (pride 002 Q4c). One bridge call per file, N at a time.
    def one(f):
        t0 = time.monotonic()
        path = pride.download_files([f], spectra_dir, overwrite=False, timeout=p["timeout_s"])[0]
        return path, round(time.monotonic() - t0, 1)
    with ThreadPoolExecutor(max_workers=max(1, p.get("parallel_downloads", 1))) as ex:
        results = list(ex.map(one, chosen))
    got = [r[0] for r in results]
    prov.rec["download_seconds"] = {f.file_name: s for f, (_, s) in zip(chosen, results)}
    prov.rec["parallel_downloads"] = p.get("parallel_downloads", 1)
    got_sdrf = pride.download_files(sdrfs, meta_dir, overwrite=False, timeout=600) if sdrfs else []

    manifest = {
        "accession": accession,
        "rest_raw_count": len([f for f in rest if f.file_name.lower().endswith(ext)]),
        "ftp_raw_count": len(ftp_raw_names),
        "raw_missing_from_rest_manifest": missing_from_rest,
        "files": [
            {"file": str(path), "name": f.file_name, "pride_size_bytes": f.file_size_bytes,
             "local_size_bytes": path.stat().st_size, "pride_checksum": f.checksum or None,
             "sha256": sha256(path), "category": f.category}
            for f, path in zip(chosen, got)
        ],
        "sdrf": [str(x) for x in got_sdrf],
    }
    (out / "fetch_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if not any(f["pride_checksum"] for f in manifest["files"]):
        prov.note("PRIDE supplied no checksum; integrity rests on the local SHA-256 only (REQ-PRIDE-2).")
    prov.outputs(*got, *got_sdrf, out / "fetch_manifest.json"); prov.write(out)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main(*sys.argv[1:4])
