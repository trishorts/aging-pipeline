"""The per-organism spectral library: write once, update thereafter, and keep every version.

User's rule (2026-09-21): the first search of an organism writes a spectral library, every search
after that updates it, the name of each library is kept so we can go back, and it applies to the
SEARCH TASK only.

These tests drive real `search_mm.main()` against `fake_metamorpheus.py`, which writes the library
under MetaMorpheus's own timestamped names and merges a loaded library's spectra on an update, so the
chain can be checked by counting spectra rather than by trusting the plumbing.
"""
import json
from pathlib import Path

import pytest

import db_prepare, qc_spectra, search_mm
import spectral_library


@pytest.fixture(autouse=True)
def ready(layout):
    """Stages 1 and 2b, which stage 4 refuses to run without."""
    stage(db_prepare.main, layout.params, str(layout.root / "db"))
    stage(qc_spectra.main, layout.params, str(layout.spectra), str(layout.run / "02b_qc"))


def stage(fn, *args):
    try:
        fn(*args)
        return 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1


def prov(path):
    return json.loads((path / "provenance.json").read_text(encoding="utf-8"))


def enable(work, layout, **over):
    """Turn the library on for these params, leaving everything else as `layout` set it."""
    search = dict(work.params()["search"])
    search["spectral_library"] = {"enabled": True, "organism": "human", **over}
    work.write(search=search, fetch={"accession": layout.acc})
    return search


def run_search(layout, n):
    """A search into its own output folder (MetaMorpheus refuses to reuse one)."""
    out = layout.run / f"04_search_{n}"
    assert stage(search_mm.main, layout.params, str(layout.spectra), str(out)) == 0
    return out


def registry(layout):
    return json.loads((layout.root / "spectral_libraries" / "registry.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- off by default

def test_off_by_default_changes_nothing(layout):
    """Every params file written before today has no `spectral_library` block, and must behave
    exactly as it did: neither setting touched, no library passed, no registry created."""
    out = run_search(layout, "a")
    toml = (out / "tasks" / "3_SearchTask.toml").read_text(encoding="utf-8")
    assert "WriteSpectralLibrary = false" in toml and "UpdateSpectralLibrary = false" in toml
    rec = prov(out)
    assert "spectral_library" not in rec
    assert not (layout.root / "spectral_libraries").exists()
    assert not any(a.endswith(".msp") for a in rec["commands"][-1])


# ---------------------------------------------------------------- first search writes

def test_first_search_of_an_organism_writes_the_library(layout, work):
    enable(work, layout)
    out = run_search(layout, "a")

    toml = (out / "tasks" / "3_SearchTask.toml").read_text(encoding="utf-8")
    assert "WriteSpectralLibrary = true" in toml
    assert "UpdateSpectralLibrary = false" in toml          # never both: MM would write two libraries
    # Nothing to consume on a first search, so no .msp among the databases.
    assert not any(a.endswith(".msp") for a in prov(out)["commands"][-1])

    lib = prov(out)["spectral_library"]
    assert lib["organism"] == "human" and lib["mode"] == "write" and lib["parent_version"] is None
    assert lib["written"]["version"] == 1
    assert lib["written"]["n_spectra"] == 2                 # the fake writes two Name: lines
    assert lib["written"]["path"] == "human/human.v001.msp"
    assert (layout.root / "spectral_libraries" / "human" / "human.v001.msp").exists()

    reg = registry(layout)
    assert reg["schema"] == "aging-spectral-library-registry/1"
    assert reg["organisms"]["human"]["current"] == "human/human.v001.msp"
    assert [v["version"] for v in reg["organisms"]["human"]["versions"]] == [1]
    # The name MetaMorpheus actually used is timestamped and unpredictable, so it is recorded rather
    # than reconstructed - that is the whole reason the registry exists.
    assert reg["organisms"]["human"]["versions"][0]["produced_by"]["metamorpheus_filename"].startswith(
        "SpectralLibrary_")


def test_the_library_booleans_are_set_on_the_search_task_only(layout, work):
    """User: 'this will apply only to the search task.'"""
    enable(work, layout)
    out = run_search(layout, "a")
    for name in ("1_CalibrationTask.toml", "2_GptmdTask.toml"):
        text = (out / "tasks" / name).read_text(encoding="utf-8")
        assert "SpectralLibrary" not in text, f"{name} must not acquire a library setting"


# ---------------------------------------------------------------- later searches update

def test_the_second_search_updates_and_the_chain_grows(layout, work):
    enable(work, layout)
    run_search(layout, "a")
    out2 = run_search(layout, "b")

    toml = (out2 / "tasks" / "3_SearchTask.toml").read_text(encoding="utf-8")
    assert "UpdateSpectralLibrary = true" in toml and "WriteSpectralLibrary = false" in toml

    rec = prov(out2)
    # v001 is consumed as another -d: DbForTask decides a database is a library by extension alone.
    assert any(a.endswith("human.v001.msp") for a in rec["commands"][-1])
    lib = rec["spectral_library"]
    assert lib["mode"] == "update" and lib["parent_version"] == 1
    assert lib["written"]["version"] == 2 and lib["written"]["parent_version"] == 1
    # 2 carried from v001 + 1 new. An update merges rather than replacing, so coverage only grows.
    assert lib["written"]["n_spectra"] == 3

    reg = registry(layout)
    assert reg["organisms"]["human"]["current"] == "human/human.v002.msp"
    assert [v["version"] for v in reg["organisms"]["human"]["versions"]] == [1, 2]
    # v001 is still on disk: versions accumulate so an earlier one can be returned to.
    assert (layout.root / "spectral_libraries" / "human" / "human.v001.msp").exists()


def test_a_third_search_keeps_growing_and_every_version_is_kept(layout, work):
    enable(work, layout)
    for n in ("a", "b", "c"):
        run_search(layout, n)
    reg = registry(layout)
    versions = reg["organisms"]["human"]["versions"]
    assert [v["version"] for v in versions] == [1, 2, 3]
    assert [v["n_spectra"] for v in versions] == [2, 3, 4]
    assert [v["mode"] for v in versions] == ["write", "update", "update"]
    assert [v["parent_version"] for v in versions] == [None, 1, 2]
    for v in versions:
        assert (layout.root / "spectral_libraries" / v["path"]).exists()


def test_each_organism_has_its_own_library(layout, work):
    """User: 'there should be one for human, one for mouse, one for rat and so forth.'"""
    enable(work, layout)
    run_search(layout, "a")
    enable(work, layout, organism="mouse")
    out = run_search(layout, "b")
    # A mouse search finds no mouse library, so it WRITES rather than updating a human one.
    assert prov(out)["spectral_library"]["mode"] == "write"
    reg = registry(layout)
    assert set(reg["organisms"]) == {"human", "mouse"}
    assert reg["organisms"]["mouse"]["current"] == "mouse/mouse.v001.msp"
    assert reg["organisms"]["human"]["current"] == "human/human.v001.msp"


# ---------------------------------------------------------------- going back

def test_rollback_points_current_at_an_earlier_version_and_the_next_search_builds_on_it(layout, work):
    """User: 'so that we may go back if we need to and make adjustments.'"""
    enable(work, layout)
    for n in ("a", "b", "c"):
        run_search(layout, n)
    params = json.loads(Path(layout.params).read_text(encoding="utf-8"))

    spectral_library._cli_rollback(params, "human", "1")
    assert registry(layout)["organisms"]["human"]["current"] == "human/human.v001.msp"

    out = run_search(layout, "d")
    lib = prov(out)["spectral_library"]
    assert lib["parent_version"] == 1                 # built on the rolled-back version
    assert lib["written"]["version"] == 4             # appended, never overwriting v2/v3
    assert lib["written"]["n_spectra"] == 3           # v001's 2 + 1, not v003's 4 + 1
    assert [v["version"] for v in registry(layout)["organisms"]["human"]["versions"]] == [1, 2, 3, 4]


def test_rollback_refuses_a_version_that_does_not_exist(layout, work):
    enable(work, layout)
    run_search(layout, "a")
    params = json.loads(Path(layout.params).read_text(encoding="utf-8"))
    with pytest.raises(SystemExit, match="no version 7"):
        spectral_library._cli_rollback(params, "human", "7")


# ---------------------------------------------------------------- refusals and silent no-ops

def test_enabled_without_an_organism_is_refused(layout, work):
    search = dict(work.params()["search"])
    search["spectral_library"] = {"enabled": True}
    work.write(search=search)
    assert stage(search_mm.main, layout.params, str(layout.spectra), str(layout.run / "04_x")) == 1


def test_a_missing_current_library_is_refused_rather_than_silently_restarting_the_chain(layout, work):
    """Falling back to `write` would start a second chain and silently discard every spectrum the
    first had accumulated - visible only as a library that got smaller."""
    enable(work, layout)
    run_search(layout, "a")
    (layout.root / "spectral_libraries" / "human" / "human.v001.msp").unlink()
    assert stage(search_mm.main, layout.params, str(layout.spectra), str(layout.run / "04_b")) == 1


def test_a_search_type_that_ignores_the_library_is_flagged(layout, work):
    """ModernSearchEngine takes no spectral library at all, so with SearchType = "Modern" the library
    is loaded, never consulted, and still updated afterwards. That is a silent no-op, so it is said."""
    search = dict(work.params()["search"])
    search["search_type"] = "Modern"
    search["spectral_library"] = {"enabled": True, "organism": "human"}
    work.write(search=search, fetch={"accession": layout.acc})
    out = run_search(layout, "a")
    notes = " ".join(prov(out)["notes"])
    assert "only Classic consults a spectral library" in notes


def test_a_failed_search_does_not_advance_the_library(layout, work, monkeypatch):
    """A partial library from a failed run must not become the parent of the next one."""
    enable(work, layout)
    monkeypatch.setenv("FAKE_MM_NO_PROTEIN_GROUPS", "1")       # exit 0, missing a key output
    out = layout.run / "04_bad"
    assert stage(search_mm.main, layout.params, str(layout.spectra), str(out)) == 1
    rec = prov(out)
    assert rec["success"] is False
    assert "written" not in rec["spectral_library"]
    assert not (layout.root / "spectral_libraries" / "registry.json").exists()


def test_a_configured_library_that_was_never_written_is_flagged_not_swallowed(layout, work, monkeypatch):
    """If MetaMorpheus is asked for a library and does not produce one, the search still stands but
    the ledger has to say the library did not advance."""
    enable(work, layout)
    monkeypatch.setenv("FAKE_MM_NO_SPECTRAL_LIBRARY", "1")
    out = run_search(layout, "a")
    rec = prov(out)
    assert rec["success"] is True
    assert "no SpectralLibrary_*.msp" in rec["spectral_library"]["error"]
    assert any("was NOT advanced" in f for f in rec["flags"])
    assert not (layout.root / "spectral_libraries" / "registry.json").exists()


# ---------------------------------------------------------------- registry integrity

def test_the_registry_records_a_hash_so_the_copy_can_be_checked_against_the_run(layout, work):
    enable(work, layout)
    out = run_search(layout, "a")
    rec = prov(out)["spectral_library"]["written"]
    dest = layout.root / "spectral_libraries" / rec["path"]
    assert spectral_library._sha256(dest) == rec["sha256"]
    # The run folder keeps what MetaMorpheus wrote; the registry holds the curated chain. Both exist.
    assert Path(rec["produced_by"]["metamorpheus_path"]).exists()


def test_a_concurrent_registration_is_refused_rather_than_silently_discarded(layout, work):
    """Two searches of one organism at once would both resolve the same parent, and the second to
    finish would silently drop the first's contribution."""
    enable(work, layout)
    run_search(layout, "a")                                   # v001
    params = json.loads(Path(layout.params).read_text(encoding="utf-8"))
    cfg = spectral_library.config(params)
    plan = spectral_library.plan(params)                      # resolves parent = 1
    run_search(layout, "b")                                   # someone else registers v002
    search_dir = next((layout.run / "04_search_b" / "mm").glob("Task*SearchTask"))
    with pytest.raises(RuntimeError, match="moved from version 1 to 2"):
        spectral_library.register(plan, search_dir, run_label="r", accession="X", metamorpheus="1.1.11")
