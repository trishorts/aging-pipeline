"""Shared fixtures. The stage scripts in bin/ are imported as modules, as they are when run."""
import json, os, stat, sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))


@pytest.fixture
def work(tmp_path):
    """A work_root with a params.json in it; `write(**sections)` updates and rewrites the file."""
    params = {"run_date": "2026-01-01", "work_root": str(tmp_path)}
    path = tmp_path / "params.json"

    class Work:
        root = tmp_path
        params_path = path

        def write(self, **sections):
            params.update(sections)
            path.write_text(json.dumps(params, indent=2), encoding="utf-8")
            return path

        def params(self):
            return params

    w = Work(); w.write()
    return w


@pytest.fixture
def fake_mm(tmp_path):
    """A MetaMorpheus-shaped folder: a CMD launcher for tests/fake_metamorpheus.py, a CMD.dll to hash,
    and the shipped Contaminants database. Returns the CMD path."""
    d = tmp_path / "MetaMorpheus"; (d / "Contaminants").mkdir(parents=True)
    fake = Path(__file__).resolve().parent / "fake_metamorpheus.py"
    if os.name == "nt":
        cmd = d / "CMD.cmd"
        cmd.write_text(f'@"{sys.executable}" "{fake}" %*\r\n', encoding="utf-8")
    else:
        cmd = d / "CMD"
        cmd.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n', encoding="utf-8")
        cmd.chmod(cmd.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (d / "CMD.dll").write_bytes(b"not really a dll")
    (d / "Contaminants" / "MetaMorpheusContaminants.xml").write_text("<uniprot/>", encoding="utf-8")
    return cmd


@pytest.fixture
def no_bridge(monkeypatch):
    """Keep offline tests off the mzLib bridge: provenance asks it for versions."""
    import importlib.metadata, pymzlib
    monkeypatch.setattr(pymzlib, "bridge_version", lambda: {"bridge": "test", "mzlib": "test"}, raising=False)
    real = importlib.metadata.version
    monkeypatch.setattr(importlib.metadata, "version", lambda n: "0.0.test" if n == "mzlib" else real(n))


def scan_headers(n_ms2=20, ms2_analyzer="Orbitrap", ms2_dissociation="HCD"):
    """Synthetic .raw scan headers: one MS1 then n_ms2 MS2, Orbitrap HCD by default, which is what the
    v1 QC gate wants to see. `ms2_analyzer="IonTrap2D"` reproduces the high-low method that failed
    PXD060431 (S37): an Orbitrap MS1 with HCD fragments read out in the ion trap. Shared by the offline
    pipeline tests and the reprovenance tests."""
    from types import SimpleNamespace
    order = [1] + [2] * n_ms2
    return SimpleNamespace(scan_count=len(order), columns={
        "ms_order": order, "mz_analyzer": ["Orbitrap"] + [ms2_analyzer] * n_ms2,
        "dissociation_type": [""] + [ms2_dissociation] * n_ms2,
        "retention_time": [float(i) for i in range(len(order))],
        "selected_ion_charge_state_guess": [0] + [2] * n_ms2})


@pytest.fixture
def layout(work, fake_mm, monkeypatch, no_bridge):
    """A work_root laid out as the pipeline lays one out, with stage 4 ready to run against fake_mm."""
    import gzip
    from types import SimpleNamespace
    import qc_spectra
    acc = "PXD000001"
    src = work.root / "source" / "proteome.xml.gz"; src.parent.mkdir()
    with gzip.open(src, "wt", encoding="utf-8") as fh:
        fh.write("<uniprot><entry/></uniprot>")
    run = work.root / "run_2026-01-01" / acc
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
    monkeypatch.setattr(qc_spectra.readers, "read_spectra", lambda f, timeout=None: scan_headers())
    return SimpleNamespace(run=run, spectra=spectra, params=str(work.params_path), root=work.root, acc=acc)
