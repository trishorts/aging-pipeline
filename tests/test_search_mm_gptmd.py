"""search_mm.py: extending the GPTMD modification list (D40). No MetaMorpheus needed."""
import pytest

import search_mm

MODS = """ID   GG (Ubiquitination Site)
TG   K
PP   Anywhere.
MT   Trypsin Digested
CF   H6 C4 N2 O2
//
ID   Acetylation
TG   K or X
PP   Anywhere.
MT   Common Biological
//
"""
TOML = 'Something = 1\nListOfModsGptmd = "Common Biological\\tAcetylation on K\\t\\tMetal\\tZinc on E"\nOther = 2\n'
GG = "Trypsin Digested\tGG (Ubiquitination Site) on K"


@pytest.fixture
def known(tmp_path):
    (tmp_path / "Mods.txt").write_text(MODS, encoding="utf-8")
    return search_mm.known_mods(tmp_path)


def test_known_mods_reads_category_and_every_target(known):
    assert ("Trypsin Digested", "GG (Ubiquitination Site) on K") in known
    assert {("Common Biological", "Acetylation on K"), ("Common Biological", "Acetylation on X")} <= known


def test_gg_is_appended_in_metamorpheus_own_encoding(known):
    text, added = search_mm.add_gptmd_mods(TOML, [GG], known)
    assert added == [GG]
    line = [l for l in text.splitlines() if l.startswith("ListOfModsGptmd")][0]
    assert line.endswith('Zinc on E\\t\\tTrypsin Digested\\tGG (Ubiquitination Site) on K"')
    assert "Other = 2" in text and "Something = 1" in text


def test_an_entry_already_listed_is_not_duplicated(known):
    text, _ = search_mm.add_gptmd_mods(TOML, [GG], known)
    again, added = search_mm.add_gptmd_mods(text, [GG], known)
    assert added == [] and again == text


def test_a_name_metamorpheus_does_not_define_is_refused(known):
    with pytest.raises(SystemExit, match="not defined"):
        search_mm.add_gptmd_mods(TOML, ["Trypsin Digested\tGG on R"], known)
