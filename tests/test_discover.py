"""discover.py (stage 1): filter order, one row per hit, the summary. PRIDE is replaced by fakes."""
import csv, json
from pathlib import Path
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


# --- the screen reads every text field, and knows metabolic labelling and enrichment (S44, S46) ------

SCREEN = {"dia_patterns": [r"\bDIA\b"], "label_patterns": [r"\bTMT"],
          "metabolic_label_patterns": ["heavy[- ]?(labell?ed )?water", r"\bD2O\b"],
          "enrichment_patterns": ["streptavidin", "kinobead", "crosslinking mass spectrometry"]}


def proj(**text):
    base = dict(title="", project_description="", sample_processing_protocol="", data_processing_protocol="",
                keywords=[], experiment_types=[], quantification_methods=[])
    return SimpleNamespace(**{**base, **text})


def test_heavy_water_in_the_description_alone_is_labelled():
    # PXD015928: the protocols never mention the label; only the project description does.
    reason, evidence = discover.screen(proj(project_description="rats fed heavy labelled water for 127 days"), SCREEN)
    assert reason == "labelled" and "heavy labelled water" in evidence


def test_an_affinity_enrichment_is_deferred_as_enriched_with_its_evidence():
    reason, evidence = discover.screen(proj(sample_processing_protocol="bound to streptavidin beads"), SCREEN)
    assert reason == "enriched" and "streptavidin" in evidence
    assert discover.screen(proj(keywords=["Kinobead", "Memory"]), SCREEN)[0] == "enriched"


def test_biology_that_merely_shares_a_word_is_not_flagged():
    # PXD067622 is about DNA-protein crosslinks, the lesion, not crosslinking mass spectrometry.
    assert discover.screen(proj(title="DNA-Protein Crosslinks Promote cGAS-STING-driven Premature Aging"), SCREEN) == ("", "")


def test_labelling_wins_over_enrichment_so_an_exclusion_is_never_softened_to_a_deferral():
    assert discover.screen(proj(project_description="TMT-labelled streptavidin pulldown"), SCREEN)[0] == "labelled"


def test_an_enrichment_is_kept_and_annotated_not_dropped(work, monkeypatch, no_bridge):
    # A LAMP1-TurboID pulldown is a lysosome proteome: enrichment is recorded, never a reason to drop.
    lyso = hit("PXD000009", protocol="LAMP1-GFP-TurboID, biotinylated proteins captured on streptavidin beads")
    tmt = hit("PXD000010", protocol="streptavidin pulldown, then TMT 10-plex")
    monkeypatch.setattr(discover.pride, "search", lambda kw, timeout=None: [lyso, tmt])
    work.write(discover={**DISCOVER, **SCREEN, "keywords": ["aging"]})
    out = work.root / "01_discover"
    discover.main(str(work.params_path), str(out))
    rows = {r["accession"]: r for r in csv.DictReader((out / "candidates_2026-01-01.tsv").open(encoding="utf-8"), delimiter="\t")}
    assert rows["PXD000009"]["keep"] == "yes" and rows["PXD000009"]["enrichment"] == "other"
    assert "streptavidin" in rows["PXD000009"]["screen_evidence"]
    assert rows["PXD000010"]["drop_reason"] == "labelled"          # labelling still excludes


def test_enrichment_kind_uses_the_repository_vocabulary():
    assert discover.enrichment_kind("phosphopeptides enriched by TiO2 beads") == "phospho"
    assert discover.enrichment_kind("K-GG remnant antibody") == "ubiquitin_GG"
    assert discover.enrichment_kind("lectin glycopeptide enrichment") == "glyco"
    assert discover.enrichment_kind("SPRTN-TurboID streptavidin pulldown") == "other"


def test_a_chromatographic_or_anatomical_apex_is_not_apex2_labelling():
    params = {"enrichment_patterns": json.load(open(Path(discover.__file__).parents[1] / "params.json"))["discover"]["enrichment_patterns"]}
    for text in ("Dynamic exclusion was set to 40 s, and apex trigger was enabled",
                 "Peak intensities (at RT apex) for top 3 unique peptides",
                 "neonatal hearts (uninjured apex) into a fine powder"):
        assert discover.screen(proj(sample_processing_protocol=text), params) == ("", ""), text
    assert discover.screen(proj(sample_processing_protocol="cells expressing APEX2 were labelled"), params)[0] == "enriched"
    assert discover.screen(proj(title="LC-MS Without the Use of Affinity Enrichment"), params) == ("", "")
