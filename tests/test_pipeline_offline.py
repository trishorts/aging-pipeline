"""A minimal pipeline, end to end and OFFLINE: stage 0 → 2b → 4 → 9 in the documented layout.

Real code in every stage. Only the outside world is faked: PRIDE isn't called (the spectra are placed as
fetch would place them), the .raw reader returns synthetic scan headers, and MetaMorpheus is
tests/fake_metamorpheus.py. The test checks the wiring between stages: directory conventions, upstream
provenance links, refusal rules, the success check, and the measurements and flags in provenance.json.
"""
import json
from pathlib import Path

import pytest

import cleanup, db_prepare, qc_spectra, search_mm
from conftest import scan_headers

ACC = "PXD000001"


def stage(fn, *args):
    """Run a stage's main(); return its exit code (0 when it returns normally)."""
    try:
        fn(*args)
        return 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1


def prov(path):
    return json.loads((path / "provenance.json").read_text(encoding="utf-8"))


def test_minimal_pipeline_end_to_end(layout):
    L = layout
    assert stage(db_prepare.main, L.params, str(L.root / "db")) == 0
    assert (L.root / "db" / "proteome.xml").read_text(encoding="utf-8").startswith("<uniprot>")
    assert stage(qc_spectra.main, L.params, str(L.spectra), str(L.run / "02b_qc")) == 0
    assert stage(search_mm.main, L.params, str(L.spectra), str(L.run / "04_search")) == 0

    rec = prov(L.run / "04_search")
    assert rec["success"] and rec["exit_code"] == 0
    assert rec["tools"]["MetaMorpheus"]["release"] == "1.1.11"
    assert {u["stage"] for u in rec["upstream"]} == {"qc_spectra", "db_prepare"}   # no fetch record here
    run_cmd = rec["commands"][-1]
    assert "--acceptThermoLicence" in run_cmd and any(a.endswith("MetaMorpheusContaminants.xml") for a in run_cmd)
    # The edited task files carry the params-named settings, and nothing else changed.
    search_toml = (L.run / "04_search" / "tasks" / "3_SearchTask.toml").read_text(encoding="utf-8")
    assert "MatchBetweenRuns = true" in search_toml and "MaxThreadsToUsePerFile = 2" in search_toml
    # The measurements, per docs/provenance.md, from the fake's tables.
    # S21: the canonical count is results.txt's target-only summary line, not the FDR engine's log line.
    assert rec["id_rate"] == {"definition": "aging DEF-PSM-1PCT v1", "psms_1pct": 8, "ms2": 40, "rate": 0.2,
                              "psms_fdr_engine_1pct": 9, "psms_fdr_engine_definition": "aging DEF-PSM-FDRENGINE v1"}
    assert rec["mbr"]["mbr_rows"] == 3 and rec["mbr"]["mbr_kept"] == 1 and rec["mbr"]["msms_peaks"] == 3
    assert rec["contamination"]["psm_share"] == round(1 / 9, 4)                   # 1 C of 8 T + 1 C (decoy, q>0.01 out)
    assert set(rec["contamination"]["intensity_share_per_file"].values()) == {0.1}
    assert rec["contamination"]["top"] == ["Serum albumin (Bos taurus)"]
    flags = " ".join(rec["flags"])
    assert "high_contamination" in flags and "no_design_file" in flags and "no_output_sdrf" in flags
    assert set(rec["per_task_resources"]) == {"Task1CalibrationTask", "Task2GptmdTask", "Task3SearchTask"}

    # Stage 9: a dry run deletes nothing; the real run deletes the spectra and records their hashes.
    assert stage(cleanup.main, L.params, str(L.run), "--dry-run") == 0
    assert len(list(L.spectra.glob("*.raw"))) == 2
    assert stage(cleanup.main, L.params, str(L.run)) == 0
    assert list(L.spectra.glob("*.raw")) == []
    assert len(prov(L.run / "09_cleanup")["deleted"]) == 2


def test_search_runs_cmd_dll_through_dotnet(layout, fake_mm, work):
    """`metamorpheus_cmd` = CMD.dll is launched as `<dotnet> CMD.dll ...` (the Linux and CI path).
    The fake launcher stands in for dotnet; it ignores the leading CMD.dll argument."""
    L, dll = layout, fake_mm.parent / "CMD.dll"
    work.write(search={**work.params()["search"], "metamorpheus_cmd": str(dll), "dotnet": str(fake_mm)})
    stage(db_prepare.main, L.params, str(L.root / "db"))
    stage(qc_spectra.main, L.params, str(L.spectra), str(L.run / "02b_qc"))
    assert stage(search_mm.main, L.params, str(L.spectra), str(L.run / "04_search")) == 0
    rec = prov(L.run / "04_search")
    assert rec["success"] and rec["tools"]["MetaMorpheus"]["release"] == "1.1.11"
    assert all(c[:2] == [str(fake_mm), str(dll)] for c in rec["commands"])
    assert rec["tools"]["MetaMorpheus"]["cmd"] == f"{fake_mm} {dll}"


def test_search_refuses_without_a_passing_qc_report(layout, monkeypatch):
    L = layout
    stage(db_prepare.main, L.params, str(L.root / "db"))
    monkeypatch.setattr(qc_spectra.readers, "read_spectra", lambda f, timeout=None: scan_headers(n_ms2=3))
    assert stage(qc_spectra.main, L.params, str(L.spectra), str(L.run / "02b_qc")) == 2
    with pytest.raises(SystemExit, match="QC failed"):
        search_mm.main(L.params, str(L.spectra), str(L.run / "04_search"))


EXCEPTION = {"reason": "PXD060431 is high-low: Orbitrap MS1, HCD read out in the ion trap (S37)",
             "granted_by": "user", "granted_date": "2026-09-20", "waives": ["low_res_ms2"],
             "restricts_to": ["abundance"], "bars": ["ptm_stoichiometry", "ptm_site_localization"]}


def test_search_runs_under_an_acquisition_exception_and_says_so(layout, work, monkeypatch):
    """A user-granted waiver lets low-res MS2 through, and the run carries that fact in its flags and
    its provenance so no downstream query can use it without seeing the restriction."""
    L = layout
    stage(db_prepare.main, L.params, str(L.root / "db"))
    monkeypatch.setattr(qc_spectra.readers, "read_spectra",
                        lambda f, timeout=None: scan_headers(ms2_analyzer="IonTrap2D"))
    assert stage(qc_spectra.main, L.params, str(L.spectra), str(L.run / "02b_qc")) == 2
    work.write(qc={**work.params()["qc"], "acquisition_exception": EXCEPTION})
    assert stage(search_mm.main, L.params, str(L.spectra), str(L.run / "04_search")) == 0
    rec = prov(L.run / "04_search")
    assert rec["acquisition_exception"]["waives"] == ["low_res_ms2"]
    assert rec["acquisition_exception"]["fail_reasons"] == ["low_res_ms2"]
    assert sorted(rec["acquisition_exception"]["files"]) == ["a.raw", "b.raw"]
    flag = next(f for f in rec["flags"] if f.startswith("acquisition_exception:"))
    assert "ptm_stoichiometry" in flag and "abundance" in flag


def test_an_acquisition_exception_does_not_waive_a_failure_it_does_not_name(layout, work, monkeypatch):
    """The waiver is scoped: too_few_ms2 is not in `waives`, so the file still fails. A waiver must
    never become a blanket override."""
    L = layout
    stage(db_prepare.main, L.params, str(L.root / "db"))
    monkeypatch.setattr(qc_spectra.readers, "read_spectra",
                        lambda f, timeout=None: scan_headers(n_ms2=3, ms2_analyzer="IonTrap2D"))
    assert stage(qc_spectra.main, L.params, str(L.spectra), str(L.run / "02b_qc")) == 2
    work.write(qc={**work.params()["qc"], "acquisition_exception": EXCEPTION})
    with pytest.raises(SystemExit, match="QC failed"):
        search_mm.main(L.params, str(L.spectra), str(L.run / "04_search"))


def test_mass_tolerance_overrides_reach_every_task_and_spare_the_lowres_line(layout, work):
    """A low-resolution dataset needs a fragment tolerance MetaMorpheus's defaults do not give it
    (S38). The override must hit every task - calibration fails for the same reason the search
    under-identifies - and must NOT touch `ProductMassTolerance_LowRes`, which is a different key
    that happens to share a prefix."""
    L = layout
    stage(db_prepare.main, L.params, str(L.root / "db"))
    stage(qc_spectra.main, L.params, str(L.spectra), str(L.run / "02b_qc"))
    work.write(search={**work.params()["search"],
                       "product_mass_tolerance": "±0.3500 Absolute",
                       "precursor_mass_tolerance": "±10.0000 PPM"})
    assert stage(search_mm.main, L.params, str(L.spectra), str(L.run / "04_search")) == 0
    for toml in sorted((L.run / "04_search" / "tasks").glob("[0-9]_*.toml")):
        body = toml.read_text(encoding="utf-8")
        assert 'ProductMassTolerance = "±0.3500 Absolute"' in body
        assert 'PrecursorMassTolerance = "±10.0000 PPM"' in body
        assert 'ProductMassTolerance_LowRes = "±0.3500 Absolute"' in body   # untouched default
    rec = prov(L.run / "04_search")
    assert rec["tolerance_overrides"]["Search.ProductMassTolerance"] == "±0.3500 Absolute"


def test_no_tolerance_override_leaves_the_defaults_and_records_nothing(layout):
    L = layout
    stage(db_prepare.main, L.params, str(L.root / "db"))
    stage(qc_spectra.main, L.params, str(L.spectra), str(L.run / "02b_qc"))
    assert stage(search_mm.main, L.params, str(L.spectra), str(L.run / "04_search")) == 0
    assert "tolerance_overrides" not in prov(L.run / "04_search")


def test_exclude_files_drops_named_spectra_and_records_the_hole(layout, work):
    """An exclusion is a hole in the dataset, so it must be named rather than inferred, and it must
    appear in provenance. PXD060431's HumanHFreducedEF_5 is why this exists (S38)."""
    L = layout
    stage(db_prepare.main, L.params, str(L.root / "db"))
    stage(qc_spectra.main, L.params, str(L.spectra), str(L.run / "02b_qc"))
    work.write(search={**work.params()["search"], "exclude_files": ["b.raw"],
                       "exclude_files_why": "stalled in ClassicSearchEngine"})
    assert stage(search_mm.main, L.params, str(L.spectra), str(L.run / "04_search")) == 0
    rec = prov(L.run / "04_search")
    assert rec["excluded_files"]["files"] == ["b.raw"]
    assert "stalled" in rec["excluded_files"]["reason"]
    searched = [Path(a).name for a in rec["commands"][-1]]
    assert "a.raw" in searched and "b.raw" not in searched


def test_exclude_files_refuses_a_name_that_is_not_there(layout, work):
    """A typo in an exclusion list would silently search everything. Refuse instead."""
    L = layout
    stage(db_prepare.main, L.params, str(L.root / "db"))
    stage(qc_spectra.main, L.params, str(L.spectra), str(L.run / "02b_qc"))
    work.write(search={**work.params()["search"], "exclude_files": ["typo.raw"]})
    with pytest.raises(SystemExit, match="not in"):
        search_mm.main(L.params, str(L.spectra), str(L.run / "04_search"))


def test_contaminant_database_can_be_overridden(layout, work, tmp_path):
    """A Search-only re-run must be able to use GPTMD's augmented contaminant database, or the
    contamination metrics stop being comparable with the full chain's."""
    L = layout
    gptmd_contam = tmp_path / "MetaMorpheusContaminantsGPTMD.xml"
    gptmd_contam.write_text("<uniprot><entry/></uniprot>", encoding="utf-8")
    stage(db_prepare.main, L.params, str(L.root / "db"))
    stage(qc_spectra.main, L.params, str(L.spectra), str(L.run / "02b_qc"))
    work.write(database={**work.params()["database"], "contaminants": str(gptmd_contam)})
    assert stage(search_mm.main, L.params, str(L.spectra), str(L.run / "04_search")) == 0
    rec = prov(L.run / "04_search")
    assert str(gptmd_contam) in rec["commands"][-1]
    assert not any(a.endswith("MetaMorpheusContaminants.xml") for a in rec["commands"][-1])
    assert any("overridden" in n for n in rec.get("notes", []))


def test_search_type_is_written_and_validated(layout, work):
    """ClassicSearch is intractable at a wide fragment tolerance (S38), so the mode is selectable -
    and a typo must refuse rather than silently leave Classic in place."""
    L = layout
    stage(db_prepare.main, L.params, str(L.root / "db"))
    stage(qc_spectra.main, L.params, str(L.spectra), str(L.run / "02b_qc"))
    work.write(search={**work.params()["search"], "search_type": "Modern"})
    assert stage(search_mm.main, L.params, str(L.spectra), str(L.run / "04_search")) == 0
    toml = (L.run / "04_search" / "tasks" / "3_SearchTask.toml").read_text(encoding="utf-8")
    assert 'SearchType = "Modern"' in toml
    assert prov(L.run / "04_search")["search_type"] == "Modern"


def test_search_type_refuses_a_value_metamorpheus_does_not_have(layout, work):
    L = layout
    stage(db_prepare.main, L.params, str(L.root / "db"))
    stage(qc_spectra.main, L.params, str(L.spectra), str(L.run / "02b_qc"))
    work.write(search={**work.params()["search"], "search_type": "Fast"})
    with pytest.raises(SystemExit, match="search_type"):
        search_mm.main(L.params, str(L.spectra), str(L.run / "04_search"))


def test_search_refuses_a_different_metamorpheus_release(layout, monkeypatch):
    L = layout
    stage(db_prepare.main, L.params, str(L.root / "db"))
    stage(qc_spectra.main, L.params, str(L.spectra), str(L.run / "02b_qc"))
    monkeypatch.setenv("FAKE_MM_RELEASE", "1.1.12")
    with pytest.raises(SystemExit, match="1.1.12"):
        search_mm.main(L.params, str(L.spectra), str(L.run / "04_search"))


def test_search_refuses_to_reuse_an_output_folder(layout):
    L = layout
    stage(db_prepare.main, L.params, str(L.root / "db"))
    (L.run / "04_search" / "mm").mkdir(parents=True)
    with pytest.raises(SystemExit, match="fresh output directory"):
        search_mm.main(L.params, str(L.spectra), str(L.run / "04_search"))


def test_exit_0_without_protein_groups_is_not_success(layout, monkeypatch):
    """FlashLFQ can fail while MetaMorpheus exits 0; success needs the tables too."""
    L = layout
    stage(db_prepare.main, L.params, str(L.root / "db"))
    stage(qc_spectra.main, L.params, str(L.spectra), str(L.run / "02b_qc"))
    monkeypatch.setenv("FAKE_MM_NO_PROTEIN_GROUPS", "1")
    assert stage(search_mm.main, L.params, str(L.spectra), str(L.run / "04_search")) == 1
    rec = prov(L.run / "04_search")
    assert rec["success"] is False and any("FlashLFQ failed silently" in n for n in rec["notes"])


def test_cleanup_refuses_before_a_successful_search(layout):
    with pytest.raises(SystemExit, match="refusing"):
        cleanup.main(layout.params, str(layout.run))


def test_an_extra_database_is_prepared_and_searched_beside_the_proteome(layout, work):
    """G61: the targeted aging isoform entries are an EXTRA `-d`, after the proteome and the
    contaminants, so the first database in `inputs` is still the reference proteome (which is what
    dataRepo reads as the search database)."""
    L = layout
    iso = L.root / "source" / "aging_isoforms_tier1-2_human.xml"
    iso.write_text("<uniprot><entry><accession>P02545-6</accession></entry></uniprot>", encoding="utf-8")
    db = dict(work.params()["database"])
    db.update(extra_xml=[str(iso)], extra_prepared=[str(L.root / "db" / iso.name)])
    work.write(database=db)

    assert stage(db_prepare.main, L.params, str(L.root / "db")) == 0
    assert (L.root / "db" / iso.name).read_text(encoding="utf-8") == iso.read_text(encoding="utf-8")
    assert stage(qc_spectra.main, L.params, str(L.spectra), str(L.run / "02b_qc")) == 0
    assert stage(search_mm.main, L.params, str(L.spectra), str(L.run / "04_search")) == 0

    rec = prov(L.run / "04_search")
    run_cmd = rec["commands"][-1]
    dbs = run_cmd[run_cmd.index("-d") + 1: run_cmd.index("-o")]
    assert [Path(x).name for x in dbs] == ["proteome.xml", "MetaMorpheusContaminants.xml", iso.name]
    assert rec["extra_databases"] == [str(L.root / "db" / iso.name)]
    db_inputs = [i["path"] for i in rec["inputs"] if str(i["path"]).endswith(".xml")]
    assert Path(db_inputs[0]).name == "proteome.xml", "the reference proteome stays first"


def test_a_missing_extra_database_stops_the_search(layout, work):
    L = layout
    db = dict(work.params()["database"])
    db.update(extra_prepared=[str(L.root / "db" / "not_prepared.xml")])
    work.write(database=db)
    assert stage(db_prepare.main, L.params, str(L.root / "db")) == 0
    assert stage(qc_spectra.main, L.params, str(L.spectra), str(L.run / "02b_qc")) == 0
    assert stage(search_mm.main, L.params, str(L.spectra), str(L.run / "04_search")) != 0
