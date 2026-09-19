"""discover.py (stage 1): filter order, one row per hit, the summary. PRIDE is replaced by fakes."""
import csv, json
from types import SimpleNamespace

import pytest

import discover

DISCOVER = {"keywords": ["aging", "senescence"], "organism": "Homo sapiens (human)", "require_sdrf_file": False,
            "thermo_instrument_patterns": ["Q Exactive", "Orbitrap", "LTQ", "Velos"],
            "orbitrap_ms2_only_patterns": ["Q Exactive"], "hybrid_patterns": ["Velos"],
            "dia_patterns": ["data-independent", "\\bDIA\\b"], "label_patterns": ["\\bTMT", "SILAC"],
            "timeout_s": 5}


def hit(acc, organisms=("Homo sapiens (human)",), instruments=("Q Exactive",), protocol="",
        files=("a.raw", "b.raw"), experiment_types=()):
    return SimpleNamespace(accession=acc, title=f"title {acc}", organisms=list(organisms),
                           instruments=list(instruments), sample_processing_protocol=protocol,
                           data_processing_protocol="", experiment_types=list(experiment_types),
                           project_file_names=list(files), organism_parts=["muscle"],
                           submission_type="COMPLETE")


HITS = {
    "aging": [hit("PXD000001"),                                                  # kept
              hit("PXD000002", organisms=("Mus musculus (mouse)",)),              # organism
              hit("PXD000003", protocol="data-independent acquisition"),          # dia
              hit("PXD000004", protocol="labelled with TMT 16-plex"),             # labelled
              hit("PXD000005", instruments=("timsTOF Pro",)),                     # not_thermo
              hit("PXD000006", instruments=("LTQ",)),                             # low_res_instrument
              hit("PXD000007", files=("peaks.mgf",))],                            # no_raw_listed
    "senescence": [hit("PXD000001"),                                              # a duplicate: unioned
                   hit("PXD000008", instruments=("LTQ Orbitrap Velos",),
                       files=("x.raw", "x_sdrf.tsv"))],                           # kept, check_ms2
}


@pytest.fixture
def run(work, monkeypatch, no_bridge):
    monkeypatch.setattr(discover.pride, "search", lambda kw, timeout=None: HITS[kw])
    work.write(discover=DISCOVER)
    out = work.root / "01_discover"
    discover.main(str(work.params_path), str(out))
    rows = list(csv.DictReader((out / "candidates_2026-01-01.tsv").open(encoding="utf-8"), delimiter="\t"))
    return {r["accession"]: r for r in rows}, json.loads((out / "discover_summary.json").read_text())


def test_one_row_per_unique_hit_with_its_first_failing_rule(run):
    rows, _ = run
    assert {a: r["drop_reason"] for a, r in rows.items()} == {
        "PXD000001": "", "PXD000002": "organism", "PXD000003": "dia", "PXD000004": "labelled",
        "PXD000005": "not_thermo", "PXD000006": "low_res_instrument", "PXD000007": "no_raw_listed",
        "PXD000008": ""}
    assert rows["PXD000001"]["keywords_hit"] == "aging;senescence"


def test_ms2_class_and_sdrf_detection(run):
    rows, _ = run
    assert rows["PXD000001"]["ms2_class"] == "orbitrap_hcd_only"
    assert rows["PXD000008"]["ms2_class"] == "check_ms2"
    assert rows["PXD000008"]["has_sdrf_file"] == "True"


def test_summary_counts(run):
    _, s = run
    assert (s["union_hits"], s["kept"], s["kept_with_sdrf_file"]) == (8, 2, 1)
    assert s["dropped_by_reason"] == {"dia": 1, "labelled": 1, "low_res_instrument": 1,
                                      "no_raw_listed": 1, "not_thermo": 1, "organism": 1}
