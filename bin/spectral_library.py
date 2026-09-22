"""Spectral libraries, one per organism, built up across searches (user, 2026-09-21).

The user's rule: the FIRST search of an organism writes a spectral library; every search after that
UPDATES it; and the name of every library ever produced is kept so a later search can be rolled back
to an earlier one. This applies to the **Search task only** - calibration and GPTMD never write or
update a library.

MetaMorpheus's side of the contract, read out of the 1.1.11 source rather than assumed:

* `WriteSpectralLibrary` and `UpdateSpectralLibrary` are two independent booleans on
  `SearchParameters`, and `PostSearchAnalysisTask` acts on them in two separate `if` blocks - so
  setting both writes TWO libraries. We never set both.
* An existing library is supplied as another `-d` database. `DbForTask` decides by extension alone
  (`.msp` or `.msl`), and `EverythingRunnerEngine` refuses a run whose databases are *all* libraries,
  so the protein database still has to be there.
* The library is **used during the search**: `ClassicSearchEngine` takes it as an argument. But
  `ModernSearchEngine` does not take one at all, so with `SearchType = "Modern"` a library is loaded,
  never consulted, and still updated afterwards - a silent no-op we flag rather than allow quietly.
* The output name is TIMESTAMPED and lands in the task's own folder:
  `SpectralLibrary_<time>.msp` for a write, `updateSpectralLibrary_<time>.msp` for an update. So the
  produced path cannot be predicted and has to be discovered after the run - which is the mechanical
  reason a registry is needed and not merely convenient.
* An update MERGES: for each (full sequence, charge) it keeps whichever has more evidence - the
  library's spectrum if its matched-ion count exceeds the new PSM's truncated score, otherwise the new
  PSM - then adds every (sequence, charge) the library did not have. So a library only ever grows in
  coverage, and a later search cannot silently delete a peptide an earlier one contributed.

What this module owns: deciding the mode, handing the search stage the library to pass and the TOML
values to set, and recording the result in a registry that is append-only. Libraries live under
`work_root` (never on E:, D3) in their own root, so `cleanup.py` deleting a run's spectra never
touches them.

Registry (`<root>/registry.json`, `aging-spectral-library-registry/1`):

    organisms.<organism>.current    -> the relative path a search should consume, or null
    organisms.<organism>.versions[] -> every library ever written for this organism, in order

Nothing is ever overwritten or removed: `current` moves, versions accumulate. "Go back" means
pointing `current` at an earlier version, which `rollback` does so the file never has to be
hand-edited.

CLI, for inspecting and rolling back outside a pipeline run:

    python spectral_library.py list     <params.json>
    python spectral_library.py rollback <params.json> <organism> <version>
"""
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "aging-spectral-library-registry/1"
# MetaMorpheus's own two output names, from MetaMorpheusTask.cs:1125 and :1139. The trailing `_` is
# deliberate: `SpectralLibrary_*` must not also match `updateSpectralLibrary_*` on a
# case-insensitive filesystem, which Windows is.
WRITE_GLOB = "SpectralLibrary_*.msp"
UPDATE_GLOB = "updateSpectralLibrary_*.msp"
# The search engines that actually consult a library. Anything else loads it and ignores it.
LIBRARY_AWARE_SEARCH_TYPES = {"Classic", None}


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def config(params: dict) -> dict:
    """The `search.spectral_library` block, with its defaults filled in."""
    cfg = dict((params.get("search") or {}).get("spectral_library") or {})
    cfg.setdefault("enabled", False)
    if cfg["enabled"]:
        root = cfg.get("root") or str(Path(params["work_root"]) / "spectral_libraries")
        cfg["root"] = root
    return cfg


def registry_path(cfg: dict) -> Path:
    return Path(cfg["root"]) / "registry.json"


def read_registry(cfg: dict) -> dict:
    p = registry_path(cfg)
    if not p.exists():
        return {"schema": SCHEMA, "organisms": {}}
    reg = json.loads(p.read_text(encoding="utf-8"))
    if reg.get("schema") != SCHEMA:
        sys.exit(f"{p}: registry schema is {reg.get('schema')!r}, this code writes {SCHEMA!r}")
    return reg


def _write_registry(cfg: dict, reg: dict) -> Path:
    """Atomic replace, so an interrupted run cannot leave a half-written registry behind."""
    p = registry_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


class Plan:
    """What the search stage needs to know before it runs."""

    def __init__(self, organism, mode, library_in, parent_version, cfg):
        self.organism = organism
        self.mode = mode                      # "write" | "update"
        self.library_in = library_in          # absolute path to pass with -d, or None
        self.parent_version = parent_version  # the version `current` pointed at when we resolved
        self.cfg = cfg

    @property
    def toml_values(self) -> dict:
        """Never both true: PostSearchAnalysisTask acts on them independently and would write two."""
        return {"WriteSpectralLibrary": self.mode == "write",
                "UpdateSpectralLibrary": self.mode == "update"}

    @property
    def output_glob(self) -> str:
        return WRITE_GLOB if self.mode == "write" else UPDATE_GLOB


def plan(params: dict) -> Plan | None:
    """Decide write-vs-update for this run, or None when the feature is off."""
    cfg = config(params)
    if not cfg["enabled"]:
        return None

    organism = (cfg.get("organism") or "").strip().lower().replace(" ", "_")
    if not organism:
        # Deliberately NOT derived from `discover.organism`: that is a PRIDE facet string chosen to
        # match a search index ("Homo sapiens (human)"), and turning it into a library key by
        # string-munging is the kind of guess that puts mouse spectra in the human library once
        # somebody writes the facet differently. The library key is a fact about the run and is
        # stated, not inferred.
        sys.exit("search.spectral_library.enabled is true but search.spectral_library.organism is "
                 "not set: name the organism the library belongs to (e.g. \"human\", \"mouse\")")

    reg = read_registry(cfg)
    entry = reg["organisms"].get(organism) or {}
    current = entry.get("current")
    if not current:
        return Plan(organism, "write", None, None, cfg)

    lib = Path(cfg["root"]) / current
    if not lib.exists():
        # Do not quietly fall back to `write`. That would start a second chain for this organism and
        # silently discard every spectrum the first chain had accumulated, and the only symptom would
        # be a library that got smaller.
        sys.exit(f"registry says {organism}'s current library is {current}, but {lib} does not "
                 f"exist. Restore it, or roll back to a version that is on disk:\n"
                 f"    python spectral_library.py list <params.json>")
    version = next((v["version"] for v in entry.get("versions", []) if v["path"] == current), None)
    return Plan(organism, "update", str(lib), version, cfg)


def warnings(plan_: "Plan | None", search_params: dict) -> list:
    """Silent no-ops worth refusing to be silent about (user rule: follow up anything suspicious)."""
    if plan_ is None:
        return []
    out = []
    st = search_params.get("search_type")
    if st not in LIBRARY_AWARE_SEARCH_TYPES:
        out.append(
            f"spectral_library: SearchType is {st!r}, and only Classic consults a spectral library - "
            f"ModernSearchEngine does not take one. The library is loaded and IGNORED during the "
            f"search; in update mode it is still merged afterwards, so it grows without ever having "
            f"contributed an identification.")
    return out


def register(plan_: Plan, search_task_dir: Path, run_label: str, accession: str,
             metamorpheus: str) -> dict:
    """Copy the library MetaMorpheus produced into the registry and advance `current`.

    Returns the version record. Raises RuntimeError when the expected file is absent, so the caller
    can flag it without failing a search that otherwise succeeded.
    """
    produced = sorted(search_task_dir.glob(plan_.output_glob))
    if plan_.mode == "write":
        # A write also matches nothing else, but an update run's folder contains BOTH names when a
        # previous write left one behind, so be explicit about which we took.
        produced = [p for p in produced if not p.name.startswith("update")]
    if not produced:
        raise RuntimeError(
            f"spectral_library: {plan_.mode} mode was configured but no {plan_.output_glob} was "
            f"written to {search_task_dir}. The library was NOT advanced.")
    src = produced[-1]

    cfg = plan_.cfg
    reg = read_registry(cfg)
    entry = reg["organisms"].setdefault(plan_.organism, {"current": None, "versions": []})

    # Optimistic concurrency. Two searches of one organism running at once would both resolve the
    # same parent and the second would silently discard the first's contribution, so refuse instead.
    if entry.get("current") is not None or plan_.parent_version is not None:
        live = next((v["version"] for v in entry.get("versions", []) if v["path"] == entry.get("current")), None)
        if live != plan_.parent_version:
            raise RuntimeError(
                f"spectral_library: {plan_.organism}'s current library moved from version "
                f"{plan_.parent_version} to {live} while this search was running. Another search "
                f"registered in between; this run's library is at {src} and has NOT been registered, "
                f"so nothing was lost - merge it deliberately rather than by race.")

    version = max((v["version"] for v in entry["versions"]), default=0) + 1
    rel = f"{plan_.organism}/{plan_.organism}.v{version:03d}.msp"
    dest = Path(cfg["root"]) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    # COPY, not move: the run folder stays a faithful record of what MetaMorpheus wrote, and the
    # registry holds the curated chain. The two are checked against each other by sha256.
    shutil.copy2(src, dest)

    rec = {
        "version": version,
        "path": rel,
        "mode": plan_.mode,
        "parent_version": plan_.parent_version,
        "sha256": _sha256(dest),
        "n_spectra": count_spectra(dest),
        "bytes": dest.stat().st_size,
        "written_utc": _now(),
        "produced_by": {
            "run": run_label,
            "accession": accession,
            "metamorpheus": metamorpheus,
            "metamorpheus_path": str(src),
            "metamorpheus_filename": src.name,
        },
    }
    entry["versions"].append(rec)
    entry["current"] = rel
    _write_registry(cfg, reg)
    return rec


def count_spectra(msp: Path) -> int:
    """`Name:` lines in an MSP - one per library spectrum. Cheap, and it makes a library that grew
    smaller visible in the registry instead of only in a file size."""
    n = 0
    with msp.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("Name:"):
                n += 1
    return n


# ------------------------------------------------------------------ CLI: inspect and roll back

def _cli_list(params: dict) -> None:
    cfg = config(params)
    if not cfg["enabled"]:
        print("search.spectral_library.enabled is false for these params")
        return
    reg = read_registry(cfg)
    print(f"registry {registry_path(cfg)}")
    if not reg["organisms"]:
        print("  (no libraries yet)")
    for organism, entry in sorted(reg["organisms"].items()):
        print(f"\n{organism}   current: {entry.get('current') or '(none)'}")
        for v in entry.get("versions", []):
            mark = "->" if v["path"] == entry.get("current") else "  "
            by = v["produced_by"]
            print(f"  {mark} v{v['version']:03d}  {v['n_spectra']:>8,} spectra  {v['mode']:<6} "
                  f"parent={v['parent_version']}  {by['accession']}  {by['run']}  {v['written_utc']}")
            print(f"        {v['path']}  sha256={v['sha256'][:16]}...")


def _cli_rollback(params: dict, organism: str, version: str) -> None:
    cfg = config(params)
    reg = read_registry(cfg)
    entry = reg["organisms"].get(organism)
    if not entry:
        sys.exit(f"no libraries registered for {organism!r}")
    want = int(version)
    rec = next((v for v in entry["versions"] if v["version"] == want), None)
    if rec is None:
        sys.exit(f"{organism} has no version {want} (have "
                 f"{[v['version'] for v in entry['versions']]})")
    path = Path(cfg["root"]) / rec["path"]
    if not path.exists():
        sys.exit(f"version {want} is registered as {rec['path']} but that file is missing")
    # Versions are never removed, so rolling back and then searching again appends a NEW version
    # whose parent is the one we rolled back to. The chain records the decision rather than hiding it.
    was = entry.get("current")
    entry["current"] = rec["path"]
    _write_registry(cfg, reg)
    print(f"{organism}: current {was} -> {rec['path']} (v{want}, {rec['n_spectra']:,} spectra)\n"
          f"the next search of {organism} will update from this version")


def main(argv) -> None:
    if len(argv) < 2:
        sys.exit(__doc__.strip().splitlines()[-4].strip() if False else
                 "usage: spectral_library.py list <params.json>\n"
                 "       spectral_library.py rollback <params.json> <organism> <version>")
    action, params_path, *rest = argv
    params = json.loads(Path(params_path).read_text(encoding="utf-8"))
    if action == "list":
        _cli_list(params)
    elif action == "rollback":
        if len(rest) != 2:
            sys.exit("usage: spectral_library.py rollback <params.json> <organism> <version>")
        _cli_rollback(params, rest[0].strip().lower().replace(" ", "_"), rest[1])
    else:
        sys.exit(f"unknown action {action!r}: expected `list` or `rollback`")


if __name__ == "__main__":
    main(sys.argv[1:])
