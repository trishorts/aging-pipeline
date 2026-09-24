"""D53: DEF-CONTAM-INT-SHARED, the upper bound beside DEF-QC-9's lower bound."""
import contam_shared as cs


def _xml(path, accs):
    path.write_text("<uniprot>" + "".join(
        f"<entry dataset=\"x\">\n<accession>{a}</accession>\n<accession>{a}-SEC</accession>\n</entry>\n" for a in accs)
        + "</uniprot>", encoding="utf-8")
    return path


def test_shared_accessions_are_primary_accessions_in_both(tmp_path):
    contam = _xml(tmp_path / "c.xml", ["P02768", "P02769", "P35527"])
    human = _xml(tmp_path / "h.xml", ["P02768", "P35527", "Q8WZ42"])
    shared, sha = cs.shared_accessions(contam, [human])
    assert shared == ["P02768", "P35527"]          # secondary accessions ignored, bovine albumin not shared
    assert len(sha) == 64


def _row(acc, td, a, b="0"):
    return {"Protein Accession": acc, "Protein Decoy/Contaminant/Target": td, "Intensity_f1": a, "Intensity_f2": b}


def test_upper_bound_counts_all_shared_groups_and_never_mixed_ones():
    rows = [_row("Q8WZ42", "T", "60"),             # ordinary target
            _row("P02769", "C", "10"),             # a real C (bovine albumin): in both bounds
            _row("P02768", "T", "20"),             # human albumin, shared: T under RemoveContaminant
            _row("P35527|Q8WZ43", "T", "10"),      # mixed group: not counted (conservative)
            _row("P99999", "D", "500")]            # decoy: never counted
    up = cs.share_per_file(rows, ["P02768", "P35527"])
    assert up["f1"] == round(30 / 100, 4)          # C 10 + shared 20, over T+C 100
    assert up["f2"] is None                        # no intensity at all: not measurable, not 0


def test_with_nothing_shared_the_upper_bound_equals_the_lower():
    rows = [_row("Q8WZ42", "T", "90"), _row("P02769", "C", "10")]
    assert cs.share_per_file(rows, [])["f1"] == 0.1
