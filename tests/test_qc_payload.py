"""qc_payload.py (stage 5): the qc-payload/1 contract, built from MetaMorpheus outputs on disk.

qc's own `python -m qctemplates validate` is the authority on the schema, but `qctemplates` ships no
installable metadata (aging 006 §4), so it cannot run in this suite. These tests assert the
structural invariants the contract states, and the parsing rules that the payload's numbers depend on.
"""
import json

import pytest

import qc_payload


# ---------------------------------------------------------------- file keys

@pytest.mark.parametrize("raw,want", [
    ("QE-002106_GM1_a.raw", "QE-002106_GM1_a"),
    ("QE-002106_GM1_a-calib", "QE-002106_GM1_a"),
    ("QE-002106_GM1_a-calib.mzML", "QE-002106_GM1_a"),
    # The regression this test exists for: the calibration report is `<file>-calib.toml`, and leaving
    # `.toml` on made `calibration_ok` false for all 18 files of a run that calibrated perfectly.
    ("QE-002106_GM1_a-calib.toml", "QE-002106_GM1_a"),
    (r"F:\aging\02_fetch\spectra\HumanControl_1.raw", "HumanControl_1"),
    # A run name containing a dot must survive: this is why Path.stem is not used.
    ("Sample_1.2_rep3.raw", "Sample_1.2_rep3"),
])
def test_stem_gives_the_contracts_file_key(raw, want):
    assert qc_payload.stem(raw) == want


# ---------------------------------------------------------------- value parsing

def test_ambiguous_cells_are_dropped_not_guessed():
    """MetaMorpheus writes an ambiguous value as `a|b`. There is no single number for those."""
    assert qc_payload.num("3.5") == 3.5
    assert qc_payload.num("1.0|2.0") is None
    assert qc_payload.num("") is None
    assert qc_payload.num("NaN") is None
    assert qc_payload.num(None) is None


def test_histogram_clips_rather_than_dropping_the_tail():
    h = qc_payload.histogram([-999, 0.1, 999], qc_payload.edges_for("precursor_ppm"))
    assert len(h["edges"]) == len(h["counts"]) + 1
    assert sum(h["counts"]) == 3          # nothing lost off the ends
    assert h["counts"][0] == 1 and h["counts"][-1] == 1


def test_bin_edges_follow_qcs_rule_not_a_copy_of_its_numbers():
    """qc 007 §4: the edges are theirs. We call `canonical_edges` when qctemplates is installed and
    fall back to a vendored copy when it is not -- and the two must agree, or the fallback is a
    silent divergence rather than a stand-in."""
    prec = qc_payload.edges_for("precursor_ppm")
    frag = qc_payload.edges_for("fragment_ppm")
    assert (prec[0], prec[-1], round(prec[1] - prec[0], 9)) == (-10.0, 10.0, 0.5)
    assert (frag[0], frag[-1], round(frag[1] - frag[0], 9)) == (-30.0, 30.0, 1.0)
    assert len(qc_payload.edges_for("ids_over_rt", 180.0)) == 37      # 36 equal bins
    assert qc_payload.edges_for("ids_over_rt", None) is None          # undefined without a run length


# ---------------------------------------------------------------- end to end

RESULTS = """MetaMorpheus: version 1.1.11

a - MS2 Scans: 100
a - Target PSMs with q-value <= 0.01: 10
a - Target peptides with q-value <= 0.01: 8
a - Target protein groups with q-value <= 0.01: 5
b - MS2 Scans: 200
b - Target PSMs with q-value <= 0.01: 20
b - Target peptides with q-value <= 0.01: 16
b - Target protein groups with q-value <= 0.01: 9
"""

PSM_COLS = ["File Name", "Decoy/Contaminant/Target", "QValue", "QValue Notch", "Notch",
            "Mass Diff (ppm)", "Matched Ion Mass Diff (Ppm)", "Missed Cleavages",
            "Precursor Charge", "Scan Retention Time"]


def psm(f, dct="T", q="0.001", notch="0", ppm="1.0", frag="[b2+1:1.0, y3+1:-2.0]",
        mc="0", ch="2.00000", rt="10.0", qn="0.001"):
    return [f, dct, q, qn, notch, ppm, frag, mc, ch, rt]


@pytest.fixture
def search(work, tmp_path):
    """A minimal finished-search layout: two files, one calibrated and one not."""
    root = tmp_path / "04_search"
    task = root / "mm" / "Task3SearchTask"
    task.mkdir(parents=True)
    cal = root / "mm" / "Task1CalibrationTask"
    cal.mkdir(parents=True)
    (cal / "a-calib.toml").write_text(
        'PrecursorMassTolerance = "\u00b13.1000 PPM"\nProductMassTolerance = "\u00b130.5000 PPM"\n',
        encoding="utf-8")                                  # b did not calibrate: no toml
    (task / "results.txt").write_text(RESULTS, encoding="utf-8")
    rows = ["\t".join(PSM_COLS)]
    rows += ["\t".join(psm("a-calib", mc="1", rt=str(r))) for r in (5.0, 50.0)]
    rows += ["\t".join(psm("a-calib", ch="3.00000"))]
    rows += ["\t".join(psm("b-calib", notch="1"))]          # isotope error: out of the ppm metrics
    rows += ["\t".join(psm("b-calib", dct="D"))]            # decoy: out entirely
    rows += ["\t".join(psm("b-calib", q="0.5"))]            # above 1% FDR: out entirely
    # QC-Q12: passes QValue but fails the notch q-value MetaMorpheus also requires -- out entirely,
    # including from the contaminant share, which used to count it.
    rows += ["\t".join(psm("b-calib", dct="C", qn="0.2"))]
    rows += ["\t".join(psm("b-calib", notch="0|1"))]        # ambiguous notch (S22): out entirely
    (task / "AllPSMs.psmtsv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    peaks = ["File Name\tPeak Detection Type\tPIP Q-Value\tRandom RT\tDecoy Peptide",
             "a-calib\tMSMS\t\tFalse\tFalse",
             "a-calib\tMBR\t0.001\tFalse\tFalse",
             "a-calib\tMBR\t0.5\tFalse\tFalse",            # above threshold: not kept
             "b-calib\tMBR\t0.001\tTrue\tFalse"]           # random RT won: not kept
    (task / "AllQuantifiedPeaks.tsv").write_text("\n".join(peaks) + "\n", encoding="utf-8")

    # SpectralCount_ AND Intensity_: presence is the `_msms` variant (SpectralCount_ > 0), while the
    # contaminant fraction is an intensity ratio, so the two metrics read different columns of the
    # same row. The `C` row is a contaminant: counted by DEF-PROTEINGROUP-1PCT, which is `!IsDecoy`.
    pg = ["Protein Decoy/Contaminant/Target\tProtein QValue"
          "\tSpectralCount_a-calib\tSpectralCount_b-calib\tIntensity_a-calib\tIntensity_b-calib",
          "T\t0.001\t5\t7\t100\t200",     # identified in both runs
          "T\t0.001\t5\t0\t100\t50",      # identified only in a -- but INTENSITY in b (MBR transfer)
          "T\t0.5\t5\t5\t100\t100",       # above 1% FDR: not counted at all
          "D\t0.001\t5\t5\t100\t100",     # decoy: not counted at all
          "C\t0.001\t3\t3\t100\t100"]     # contaminant: counted, and drives the intensity fraction
    (task / "AllQuantifiedProteinGroups.tsv").write_text("\n".join(pg) + "\n", encoding="utf-8")

    qc_dir = tmp_path / "02b_qc"
    qc_dir.mkdir()
    (qc_dir / "qc_report.json").write_text(json.dumps({
        "a.raw": {"pass": True, "run_minutes": 100.0},
        "b.raw": {"pass": True, "run_minutes": 100.0}}), encoding="utf-8")

    work.write(run_date="2026-01-01", fetch={"accession": "PXD000001"},
               search={"metamorpheus_version": "1.1.11"}, qc={})
    return root, qc_dir, tmp_path / "05_qc"


def build(work, search, accession=""):
    root, qc_dir, out = search
    qc_payload.main(str(work.params_path), str(root), str(qc_dir), str(out), accession)
    return json.loads((out / "qc_payload.json").read_text(encoding="utf-8"))


def test_payload_has_the_contracts_shape(work, search):
    p = build(work, search)
    assert p["schema"] == "qc-payload/1"
    assert p["dataset"]["accession"] == "PXD000001"
    assert {f["file"] for f in p["files"]} == {"a", "b"}
    for f in p["files"]:
        for d in f["distributions"].values():
            if isinstance(d, dict):
                assert len(d["edges"]) == len(d["counts"]) + 1
    # We supply values, never verdicts: the derived ratios are qc's to compute.
    assert not {"id_rate", "mbr_msms_ratio"} & set(p["files"][0]["metrics"])


def test_counts_come_from_the_per_file_lines(work, search):
    m = {f["file"]: f["metrics"] for f in build(work, search)["files"]}
    assert m["a"]["ms2_scans"] == 100 and m["a"]["psms"] == 10
    assert m["b"]["peptides"] == 16 and m["b"]["protein_groups"] == 9


def test_per_file_counts_carry_per_file_definitions(work, search):
    """qc 010 QC-Q13: a per-file line comes from that file's own FDR, so it is never filed under the
    dataset definition, and the payload says which PSM population each number describes."""
    p = build(work, search)
    assert p["definitions"]["psms"]["id"] == "aging:DEF-PSM-1PCT-RUN"
    assert p["definitions"]["peptides"]["id"] == "aging:DEF-PEPTIDE-1PCT-RUN"
    assert p["definitions"]["protein_groups"]["id"] == "aging:DEF-PROTEINGROUP-1PCT-RUN"
    assert "DEF-PSM-1PCT-INFILE" in " ".join(p["dataset"]["notes"])


def test_psm_population_is_the_filter_metamorpheus_applies(work, search):
    """QC-Q12: QValue AND QValue Notch <= 0.01, unambiguous notch. b keeps only its Notch-1 target;
    the contaminant that failed the notch q-value and the ambiguous-notch row are both out."""
    m = {f["file"]: f["metrics"] for f in build(work, search)["files"]}
    assert m["b"]["notch_frac"] == 1.0
    assert m["b"]["contaminant_psm_share"] == 0.0
    assert m["b"]["charge_2_frac"] == 1.0


def test_refuses_a_psm_table_without_the_notch_q_value(work, search):
    task = next((search[0] / "mm").glob("Task*SearchTask"))
    f = task / "AllPSMs.psmtsv"
    rows = f.read_text(encoding="utf-8").splitlines()
    i = rows[0].split("\t").index("QValue Notch")
    f.write_text("\n".join("\t".join(c for j, c in enumerate(r.split("\t")) if j != i) for r in rows) + "\n",
                 encoding="utf-8")
    with pytest.raises(SystemExit, match="QValue Notch"):
        build(work, search)


def test_calibration_ok_keys_on_the_toml_and_reads_the_plus_minus_string(work, search):
    m = {f["file"]: f["metrics"] for f in build(work, search)["files"]}
    assert m["a"]["calibration_ok"] is True
    assert m["a"]["cal_precursor_tol_ppm"] == 3.1
    assert m["a"]["cal_product_tol_ppm"] == 30.5      # "±30.5000 PPM" is a string, not a number
    assert m["b"]["calibration_ok"] is False          # no toml written: calibration failed


def test_psm_metrics_use_target_1pct_and_exclude_isotope_errors(work, search):
    m = {f["file"]: f["metrics"] for f in build(work, search)["files"]}
    # a: three PSMs, two with a missed cleavage, one at charge 3
    assert m["a"]["missed_cleavage_frac"] == pytest.approx(2 / 3, abs=1e-3)
    assert m["a"]["charge_3_frac"] == pytest.approx(1 / 3, abs=1e-3)
    # b's only surviving PSM is Notch 1, so it counts for notch_frac but not for the ppm metrics
    assert m["b"]["notch_frac"] == 1.0
    assert "precursor_ppm_median" not in m["b"]
    # RT coverage spans 5..50 of a 100-minute run
    assert m["a"]["id_rt_coverage"] == pytest.approx(0.45, abs=0.02)


def test_mbr_uses_the_shared_def_mbr_kept_rule(work, search):
    m = {f["file"]: f["metrics"] for f in build(work, search)["files"]}
    assert m["a"]["msms_peaks"] == 1
    assert m["a"]["mbr_kept"] == 1        # the q=0.5 row is a row, not a transfer
    assert m["b"]["mbr_kept"] == 0        # random RT won


def test_pg_missing_frac_needs_the_dataset_first(work, search):
    p = build(work, search)
    m = {f["file"]: f["metrics"] for f in p["files"]}
    assert p["dataset_metrics"]["protein_groups_quantified"] == 3      # 2 target + 1 contaminant
    assert p["dataset_metrics"]["runs_per_protein_group"] == {"1": 1, "2": 2}
    assert m["a"]["pg_missing_frac"] == 0.0     # a identified all three
    assert round(m["b"]["pg_missing_frac"], 4) == round(1 / 3, 4)


def test_pg_missing_frac_is_the_msms_variant_so_mbr_cannot_inflate_it(work, search):
    """QC-Q9: presence is `SpectralCount_ > 0`, never `Intensity_ > 0`.

    The fixture's second group has SpectralCount_b = 0 and Intensity_b = 50 -- exactly what a
    match-between-runs transfer looks like. Under the `_any` variant b would score as complete,
    because a neighbouring file identified the group. Under `_msms` it is missing, which is the
    file's own evidence and is the only reading that makes the number a property of the file.
    """
    m = {f["file"]: f["metrics"] for f in build(work, search)["files"]}
    assert m["b"]["pg_missing_frac"] > 0.0
    note = " ".join(build(work, search)["dataset"]["notes"])
    assert "_msms" in note and "_any" in note


def test_pg_missing_frac_is_absent_when_it_cannot_be_measured(work, search, tmp_path):
    """qc's QC-Q8: absent means not measured, 0 means measured and zero.

    A sample group's column block is 2-4 columns wide, so a real run can carry no SpectralCount_
    at all. Reporting 0 there would claim a completeness nobody measured.
    """
    task = next((search[0] / "mm").glob("Task*SearchTask"))
    pg = task / "AllQuantifiedProteinGroups.tsv"
    rows = pg.read_text(encoding="utf-8").splitlines()
    keep = [i for i, h in enumerate(rows[0].split("\t")) if not h.startswith("SpectralCount_")]
    pg.write_text("\n".join("\t".join(r.split("\t")[i] for i in keep) for r in rows) + "\n",
                  encoding="utf-8")
    p = build(work, search)
    assert p["dataset_metrics"]["protein_groups_quantified"] is None
    assert all("pg_missing_frac" not in f["metrics"] for f in p["files"])
    assert "NOT REPORTED" in " ".join(p["dataset"]["notes"])


def test_m13_contamination_metrics(work, search):
    """qc 007 §1. The intensity fraction is QuantProject's DEF-QC-9 v2 at RUN grain; the PSM share
    is `aging:DEF-CONTAM-PSM-RUN` (v2 since QC-Q12) and NOT `DEF-CONTAM-PSM v1`, which our register defines at
    dataset grain -- a per-file share is a different quantity, not the dataset one pushed down.
    """
    p = build(work, search)
    m = {f["file"]: f["metrics"] for f in p["files"]}
    # a: contaminant 100 of every T and C row's intensity (100 + 100 + 100 + 100). DEF-QC-9 states no
    # protein-FDR filter, so the q = 0.5 target row counts too (QC-Q15); the decoy never does.
    assert m["a"]["contaminant_intensity_frac"] == round(100 / 400, 4)
    assert p["definitions"]["contaminant_intensity_frac"]["id"] == "QuantProject:DEF-QC-9"
    assert p["definitions"]["contaminant_psm_share"]["id"] == "aging:DEF-CONTAM-PSM-RUN"
    assert p["definitions"]["pg_missing_frac"]["id"] == "QuantProject:DEF-QC-13"
    assert "_msms" in p["definitions"]["pg_missing_frac"]["source"]


def test_an_acquisition_exception_travels_with_the_numbers(work, search):
    work.write(qc={"acquisition_exception": {
        "granted_by": "user", "granted_date": "2026-09-20", "waives": ["low_res_ms2"],
        "restricts_to": ["abundance"], "bars": ["ptm_stoichiometry"]}})
    note = " ".join(build(work, search)["dataset"]["notes"])
    assert "abundance" in note and "ptm_stoichiometry" in note


def test_refuses_a_payload_it_cannot_name(work, search):
    work.write(fetch={"accession": None})
    with pytest.raises(SystemExit, match="no accession"):
        build(work, search)
