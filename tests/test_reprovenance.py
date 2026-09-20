"""reprovenance.py: re-derive a finished search's metrics without re-running the search.

The case these tests pin down is the real one (S21/S23): a run whose `provenance.json` was written when
`id_rate.psms_1pct` meant the FDR engine's count, re-derived so that it means `aging DEF-PSM-1PCT v1`.
What matters is not only that the number changes, but that the *history* around it does not, and that
the file says out loud that it was re-derived.
"""
import json

import pytest

import db_prepare, qc_spectra, reprovenance, search_mm


def run_stage(fn, *args):
    try:
        fn(*args)
        return 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1


@pytest.fixture
def searched(layout):
    """A completed stage-4 output, with its provenance.json."""
    L = layout
    assert run_stage(db_prepare.main, L.params, str(L.root / "db")) == 0
    assert run_stage(qc_spectra.main, L.params, str(L.spectra), str(L.run / "02b_qc")) == 0
    out = L.run / "04_search"
    assert run_stage(search_mm.main, L.params, str(L.spectra), str(out)) == 0
    return L, out


def rec(out):
    return json.loads((out / "provenance.json").read_text(encoding="utf-8"))


def age_it(out, **id_rate):
    """Rewrite the record to look like an older schema, the way the 18-file run's really does."""
    r = rec(out)
    r["schema"] = "aging-provenance/2"
    r["id_rate"] = id_rate
    r.pop("contamination", None)
    r["flags"] = [f for f in r["flags"] if not f.startswith(("low_id_rate", "high_contamination"))]
    (out / "provenance.json").write_text(json.dumps(r, indent=2), encoding="utf-8")
    return r


def test_rederives_the_psm_definition_and_says_so(searched):
    L, out = searched
    fresh = rec(out)
    canonical = fresh["id_rate"]["psms_1pct"]
    engine = fresh["id_rate"]["psms_fdr_engine_1pct"]
    assert engine != canonical, "the fake search must print two different PSM counts for this to mean anything"
    before = age_it(out, psms_1pct=engine, ms2=fresh["id_rate"]["ms2"],
                    rate=round(engine / fresh["id_rate"]["ms2"], 4))

    reprovenance.main(L.params, str(out))
    after = rec(out)

    assert after["schema"] == "aging-provenance/3"
    assert after["id_rate"]["psms_1pct"] == canonical
    assert after["id_rate"]["definition"] == "aging DEF-PSM-1PCT v1"
    assert after["id_rate"]["psms_fdr_engine_1pct"] == engine, "the superseded count is kept, not deleted"

    entry = after["rederived"][-1]
    assert entry["from_schema"] == "aging-provenance/2" and entry["to_schema"] == "aging-provenance/3"
    assert entry["changes"]["id_rate"]["psms_1pct"] == {"was": engine, "now": canonical}
    assert entry["pipeline"]["commit"]
    assert before["id_rate"]["psms_1pct"] == engine  # the fixture really did start from the old value


def test_history_is_never_rewritten(searched):
    L, out = searched
    before = rec(out)
    age_it(out, psms_1pct=1, ms2=before["id_rate"]["ms2"], rate=0.1)
    reprovenance.main(L.params, str(out))
    after = rec(out)
    # Everything that records what HAPPENED must survive byte-for-byte.
    for k in ("stage", "started_utc", "finished_utc", "host", "commands", "tools",
              "inputs", "outputs", "upstream", "resources", "exit_code", "success"):
        assert after[k] == before[k], f"{k} is history and must not be re-derived"


def test_adds_a_block_that_did_not_exist(searched):
    L, out = searched
    assert "contamination" in rec(out)
    age_it(out, psms_1pct=1, ms2=1, rate=1.0)
    assert "contamination" not in rec(out)
    reprovenance.main(L.params, str(out))
    after = rec(out)
    assert "contamination" in after
    assert after["rederived"][-1]["changes"]["contamination"] == "added (absent before)"


def test_is_idempotent(searched):
    L, out = searched
    reprovenance.main(L.params, str(out))
    once = rec(out)
    reprovenance.main(L.params, str(out))
    twice = rec(out)
    assert twice["id_rate"] == once["id_rate"]
    assert twice["flags"] == once["flags"]
    assert twice["rederived"][-1]["changes"] == "none", "a second pass changes nothing and says so"
    assert len(twice["rederived"]) == len(once["rederived"]) + 1, "but it is still recorded"


def test_recovers_the_spectra_from_the_record(searched):
    L, out = searched
    files = reprovenance.spectra_from_record(rec(out))
    assert [f.name for f in files] == ["a.raw", "b.raw"]


def test_refuses_a_stage_it_does_not_own(searched):
    L, out = searched
    qc = L.run / "02b_qc"
    with pytest.raises(SystemExit) as e:
        reprovenance.main(L.params, str(qc))
    assert "reprovenance only handles" in str(e.value)


def test_refuses_an_unsuccessful_run(searched):
    L, out = searched
    r = rec(out); r["success"] = False
    (out / "provenance.json").write_text(json.dumps(r), encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        reprovenance.main(L.params, str(out))
    assert "unsuccessful" in str(e.value)


def test_missing_spectra_are_recorded_not_ignored(searched):
    """Raw files are deleted after a run (G14). Re-deriving then is fine, but the record must say so."""
    L, out = searched
    age_it(out, psms_1pct=1, ms2=1, rate=1.0)
    for f in L.spectra.glob("*.raw"):
        f.unlink()
    reprovenance.main(L.params, str(out))
    after = rec(out)
    assert after["rederived"][-1]["spectra_absent"] == ["a.raw", "b.raw"]
    assert any("no longer on disk" in n for n in after["notes"])
