"""Regression tests for the findings of the 2026-09-22 pipeline review
(`code/reviews/2026-09-22_pipeline_review.md`).

Each of these covers a path that only runs when something has already gone wrong, which is why none
of them was covered before: the happy path was well tested and the failure paths turned a recoverable
situation into an unrecoverable one.
"""
import json
from pathlib import Path

import pytest

import cleanup, db_prepare, qc_spectra, search_mm
from conftest import scan_headers


def stage(fn, *args):
    try:
        fn(*args)
        return 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1


def prov(path):
    return json.loads((path / "provenance.json").read_text(encoding="utf-8"))


@pytest.fixture
def searched(layout):
    """A run with a successful search behind it, which is what cleanup requires."""
    stage(db_prepare.main, layout.params, str(layout.root / "db"))
    stage(qc_spectra.main, layout.params, str(layout.spectra), str(layout.run / "02b_qc"))
    assert stage(search_mm.main, layout.params, str(layout.spectra), str(layout.run / "04_search")) == 0
    return layout


# ---------------------------------------------------------------- F1

def test_a_hung_search_is_killed_at_the_timeout_and_still_writes_provenance(layout, work, monkeypatch):
    """F1. `for line in proc.stdout` blocks until the process EXITS, so the old code reached
    `proc.wait(timeout=...)` only after there was nothing left to time out. A hung search blocked
    forever with no provenance - which is what every PXD060431 stall did (S38)."""
    stage(db_prepare.main, layout.params, str(layout.root / "db"))
    stage(qc_spectra.main, layout.params, str(layout.spectra), str(layout.run / "02b_qc"))
    work.write(search={**work.params()["search"], "timeout_s": 3})
    monkeypatch.setenv("FAKE_MM_HANG", "30")            # far longer than the 3 s deadline

    out = layout.run / "04_search"
    rc = stage(search_mm.main, layout.params, str(layout.spectra), str(out))
    assert rc == 1                                       # a clean failure, not a hang and not a crash

    rec = prov(out)                                      # the point: provenance EXISTS
    assert rec["timed_out_after_s"] == 3
    assert rec["success"] is False
    assert any("KILLED" in n for n in rec["notes"])


# ---------------------------------------------------------------- F2

def test_a_file_that_cannot_be_deleted_is_recorded_and_the_rest_still_are(searched):
    """F2. An unguarded `t.unlink()` meant one locked file killed the stage after it had already
    deleted others, and provenance - the only record of what went - was never written."""
    L = searched
    raws = sorted(L.spectra.glob("*.raw"))
    assert len(raws) == 2
    held = raws[0].open("rb")                            # Windows will not delete an open file
    try:
        rc = stage(cleanup.main, L.params, str(L.run))
    finally:
        held.close()

    rec = prov(L.run / "09_cleanup")
    assert rc == 0
    assert len(rec["not_deleted"]) == 1 and raws[0].name in rec["not_deleted"][0]["path"]
    assert [Path(d["path"]).name for d in rec["deleted"]] == [raws[1].name]
    assert raws[0].exists() and not raws[1].exists()     # the locked one survived, the other went
    assert rec["bytes_freed"] > 0


# ---------------------------------------------------------------- F3

def test_a_second_cleanup_refuses_rather_than_overwriting_the_first_record(searched):
    """F3. `Provenance.write` overwrites, so a no-op re-run replaced a record of 18 deleted files
    with `0 files, 0 bytes`. The files are already gone; the record is all that is left."""
    L = searched
    assert stage(cleanup.main, L.params, str(L.run)) == 0
    first = prov(L.run / "09_cleanup")
    assert len(first["deleted"]) == 2

    assert stage(cleanup.main, L.params, str(L.run)) == 1          # refuses
    assert prov(L.run / "09_cleanup")["deleted"] == first["deleted"]   # record intact

    assert stage(cleanup.main, L.params, str(L.run), "--force") == 0   # explicit override still works


def test_a_dry_run_neither_blocks_the_real_one_nor_is_blocked(searched):
    """A dry run deletes nothing, so its record is not evidence of anything and must not gate."""
    L = searched
    assert stage(cleanup.main, L.params, str(L.run), "--dry-run") == 0
    assert prov(L.run / "09_cleanup")["dry_run"] is True
    assert stage(cleanup.main, L.params, str(L.run)) == 0               # not blocked
    assert prov(L.run / "09_cleanup")["dry_run"] is False


# ---------------------------------------------------------------- F4

def test_one_unreadable_file_does_not_throw_away_the_other_verdicts(layout, monkeypatch):
    """F4. `readers.read_spectra` was unguarded, so a corrupt .raw raised and qc_report.json was
    never written - losing every other file's verdict. Downloads retry rather than resume (S33), so
    a truncated file is not a remote possibility."""
    real_names = sorted(p.name for p in layout.spectra.glob("*.raw"))
    bad = real_names[0]

    def flaky(f, timeout=None):
        if Path(f).name == bad:
            raise RuntimeError("Thermo reader: unexpected end of file")
        return scan_headers()

    monkeypatch.setattr(qc_spectra.readers, "read_spectra", flaky)
    out = layout.run / "02b_qc"
    rc = stage(qc_spectra.main, layout.params, str(layout.spectra), str(out))

    report = json.loads((out / "qc_report.json").read_text(encoding="utf-8"))
    assert set(report) == set(real_names)                     # every file still has a verdict
    assert report[bad]["fail_reasons"] == ["unreadable"]
    assert report[bad]["pass"] is False
    assert "unexpected end of file" in report[bad]["error"]
    assert report[real_names[1]]["pass"] is True              # the good file is unaffected
    assert rc != 0                                            # the dataset still fails overall


def test_unreadable_is_not_waivable_by_an_acquisition_exception(layout, work, monkeypatch):
    """`unreadable` joins `too_few_ms2` as never-waivable: a file we cannot read is not an
    acquisition choice, so no waiver may admit it."""
    bad = sorted(p.name for p in layout.spectra.glob("*.raw"))[0]

    def flaky(f, timeout=None):
        if Path(f).name == bad:
            raise RuntimeError("boom")
        return scan_headers()

    monkeypatch.setattr(qc_spectra.readers, "read_spectra", flaky)
    stage(qc_spectra.main, layout.params, str(layout.spectra), str(layout.run / "02b_qc"))
    stage(db_prepare.main, layout.params, str(layout.root / "db"))
    work.write(qc={**work.params()["qc"], "acquisition_exception": {
        "granted_by": "user", "granted_date": "2026-09-22", "waives": ["unreadable", "low_res_ms2"],
        "restricts_to": ["abundance"], "bars": ["ptm_stoichiometry"]}})
    assert stage(search_mm.main, layout.params, str(layout.spectra),
                 str(layout.run / "04_search")) == 1
