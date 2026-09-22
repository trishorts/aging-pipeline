"""Stage 2 - fetch the spectra (and the SDRF, if any) for ONE accession from the frozen list.

Glue only: downloads go through pymzlib, which writes `<name>.partial` and renames on success. We
use overwrite=False, so a rerun skips files that are complete (pyMzLib 003 D5-d).

A transfer that dies mid-flight is RETRIED here (`fetch.max_attempts`, default 3, with linear
backoff). This is the orchestrator's policy, not a client feature: pymzlib raises
`ServiceUnavailableError` for a transport failure, and for a download that is a thing to retry rather
than a reason to abandon 20 GB of work. Only that error class is retried; anything else fails the
stage immediately, exactly as in the live-test rule.

Known gaps, recorded rather than worked around:
  * no byte-range RESUME (REQ-PRIDE-1, pride #7c). A retry re-requests the file, so a transfer that
    dies at 90% pays for the whole file again. Retrying is not resuming, and the difference is real
    on a 1.6 GB file;
  * no checksum verification (REQ-PRIDE-2); we record a local SHA-256 for PROVENANCE;
  * the REST manifest (list_files) is knowingly incomplete for some projects (pride 002 Q4); the
    prototype checks it against the FTP inventory and reports any difference.

usage: fetch.py <params.json> <accession> <out_dir>
"""
import hashlib, json, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymzlib.pride as pride
from pymzlib import ServiceUnavailableError
from provenance import Provenance, pymzlib_tool


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download_with_retry(f, spectra_dir, timeout, attempts=3, backoff_s=10.0):
    """Download one PRIDE file, retrying a transport failure. Returns (path, seconds, attempts_used).

    Only `ServiceUnavailableError` is retried. EBI drops connections on long transfers - observed on
    PXD027318 as "The response ended prematurely, with at least 334864664 additional bytes expected"
    after 7.7 GB of an 18-file set - and one such drop used to abandon the whole stage along with every
    other file's work. Any other exception is a real failure and propagates immediately, which is the
    same rule the live tests use: an outage is tolerated, anything else is not.

    This is a RETRY, not a resume. `overwrite=False` means an already-complete file is skipped, so
    re-running the stage is cheap, but a transfer that dies at 90% pays for the whole file again
    (REQ-PRIDE-1, pride #7c).
    """
    t0 = time.monotonic()
    last = None
    for attempt in range(1, attempts + 1):
        try:
            path = pride.download_files([f], spectra_dir, overwrite=False, timeout=timeout)[0]
            return path, round(time.monotonic() - t0, 1), attempt
        except ServiceUnavailableError as e:
            last = e
            if attempt < attempts:
                time.sleep(backoff_s * attempt)
    raise RuntimeError(f"{f.file_name}: {attempts} attempts all failed; last error: {last}")


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

    n_listed = len(raws)
    raws = [f for f in raws if f.file_size_bytes <= p["max_file_mb"] * 1_000_000]
    n_oversize = n_listed - len(raws)
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
    # A subset of a deposit is a design decision, and making it by file size or name is how an arm or an
    # acquisition batch silently goes missing (S43: a median-size window kept 1 of 3 wild-type controls).
    # It is allowed -- a probe needs one file -- but it is never silent.
    prov.rec["raw_files_listed"] = n_listed
    prov.rec["raw_files_chosen"] = len(chosen)
    if len(chosen) < n_listed:
        prov.rec.setdefault("flags", []).append(
            f"subset_of_deposit: {len(chosen)} of {n_listed} raw files (pick={p['pick']}"
            + (f", {n_oversize} above max_file_mb={p['max_file_mb']}" if n_oversize else "")
            + "); results describe this subset, not the experiment")

    spectra_dir = out / "spectra"; meta_dir = out / "metadata"
    prov.command(["pymzlib.pride.download_files", *[f.file_name for f in chosen + sdrfs], "overwrite=False"])
    # Concurrency is the caller's policy (pride 002 Q4c). One bridge call per file, N at a time.
    attempts_allowed = int(p.get("max_attempts", 3))
    backoff_s = float(p.get("retry_backoff_s", 10))

    def one(f):
        return download_with_retry(f, spectra_dir, p["timeout_s"], attempts_allowed, backoff_s)

    with ThreadPoolExecutor(max_workers=max(1, p.get("parallel_downloads", 1))) as ex:
        results = list(ex.map(one, chosen))
    got = [r[0] for r in results]
    prov.rec["download_seconds"] = {f.file_name: s for f, (_, s, _a) in zip(chosen, results)}
    prov.rec["download_attempts"] = {f.file_name: a for f, (_p, _s, a) in zip(chosen, results)}
    prov.rec["max_attempts"] = attempts_allowed
    prov.rec["parallel_downloads"] = p.get("parallel_downloads", 1)
    retried = {f.file_name: a for f, (_p, _s, a) in zip(chosen, results) if a > 1}
    if retried:
        # Not a failure, but not nothing either: a flaky source is worth following up (user rule).
        prov.rec.setdefault("flags", []).append(
            f"download_retried: {len(retried)} of {len(chosen)} files needed more than one attempt "
            f"({', '.join(f'{k} x{v}' for k, v in sorted(retried.items()))})")
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
