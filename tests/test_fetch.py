"""fetch.py (stage 2): file picking, the manifest, and rejecting a bad `pick`. PRIDE is replaced by fakes."""
import hashlib, json
from pathlib import Path
from types import SimpleNamespace

import pytest

import fetch

SIZES = {"blank.raw": 10, "s1.raw": 200, "s2.raw": 300, "s3.raw": 400, "huge.raw": 5_000_000_000}


def pf(name, size):
    return SimpleNamespace(file_name=name, file_size_bytes=size, checksum="", category="RAW")


@pytest.fixture
def fake_pride(monkeypatch, no_bridge):
    rest = [pf(n, s) for n, s in SIZES.items()] + [pf("design.sdrf.tsv", 1)]
    ftp = [SimpleNamespace(file_name=n) for n in [*SIZES, "only_on_ftp.raw"]]
    monkeypatch.setattr(fetch.pride, "list_files", lambda acc: rest)
    monkeypatch.setattr(fetch.pride, "list_ftp_files", lambda acc: ftp)

    def download(files, dest, overwrite=True, timeout=None):
        Path(dest).mkdir(parents=True, exist_ok=True)
        out = []
        for f in files:
            p = Path(dest) / f.file_name; p.write_bytes(f.file_name.encode()); out.append(p)
        return out
    monkeypatch.setattr(fetch.pride, "download_files", download)


def run(work, **fetch_params):
    base = {"max_files": 1, "pick": "median_size", "parallel_downloads": 2, "max_file_mb": 1500,
            "extension": ".raw", "timeout_s": 5}
    work.write(fetch={**base, **fetch_params})
    out = work.root / "run_2026-01-01" / "PXD1" / "02_fetch"
    fetch.main(str(work.params_path), "PXD1", str(out))
    return json.loads((out / "fetch_manifest.json").read_text(encoding="utf-8"))


def test_median_size_never_takes_the_smallest_or_the_oversized(work, fake_pride):
    m = run(work)
    assert [f["name"] for f in m["files"]] == ["s2.raw"]


def test_all_takes_everything_under_the_size_cap_sorted_by_name(work, fake_pride):
    m = run(work, pick="all")
    assert [f["name"] for f in m["files"]] == ["blank.raw", "s1.raw", "s2.raw", "s3.raw"]


def test_manifest_records_hash_sdrf_and_rest_ftp_difference(work, fake_pride):
    m = run(work)
    f = m["files"][0]
    assert f["sha256"] == hashlib.sha256(b"s2.raw").hexdigest() and f["pride_checksum"] is None
    assert m["raw_missing_from_rest_manifest"] == ["only_on_ftp.raw"]
    assert len(m["sdrf"]) == 1


def test_unknown_pick_is_rejected_before_downloading(work, fake_pride):
    with pytest.raises(SystemExit, match="fetch.pick"):
        run(work, pick="smallest")
