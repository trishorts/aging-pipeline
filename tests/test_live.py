"""LIVE tests: real PRIDE (and UniProt) calls. They are canaries for the contract with those services.

Marked `network`, so the offline run (`pytest -m "not network"`) never touches them. The rule follows mzLib
and pyMzLib:
  * the SERVICE being down (a timeout, a refused connection, HTTP 408/429/5xx) SKIPS the test, with the
    reason printed (`pytest -rs`). It is a third-party availability problem, not a code failure;
  * anything else FAILS. A changed response shape, a missing field, or a wrong answer means the
    contract broke, and that must stay red.
pymzlib already classifies outages as `pymzlib.ServiceUnavailableError` in its bridge. For plain HTTP
calls, `external_service()` applies the same status rule itself.

Nothing large is downloaded: fetch runs with a 0 MB file cap, so only the SDRF comes down.
"""
import csv, json, urllib.request

import pytest

import discover, fetch
from live_guard import external_service

pytestmark = pytest.mark.network

ACC = "PXD036557"          # HGPS iPSC-derived cardiomyocytes: 18 .raw files, Q Exactive, a community SDRF


def test_discover_finds_a_known_aging_dataset(work):
    work.write(discover={
        "keywords": ["progeria"], "organism": "Homo sapiens (human)", "require_sdrf_file": False,
        "thermo_instrument_patterns": ["Q Exactive", "Orbitrap", "Exploris", "Fusion", "Lumos", "Eclipse", "LTQ",
                                       "Velos", "Elite"],
        "orbitrap_ms2_only_patterns": ["Q Exactive", "Exploris"],
        "hybrid_patterns": ["Velos", "Elite", "Fusion", "Lumos", "Eclipse", "Orbitrap XL", "Orbitrap Tribrid"],
        "dia_patterns": ["data-independent", "\\bDIA\\b"], "label_patterns": ["\\bTMT", "SILAC", "iTRAQ"],
        "timeout_s": 120})
    out = work.root / "01_discover"
    with external_service("PRIDE"):
        discover.main(str(work.params_path), str(out))
    rows = {r["accession"]: r for r in
            csv.DictReader((out / "candidates_2026-01-01.tsv").open(encoding="utf-8"), delimiter="\t")}
    assert ACC in rows, "PRIDE search for 'progeria' no longer returns PXD036557"
    assert rows[ACC]["keep"] == "yes" and rows[ACC]["ms2_class"] == "orbitrap_hcd_only"
    assert json.loads((out / "provenance.json").read_text())["tools"]["pymzlib"]["bridge"]


def test_fetch_lists_the_dataset_and_downloads_only_the_sdrf(work):
    work.write(fetch={"max_files": 1, "pick": "median_size", "parallel_downloads": 1, "max_file_mb": 0,
                      "extension": ".raw", "timeout_s": 600})
    out = work.root / "run_2026-01-01" / ACC / "02_fetch"
    with external_service("PRIDE"):
        fetch.main(str(work.params_path), ACC, str(out))
    m = json.loads((out / "fetch_manifest.json").read_text(encoding="utf-8"))
    assert m["rest_raw_count"] == 18 and m["ftp_raw_count"] == 18
    assert m["files"] == []                                   # the 0 MB cap: no .raw downloaded
    assert any(s.endswith(".sdrf.tsv") for s in m["sdrf"])


def test_uniprot_serves_uniprot_xml():
    """Canary for the planned stage-0 download from UniProt (UniProt XML carries the annotated PTMs GPTMD
    starts from). LMNA is the protein behind progeria."""
    req = urllib.request.Request("https://rest.uniprot.org/uniprotkb/P02545.xml",
                                 headers={"User-Agent": "aging-pipeline-ci"})
    with external_service("UniProt"):
        body = urllib.request.urlopen(req, timeout=60).read().decode("utf-8")
    assert "<accession>P02545</accession>" in body and "<feature type=\"modified residue\"" in body
