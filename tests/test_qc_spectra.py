"""qc_spectra.py (stage 2b): the Orbitrap-HCD rule and the exit code. The .raw reader is replaced by a fake."""
import json
from types import SimpleNamespace

import pytest

import qc_spectra


def spectra(n_ms2, analyzer, dissociation):
    order = [1] + [2] * n_ms2
    return SimpleNamespace(scan_count=len(order), columns={
        "ms_order": order,
        "mz_analyzer": ["Orbitrap"] + [analyzer] * n_ms2,
        "dissociation_type": [""] + [dissociation] * n_ms2,
        "retention_time": [0.0] + [float(i) for i in range(n_ms2)],
        "selected_ion_charge_state_guess": [0] + [2] * n_ms2})


FILES = {"good.raw": spectra(20, "Orbitrap", "HCD"),
         "iontrap.raw": spectra(20, "IonTrap", "CID"),
         "blank.raw": spectra(3, "Orbitrap", "HCD")}


@pytest.fixture
def run(work, monkeypatch, no_bridge):
    def go(names):
        sp = work.root / "02_fetch" / "spectra"; sp.mkdir(parents=True, exist_ok=True)
        for n in names:
            (sp / n).write_bytes(b"raw")
        monkeypatch.setattr(qc_spectra.readers, "read_spectra", lambda f, timeout=None: FILES[f.name])
        work.write(qc={"min_fraction_orbitrap_hcd": 0.9, "min_ms2": 10, "timeout_s": 5})
        out = work.root / "02b_qc"
        with pytest.raises(SystemExit) as e:
            qc_spectra.main(str(work.params_path), str(sp), str(out))
        return e.value.code, json.loads((out / "qc_report.json").read_text(encoding="utf-8"))
    return go


def test_all_orbitrap_hcd_passes_with_exit_0(run):
    code, report = run(["good.raw"])
    assert code == 0 and report["good.raw"]["pass"] and report["good.raw"]["fraction_orbitrap_hcd"] == 1.0


def test_ion_trap_ms2_or_too_few_ms2_fails_with_exit_2(run):
    code, report = run(["good.raw", "iontrap.raw", "blank.raw"])
    assert code == 2
    assert not report["iontrap.raw"]["pass"] and report["iontrap.raw"]["ms2_analyzer_dissociation"] == {"IonTrap/CID": 20}
    assert not report["blank.raw"]["pass"] and report["blank.raw"]["ms2"] == 3
