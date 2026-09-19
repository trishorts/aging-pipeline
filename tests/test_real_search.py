"""A REAL MetaMorpheus search: stages 0 → 2b → 4 on two small Thermo .raw files, with no fakes.

It needs the MetaMorpheus 1.1.11 command-line release and three small public files from mzLib's test data
(pinned in .github/workflows/ci.yml): f1r1_sliced_mbr.raw, f1r2_sliced_mbr.raw and
UP000005640_reviewedproteinPruned.xml. Point the test at them with two environment variables:

  AGING_MM_CMD    CMD.dll (runs as `dotnet CMD.dll`, any OS) or CMD.exe
  AGING_MM_DATA   the folder holding the three files

Without them, the test skips. The CI `search` job sets both. Running it accepts Thermo's RawFileReader
licence (search.accept_thermo_licence), as any run of the pipeline on .raw input does.
"""
import json, os, shutil
from pathlib import Path

import pytest

import db_prepare, qc_spectra, search_mm

MM, DATA = os.environ.get("AGING_MM_CMD"), os.environ.get("AGING_MM_DATA")
RAWS = ("f1r1_sliced_mbr.raw", "f1r2_sliced_mbr.raw")
DB = "UP000005640_reviewedproteinPruned.xml"

pytestmark = [pytest.mark.metamorpheus,
              pytest.mark.skipif(not (MM and DATA), reason="set AGING_MM_CMD and AGING_MM_DATA to run a real search")]


def stage(fn, *args):
    try:
        fn(*args)
        return 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1


def test_real_search_two_sliced_raw_files(work):
    data = Path(DATA)
    run = work.root / "run_2026-01-01" / "PXD000000"
    spectra = run / "02_fetch" / "spectra"; spectra.mkdir(parents=True)
    for n in RAWS:
        shutil.copyfile(data / n, spectra / n)
    params = work.write(
        # The sliced files hold a few thousand scans, so the MS2 floor is lowered; the HCD/Orbitrap rule is not.
        qc={"min_fraction_orbitrap_hcd": 0.9, "min_ms2": 100, "timeout_s": 600},
        database={"uniprot_xml": str(data / DB), "prepared": str(work.root / "db" / DB), "include_contaminants": True},
        search={"metamorpheus_cmd": MM, "metamorpheus_version": "1.1.11", "accept_thermo_licence": True,
                "tasks": ["Calibration", "Gptmd", "Search"], "max_threads": os.cpu_count() or 2,
                "match_between_runs": True, "flag_min_id_rate": 0.15, "timeout_s": 3600})

    assert stage(db_prepare.main, str(params), str(work.root / "db")) == 0
    assert stage(qc_spectra.main, str(params), str(spectra), str(run / "02b_qc")) == 0, "QC gate failed"
    rc = stage(search_mm.main, str(params), str(spectra), str(run / "04_search"))
    out = run / "04_search"
    log = (out / "metamorpheus.log").read_text(encoding="utf-8", errors="replace") if (out / "metamorpheus.log").exists() else ""
    assert rc == 0, f"search failed; last log lines:\n{log[-3000:]}"

    rec = json.loads((out / "provenance.json").read_text(encoding="utf-8"))
    mm = rec["tools"]["MetaMorpheus"]
    assert rec["success"] and mm["release"] == "1.1.11" and len(mm["commit"]) == 40
    if MM.lower().endswith(".dll"):
        assert mm["cmd"].endswith(" " + str(Path(MM)))                      # launched as <dotnet> CMD.dll
    assert set(rec["per_task_resources"]) == {"Task1CalibrationTask", "Task2GptmdTask", "Task3SearchTask"}
    assert any(a.endswith("MetaMorpheusContaminants.xml") for a in rec["commands"][-1])
    # A floor, not an exact count: it catches a search that runs but finds (almost) nothing.
    assert rec["id_rate"]["psms_1pct"] >= 50, rec["id_rate"]          # 1.1.11, Windows and Linux: 78 of 1,155 MS2
    assert rec["mbr"]["msms_peaks"] > 0
    print(json.dumps({k: rec[k] for k in ("id_rate", "mbr", "contamination", "flags")}, indent=2))
