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
