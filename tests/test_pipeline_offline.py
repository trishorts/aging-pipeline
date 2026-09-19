"""A minimal pipeline, end to end and OFFLINE: stage 0 → 2b → 4 → 9 in the documented layout.

Real code in every stage. Only the outside world is faked: PRIDE isn't called (the spectra are placed as
fetch would place them), the .raw reader returns synthetic scan headers, and MetaMorpheus is
tests/fake_metamorpheus.py. The test checks the wiring between stages: directory conventions, upstream
provenance links, refusal rules, the success check, and the measurements and flags in provenance.json.
"""
import gzip, json
from types import SimpleNamespace

import pytest

import cleanup, db_prepare, qc_spectra, search_mm

ACC = "PXD000001"


def headers(n_ms2=20):
    order = [1] + [2] * n_ms2
    return SimpleNamespace(scan_count=len(order), columns={
        "ms_order": order, "mz_analyzer": ["Orbitrap"] * len(order),
        "dissociation_type": [""] + ["HCD"] * n_ms2, "retention_time": [float(i) for i in range(len(order))],
        "selected_ion_charge_state_guess": [0] + [2] * n_ms2})


@pytest.fixture
def layout(work, fake_mm, monkeypatch, no_bridge):
    src = work.root / "source" / "proteome.xml.gz"; src.parent.mkdir()
    with gzip.open(src, "wt", encoding="utf-8") as fh:
        fh.write("<uniprot><entry/></uniprot>")
    run = work.root / "run_2026-01-01" / ACC
    spectra = run / "02_fetch" / "spectra"; spectra.mkdir(parents=True)
    for n in ("a.raw", "b.raw"):
        (spectra / n).write_bytes(b"x" * 1000)
    work.write(
        qc={"min_fraction_orbitrap_hcd": 0.9, "min_ms2": 10, "timeout_s": 5},
        database={"uniprot_xml": str(src), "prepared": str(work.root / "db" / "proteome.xml"),
                  "include_contaminants": True},
        search={"metamorpheus_cmd": str(fake_mm), "metamorpheus_version": "1.1.11",
                "accept_thermo_licence": True, "tasks": ["Calibration", "Gptmd", "Search"],
                "max_threads": 2, "match_between_runs": True, "flag_min_id_rate": 0.15, "timeout_s": 600})
    monkeypatch.setattr(qc_spectra.readers, "read_spectra", lambda f, timeout=None: headers())
    return SimpleNamespace(run=run, spectra=spectra, params=str(work.params_path), root=work.root)


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
    monkeypatch.setattr(qc_spectra.readers, "read_spectra", lambda f, timeout=None: headers(n_ms2=3))
    assert stage(qc_spectra.main, L.params, str(L.spectra), str(L.run / "02b_qc")) == 2
    with pytest.raises(SystemExit, match="QC failed"):
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
