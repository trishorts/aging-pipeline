"""provenance.py: the record every stage writes (docs/provenance.md)."""
import hashlib, json

import provenance
from provenance import Provenance, file_entry


def test_file_entry_is_relative_under_work_root_and_absolute_outside(tmp_path):
    root = tmp_path / "work"; (root / "a").mkdir(parents=True)
    inside = root / "a" / "x.txt"; inside.write_text("hello")
    outside = tmp_path / "y.txt"; outside.write_text("hi")

    e = file_entry(inside, root)
    assert e == {"path": "a/x.txt", "root": "work_root", "size_bytes": 5,
                 "sha256": hashlib.sha256(b"hello").hexdigest()}
    e = file_entry(outside, root)
    assert "root" not in e and e["path"] == str(outside.resolve())


def test_upstream_hash_is_reused_for_inputs_and_named(work):
    data = work.root / "run" / "f.raw"; data.parent.mkdir(); data.write_bytes(b"spectra")
    up = work.root / "run" / "provenance.json"
    up.write_text(json.dumps({"stage": "fetch", "roots": {"work_root": str(work.root)},
                              "outputs": [{"path": "run/f.raw", "root": "work_root",
                                           "size_bytes": 7, "sha256": "cafe"}]}), encoding="utf-8")
    p = Provenance("t", work.params_path, "none")
    p.upstream(up)
    p.inputs(data)
    assert p.rec["upstream"][0]["stage"] == "fetch"
    assert p.rec["inputs"][0]["sha256"] == "cafe" and p.rec["inputs"][0]["sha256_from"] == "fetch"
    p.outputs(data)                                   # outputs are always hashed fresh
    assert p.rec["outputs"][0]["sha256"] == hashlib.sha256(b"spectra").hexdigest()


def test_missing_upstream_is_noted_not_fatal(work):
    p = Provenance("t", work.params_path, "none")
    p.upstream(work.root / "nope" / "provenance.json")
    assert p.rec["upstream"] == [] and "expected upstream provenance missing" in p.rec["notes"][0]


def test_written_record_has_the_documented_common_fields(work):
    work.write(qc={"_rule": "kept", "min_ms2": 1})
    out = work.root / "out"; out.mkdir()
    Provenance("qc_spectra", work.params_path, "qc").write(out)
    rec = json.loads((out / "provenance.json").read_text(encoding="utf-8"))
    for k in ("schema", "stage", "started_utc", "finished_utc", "host", "pipeline", "params_file",
              "params", "run_date", "roots", "tools", "commands", "upstream", "inputs", "outputs",
              "notes", "resources"):
        assert k in rec, k
    assert rec["schema"] == "aging-provenance/3"
    assert rec["params"] == {"_rule": "kept", "min_ms2": 1}     # a section's own notes are copied
    assert set(rec["pipeline"]) == {"version", "repo", "commit"}
    assert rec["resources"]["wall_s"] >= 0 and "output_bytes" in rec["resources"]


def test_version_file_is_read():
    assert provenance.pipeline_version() == (provenance.Path(provenance.__file__).resolve().parent.parent
                                             / "VERSION").read_text(encoding="utf-8").strip()


def test_low_core_use_flag(work, monkeypatch):
    out = work.root / "o"; out.mkdir()
    p = Provenance("t", work.params_path, "none")
    p.rec["expected_cores"] = 32
    monkeypatch.setattr(p.monitor, "result", lambda: {"wall_s": 10.0, "avg_cores_used": 2.0})
    p.write(out)
    assert any(f.startswith("low_core_use") for f in p.rec["flags"])
