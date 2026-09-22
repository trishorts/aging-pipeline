"""fetch.py (stage 2): file picking, the manifest, and rejecting a bad `pick`. PRIDE is replaced by fakes."""
import hashlib, json
from pathlib import Path
from types import SimpleNamespace

import pytest

import fetch
from pymzlib import ServiceUnavailableError

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


def _prov(work):
    return json.loads((work.root / "run_2026-01-01" / "PXD1" / "02_fetch" / "provenance.json").read_text(encoding="utf-8"))


def test_a_subset_of_the_deposit_is_flagged_never_silent(work, fake_pride):
    run(work)  # median_size, one file of five listed
    prov = _prov(work)
    assert prov["raw_files_listed"] == 5 and prov["raw_files_chosen"] == 1
    assert any(f.startswith("subset_of_deposit: 1 of 5") for f in prov["flags"])


def test_even_pick_all_is_flagged_when_the_size_cap_drops_a_file(work, fake_pride):
    run(work, pick="all")  # huge.raw exceeds max_file_mb
    flags = _prov(work)["flags"]
    assert any("subset_of_deposit: 4 of 5" in f and "1 above max_file_mb" in f for f in flags)


def test_the_whole_deposit_carries_no_subset_flag(work, fake_pride):
    run(work, pick="all", max_file_mb=10_000)
    prov = _prov(work)
    assert prov["raw_files_chosen"] == prov["raw_files_listed"] == 5
    assert not any(f.startswith("subset_of_deposit") for f in prov.get("flags", []))


def test_unknown_pick_is_rejected_before_downloading(work, fake_pride):
    with pytest.raises(SystemExit, match="fetch.pick"):
        run(work, pick="smallest")


# --- transport retry (added after a 20 GB PXD027318 download died on one EBI drop) -------------

class _Flaky:
    """Stands in for pride.download_files: fails the first `fail_times` calls, then succeeds."""

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self, files, out_dir, overwrite=False, timeout=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ServiceUnavailableError(
                "ResponseEnded",
                "The response ended prematurely, with at least 334864664 additional bytes expected.")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / files[0].file_name
        path.write_bytes(b"x" * 10)
        return [path]


class _File:
    file_name = "a.raw"


def test_one_dropped_connection_does_not_abandon_the_transfer(monkeypatch, tmp_path):
    flaky = _Flaky(fail_times=2)
    monkeypatch.setattr(fetch.pride, "download_files", flaky)
    monkeypatch.setattr(fetch.time, "sleep", lambda _s: None)

    path, seconds, attempts = fetch.download_with_retry(
        _File(), tmp_path / "spectra", timeout=5, attempts=3, backoff_s=0)

    assert path.exists()
    assert attempts == 3, "two failures then a success is three attempts"
    assert flaky.calls == 3
    assert seconds >= 0


def test_it_gives_up_after_the_allowance_and_names_the_file(monkeypatch, tmp_path):
    flaky = _Flaky(fail_times=99)
    monkeypatch.setattr(fetch.pride, "download_files", flaky)
    monkeypatch.setattr(fetch.time, "sleep", lambda _s: None)

    with pytest.raises(RuntimeError) as e:
        fetch.download_with_retry(_File(), tmp_path / "spectra", timeout=5, attempts=3, backoff_s=0)

    assert "a.raw" in str(e.value)
    assert "3 attempts" in str(e.value)
    assert "premature" in str(e.value), "the underlying transport error is carried, not swallowed"
    assert flaky.calls == 3, "it must not keep trying forever"


def test_a_non_transport_error_is_not_retried(monkeypatch, tmp_path):
    """The live-test rule: an outage is tolerated, anything else fails at once."""
    calls = []

    def boom(files, out_dir, overwrite=False, timeout=None):
        calls.append(1)
        raise ValueError("malformed response")

    monkeypatch.setattr(fetch.pride, "download_files", boom)
    monkeypatch.setattr(fetch.time, "sleep", lambda _s: None)

    with pytest.raises(ValueError):
        fetch.download_with_retry(_File(), tmp_path / "spectra", timeout=5, attempts=3, backoff_s=0)
    assert len(calls) == 1, "a real error must not be retried"


def test_backoff_grows_between_attempts(monkeypatch, tmp_path):
    slept = []
    monkeypatch.setattr(fetch.pride, "download_files", _Flaky(fail_times=2))
    monkeypatch.setattr(fetch.time, "sleep", lambda s: slept.append(s))

    fetch.download_with_retry(_File(), tmp_path / "spectra", timeout=5, attempts=3, backoff_s=10)
    assert slept == [10, 20], "linear backoff, and no sleep after the final attempt"
