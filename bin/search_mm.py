"""Stage 4 - MetaMorpheus: Calibration -> GPTMD -> Search (+ FlashLFQ), in ONE CLI invocation.

It calls the official MetaMorpheus command-line tool (CMD) directly: the workflow engine calls the tools
(bridge 003), and no Python wrapper sits in between. `search.metamorpheus_cmd` is either the CMD
executable (Windows `CMD.exe`) or `CMD.dll`, which runs as `dotnet CMD.dll` on any OS with the .NET
runtime the release targets (1.1.11: net10.0). The .dll form is the Linux and CI path.

Rules for running it:
  * default task TOMLs are generated ON THE NODE THAT RUNS (`CMD -g`), then only the settings named
    in params are changed (today: threads);
  * one invocation per dataset; MetaMorpheus chains the tasks internally;
  * a fresh output directory every run (MetaMorpheus never cleans an existing one);
  * the Thermo licence is accepted explicitly (--acceptThermoLicence, a params setting), and settings
    live in a writable --mmsettings dir;
  * FlashLFQ can fail with exit 0, so success requires AllQuantifiedProteinGroups.tsv to exist.

usage: search_mm.py <params.json> <spectra_dir_or_file> <out_dir>
"""
import collections, csv, json, re, shutil, subprocess, sys, time
import threading
from pathlib import Path

import spectral_library
from provenance import Provenance, file_entry, sha256

TASK_FILE = {"Calibration": "CalibrationTask.toml", "Gptmd": "GptmdTask.toml", "Search": "SearchTask.toml"}
SEARCH_TYPES = {"Classic", "Modern", "NonSpecific"}
# QC failures an acquisition exception may NOT forgive, whatever it names. A low-resolution MS2 is
# an acquisition choice and can be waived with the restriction recorded (D24); a file with almost
# no spectra, or one the reader cannot open, is not a choice about acquisition at all.
NEVER_WAIVABLE = {"too_few_ms2", "unreadable"}


MBR_FDR_THRESHOLD = 0.01   # SearchParameters.MbrFdrThreshold default; not yet read from the TOML


def kill_tree(proc) -> None:
    """Terminate a process and its descendants.

    `dotnet CMD.dll` runs the search as a CHILD of the launcher, and on Windows there is no
    process-group kill, so `proc.kill()` alone leaves the real search running.
    """
    try:
        import psutil
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except Exception:
                pass
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def classify_peak(row: dict, thr: float = MBR_FDR_THRESHOLD):
    """Classify one `AllQuantifiedPeaks.tsv` row. Returns ``(kind, random_rt_won)``.

    `kind` is ``"msms"``, ``"mbr_kept"``, ``"mbr_other"`` or ``None`` (a row that is neither).
    **This is the single implementation of QuantProject's DEF-MBR-KEPT v1** (thread 009, checked
    against MM 1.1.10 / mzLib 1.0.589): the peaks table is written UNFILTERED, so a raw MBR row is not
    a transfer used in quant. The headline is kept / msms, never rows / msms — counting rows is what
    produced the false S4 alarm.

    It lives here rather than inside `derive_metrics` because `qc_payload.py` needs the same rule
    **per file** while `derive_metrics` needs it per dataset, and a definition with two
    implementations is a definition that will drift.
    """
    kind = row.get("Peak Detection Type")
    if kind == "MSMS":
        return "msms", False
    if kind != "MBR":
        return None, False
    random_rt = (row.get("Random RT") or "").lower() == "true"
    try:
        q = float(row.get("PIP Q-Value") or "nan")
    except ValueError:
        q = float("nan")
    decoy = (row.get("Decoy Peptide") or "").lower() == "true"
    kept = q < thr and not random_rt and not decoy
    return ("mbr_kept" if kept else "mbr_other"), random_rt


def derive_metrics(out: Path, params: dict, spectra_files, qc: Path):
    """Recompute every DERIVED block of a search stage from the MetaMorpheus outputs already on disk.

    Reads `out`, never runs MetaMorpheus, and returns ``(blocks, flags, notes)`` instead of touching a
    Provenance record. `main` calls it at the end of a run; `reprovenance.py` calls it to re-derive an
    OLD run's numbers under today's definitions without spending the compute again.

    That second caller is why this is a function. Definitions move (S21, S23): `psms_1pct` meant the
    FDR-engine count in `aging-provenance/2` and means `aging DEF-PSM-1PCT v1` from /3 on. A run searched
    under a superseded definition is not wrong, it is *labelled* wrong, and re-searching 18 files for 21
    minutes to correct a label would be a manual workaround wearing a pipeline's clothes (D4).
    """
    p = params["search"]
    mm = out / "mm"
    search_dirs = sorted(mm.glob("Task*SearchTask"))
    blocks, flags, notes = {}, [], []
    # Automatic suspicion flags (user: follow up on anything suspicious). They land in provenance.json
    # and feed results/SUSPICIOUS.md; they never fail the stage by themselves.
    log_text = (out / "metamorpheus.log").read_text(encoding="utf-8", errors="replace")
    if "Calibration failure" in log_text:
        flags.append("calibration_failed: GPTMD/search ran on uncalibrated spectra (S7)")
    if search_dirs and (search_dirs[-1] / "results.txt").exists():
        # results.txt prints two different counts (S21). aging DEF-PSM-1PCT v1 is the summary line, target PSMs
        # only; the FDR engine's log line ("PSMs within 1% FDR", the first of several) is higher and is kept
        # beside it under its own definition, so neither is mistaken for the other.
        txt = (search_dirs[-1] / "results.txt").read_text(encoding="utf-8", errors="replace")
        m = re.search(r"All target PSMs with q-value <= 0\.01: (\d+)", txt)
        e = re.search(r"PSMs within 1% FDR: (\d+)", txt)
        psms = int(m.group(1)) if m else None
        ms2 = sum(r["ms2"] for r in json.loads(qc.read_text(encoding="utf-8")).values())
        blocks["id_rate"] = {"definition": "aging DEF-PSM-1PCT v1", "psms_1pct": psms, "ms2": ms2,
                               "rate": round(psms / ms2, 4) if psms and ms2 else None,
                               "psms_fdr_engine_1pct": int(e.group(1)) if e else None,
                               "psms_fdr_engine_definition": "aging DEF-PSM-FDRENGINE v1"}
        if psms is not None and ms2 and psms / ms2 < p.get("flag_min_id_rate", 0.15):
            # Two decimals, deliberately: this dataset's canonical rate is 9.98% and the superseded
            # FDR-engine rate was 10.49%. At one decimal they read 10.0% and 10.5%, which makes the
            # correction that S23 is about look like a rounding wobble.
            flags.append(f"low_id_rate: {psms}/{ms2} = {psms / ms2:.2%} of MS2 identified (S3)")
    peaks = next(iter(sorted(search_dirs[-1].glob("AllQuantifiedPeaks.tsv"))), None) if search_dirs else None
    if peaks:

        # QuantProject DEF-MBR-ROW / DEF-MBR-KEPT v1 (thread 009, checked against MM 1.1.10 / mzLib 1.0.589):
        # the peaks table is written UNFILTERED, so raw MBR rows are not transfers used in quant. The
        # headline is kept / msms, never rows / msms (our first S4 flag used rows and was wrong).
        thr = MBR_FDR_THRESHOLD
        rows = kept = random_won = msms = 0
        with peaks.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh, delimiter="\t"):
                kind, is_random = classify_peak(r, thr)
                if kind == "msms":
                    msms += 1
                elif kind is not None:
                    rows += 1
                    random_won += is_random
                    if kind == "mbr_kept":
                        kept += 1
        blocks["mbr"] = {"definition": "QuantProject DEF-QC-MBR v1", "mbr_rows": rows, "mbr_random_rt_won": random_won,
                           "mbr_kept": kept, "msms_peaks": msms, "mbr_fdr_threshold": thr,
                           "kept_over_msms": round(kept / msms, 3) if msms else None}
        if msms and kept > msms:
            flags.append(f"mbr_kept_exceeds_msms: kept MBR {kept} > MSMS {msms} (DEF-MBR-KEPT v1)")
    # Degree of contamination (user: a good QC value), checked against MetaMorpheus 1.1.11: a row is a
    # contaminant when `Decoy/Contaminant/Target` (PSMs) or `Protein Decoy/Contaminant/Target` (protein
    # groups) is exactly "C". PSM share = aging DEF-CONTAM-PSM v1: QValue <= 0.01, decoys excluded, ambiguous
    # ("C|T") rows count as not-contaminant. Intensity share = QuantProject DEF-QC-9 v2 (intensity is theirs):
    # per file, C over C + T groups' Intensity_<file> in AllQuantifiedProteinGroups.tsv (apex, DEF-PEP-INT v1).
    if (search_dirs and params["database"].get("include_contaminants", True)
            and (search_dirs[-1] / "AllPSMs.psmtsv").exists()):
        sd = search_dirs[-1]
        with (sd / "AllPSMs.psmtsv").open(encoding="utf-8") as fh:
            psm = [r for r in csv.DictReader(fh, delimiter="\t") if float(r.get("QValue") or 1) <= 0.01]
        tgt = [r for r in psm if not r["Decoy/Contaminant/Target"].startswith("D")]
        c_psm = sum(1 for r in tgt if r["Decoy/Contaminant/Target"] == "C")
        pg_file = sd / "AllQuantifiedProteinGroups.tsv"
        per_file, top = {}, {}
        if pg_file.exists():
            with pg_file.open(encoding="utf-8") as fh:
                pgs = list(csv.DictReader(fh, delimiter="\t"))
            for col in [k for k in (pgs[0] if pgs else {}) if k.startswith("Intensity_")]:
                tot = sum(float(r[col] or 0) for r in pgs if r["Protein Decoy/Contaminant/Target"] in ("T", "C"))
                con = sum(float(r[col] or 0) for r in pgs if r["Protein Decoy/Contaminant/Target"] == "C")
                per_file[col[len("Intensity_"):]] = round(con / tot, 4) if tot else None
            for r in pgs:
                if r["Protein Decoy/Contaminant/Target"] == "C":
                    top[f'{r["Protein Full Name"]} ({r["Organism"]})'] = sum(
                        float(r[k] or 0) for k in r if k.startswith("Intensity_"))
        # Per-file SPREAD, not just the total (S17's follow-up): on PXD036557 the dataset-level share is
        # 7.0% while the per-file values run 2.6% to 18.9%, and the high files are one cell line. A single
        # dataset-level number would have hidden that entirely.
        vals = sorted(v for v in per_file.values() if v is not None)
        worst = vals[-1] if vals else 0
        med = vals[len(vals) // 2] if vals else 0
        blocks["contamination"] = {
            "psm_share": round(c_psm / len(tgt), 4) if tgt else None, "psm_share_definition": "aging DEF-CONTAM-PSM v1",
            "contaminant_psms": c_psm, "target_plus_contaminant_psms": len(tgt),
            "intensity_share_per_file": per_file, "intensity_share_definition": "QuantProject DEF-QC-9 v2",
            "intensity_share_median": med, "intensity_share_min": vals[0] if vals else None,
            "intensity_share_max": worst,
            "top": [k for k, _ in sorted(top.items(), key=lambda kv: -kv[1])[:5]]}
        if worst > p.get("flag_max_contaminant_intensity_share", 0.05):
            flags.append(f"high_contamination: {worst:.2%} of protein intensity in the worst file, "
                         f"{med:.2%} median across {len(per_file)} files (DEF-QC-9 v2)")

    # Known gaps stated on every run, so no result is mistaken for a designed or deposit-ready one.
    design = [f.parent / "ExperimentalDesign.tsv" for f in spectra_files[:1]]
    if not any(d.exists() for d in design):
        flags.append("no_design_file: FlashLFQ treated each file as its own biorep under one blank condition; "
                     "no normalization; not usable for condition comparisons (owner: QuantProject projection)")
    if not list(mm.glob("**/*.sdrf.tsv")):
        flags.append("no_output_sdrf: no reanalysis SDRF written (WriteSdrf is MetaMorpheus #2816, unreleased)")

    if p["match_between_runs"] and len(spectra_files) < 2:
        notes.append("MatchBetweenRuns is on but only one spectra file was searched: MBR has nothing to transfer.")
    return blocks, flags, notes


def main(params_path: str, spectra: str, out_dir: str) -> None:
    params = json.loads(Path(params_path).read_text(encoding="utf-8"))
    # Always the prepared, uncompressed copy (db_prepare.py): a .gz makes MM write temp.xml beside it.
    p, db = params["search"], params["database"]["prepared"]
    if not Path(db).exists():
        sys.exit(f"prepared database {db} missing: run db_prepare.py first")
    out = Path(out_dir)
    if (out / "mm").exists():
        sys.exit(f"refusing to reuse {out/'mm'}: MetaMorpheus needs a fresh output directory")
    out.mkdir(parents=True, exist_ok=True)
    cmd = Path(p["metamorpheus_cmd"])
    # CMD.dll is framework-dependent: `dotnet CMD.dll` is the same program as CMD.exe, on any OS.
    launch = [p.get("dotnet", "dotnet"), str(cmd)] if cmd.suffix.lower() == ".dll" else [str(cmd)]
    prov = Provenance("search_metamorpheus", params_path, "search")
    sp = Path(spectra)
    prov.upstream(out.parent / "02b_qc" / "provenance.json",                         # qc
                  (sp if sp.is_dir() else sp.parent).parent / "provenance.json",       # fetch
                  Path(db).parent / "provenance.json")                                 # db_prepare

    # 1. default TOMLs generated here, then only params-named settings are changed
    toml_dir = out / "tasks"; toml_dir.mkdir(exist_ok=True)
    gen = [*launch, "-g", "-o", str(toml_dir)]
    prov.command(gen)
    banner = subprocess.run(gen, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=True).stdout

    # `--version` prints the help text and exits 1 (1.1.9 and 1.1.10), so the release comes from the
    # -g banner ("Welcome to MetaMorpheus\n1.1.10") and the commit from the help's "CMD 1.0.0+<sha>".
    lines = [l.strip() for l in banner.splitlines() if l.strip()]
    release = lines[1] if len(lines) > 1 and re.fullmatch(r"\d+(\.\d+)+", lines[1]) else "unknown"
    helptext = subprocess.run([*launch, "--help"], capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
    commit = (re.search(r"CMD \S+\+([0-9a-f]{40})", helptext) or [None, "unknown"])[1]
    prov.tool("MetaMorpheus", release=release, commit=commit, expected_release=p["metamorpheus_version"],
              cmd=" ".join(launch), cmd_dll_sha256=sha256(cmd.with_suffix(".dll")))
    if release != p["metamorpheus_version"]:
        sys.exit(f"MetaMorpheus is {release}, params expect {p['metamorpheus_version']}")
    # Spectral library (user, 2026-09-21): the first search of an organism WRITES one, every search
    # after that UPDATES it. Resolved before the TOMLs are generated because it decides two of their
    # values, and before the run because it adds a database to `-d`.
    lib_plan = spectral_library.plan(params)
    for w in spectral_library.warnings(lib_plan, p):
        prov.note(w)

    tomls = []
    tolerance_overrides = {}
    for i, task in enumerate(p["tasks"], 1):
        src = toml_dir / TASK_FILE[task]
        text = src.read_text(encoding="utf-8")
        text = re.sub(r"^MaxThreadsToUsePerFile = \d+", f"MaxThreadsToUsePerFile = {p['max_threads']}", text, flags=re.M)
        if task == "Search":
            mbr = "true" if p["match_between_runs"] else "false"
            text = re.sub(r"^MatchBetweenRuns = \w+", f"MatchBetweenRuns = {mbr}", text, flags=re.M)
            # `Classic` scores every candidate peptide against every spectrum. That is fine for a
            # high-resolution fragment tolerance and intractable for a wide one: on PXD060431's
            # ion-trap spectra (~1,000 peaks per MS2) at ±0.5 Da it searched 22 files in 192 s and
            # then spent hours on the 23rd, at 52 sustained cores, three times over (S38). `Modern`
            # indexes fragments instead, which is the mode built for exactly that case.
            st = p.get("search_type")
            if st:
                if st not in SEARCH_TYPES:
                    sys.exit(f"search.search_type must be one of {sorted(SEARCH_TYPES)}, not {st!r}")
                text, n = re.subn(r'^SearchType = ".*"$', f'SearchType = "{st}"', text, flags=re.M)
                if n != 1:
                    sys.exit(f"search_type: expected exactly one SearchType line in {src.name}, replaced {n}")
                prov.rec["search_type"] = st
            # The two library booleans, on the SEARCH task only (user: "this will apply only to the
            # search task"). Calibration and GPTMD have no such settings and must not acquire any.
            if lib_plan is not None:
                for field, value in lib_plan.toml_values.items():
                    text, n = re.subn(rf"^{field} = \w+$", f"{field} = {str(value).lower()}", text, flags=re.M)
                    if n != 1:
                        sys.exit(f"spectral_library: expected exactly one {field} line in {src.name}, "
                                 f"replaced {n} - the pinned MetaMorpheus does not have the setting "
                                 f"this code was written against")
        # Optional mass-tolerance overrides, applied to EVERY task. MetaMorpheus's defaults assume
        # high-resolution fragments; on a low-resolution MS2 dataset they silently identify only the
        # small subset that happens to fall inside a high-res window, and calibration fails outright
        # for the same reason (S38). The value is written exactly as MetaMorpheus writes one, e.g.
        # "±0.3500 Absolute" or "±20.0000 PPM". The `= ` in the pattern is what keeps this off
        # `ProductMassTolerance_LowRes`, which must not be touched.
        for key, field in (("product_mass_tolerance", "ProductMassTolerance"),
                           ("precursor_mass_tolerance", "PrecursorMassTolerance")):
            value = p.get(key)
            if value:
                text, n = re.subn(rf'^{field} = ".*"$', f'{field} = "{value}"', text, flags=re.M)
                if n != 1:
                    sys.exit(f"{key}: expected exactly one {field} line in {src.name}, replaced {n}")
                tolerance_overrides[f"{task}.{field}"] = value
        dst = toml_dir / f"{i}_{TASK_FILE[task]}"
        dst.write_text(text, encoding="utf-8"); tomls.append(dst)
    prov.inputs(*tomls)
    if lib_plan is not None:
        prov.rec["spectral_library"] = {
            "organism": lib_plan.organism,
            "mode": lib_plan.mode,
            "library_in": lib_plan.library_in,
            "parent_version": lib_plan.parent_version,
            "registry": str(spectral_library.registry_path(lib_plan.cfg)),
        }
    if tolerance_overrides:
        # A deviation from the pinned engine's defaults is a fact about the result, not a
        # convenience, so it is recorded and flagged rather than left in the params file.
        prov.rec["tolerance_overrides"] = tolerance_overrides


    # 2. one invocation for the whole chain
    # Do NOT create this dir: MetaMorpheus 1.1.9 seeds it (Data/, Mods/ ...) only when it does not
    # exist; an empty pre-created dir crashes with DirectoryNotFoundException on Data/Crosslinkers.tsv.
    # One settings dir per release: it holds that release's Data/ and Mods/, which must not mix.
    settings = Path(params["work_root"]) / "mm_settings" / release
    settings.parent.mkdir(parents=True, exist_ok=True)
    spectra_files = sorted(Path(spectra).glob("*.raw")) if Path(spectra).is_dir() else [Path(spectra)]
    # Named files can be excluded from a search. This is for a file that cannot be searched rather
    # than one you would rather not search: PXD060431's HumanHFreducedEF_5 sat in ClassicSearchEngine
    # for over 90 minutes while its 25 predecessors took 7 seconds each, on a file whose bytes hash to
    # the download record and whose peak data is indistinguishable from its neighbours' (S38). An
    # exclusion is a hole in the dataset, so it is named, recorded in provenance and never inferred.
    excluded = list(params["search"].get("exclude_files") or [])
    if excluded:
        missing = sorted(set(excluded) - {f.name for f in spectra_files})
        if missing:
            sys.exit(f"search.exclude_files names {missing}, which are not in {spectra}")
        spectra_files = [f for f in spectra_files if f.name not in excluded]
        if not spectra_files:
            sys.exit("search.exclude_files excluded every file")
        prov.rec["excluded_files"] = {"files": sorted(excluded),
                                      "reason": params["search"].get("exclude_files_why", "")}
    qc = out.parent / "02b_qc" / "qc_report.json"
    if not qc.exists():
        sys.exit(f"no QC report at {qc}: run qc_spectra.py first (v1 requires high-res Orbitrap HCD MS2)")
    report = json.loads(qc.read_text(encoding="utf-8"))
    failed = {n: r.get("fail_reasons") or ["unspecified"] for n, r in report.items() if not r["pass"]}
    exception_flag = None
    if failed:
        # A dataset may carry an acquisition exception: an explicit, user-granted waiver naming the
        # exact QC conditions it forgives. It is scoped on purpose - a file failing anything the
        # exception does not name still fails, so a waiver cannot quietly become a blanket override.
        exc = params["qc"].get("acquisition_exception") or {}
        waived = set(exc.get("waives") or []) - NEVER_WAIVABLE
        refused = set(exc.get("waives") or []) & NEVER_WAIVABLE
        if refused:
            # The docs said these "should never be waived" and nothing enforced it, so a waiver
            # naming one was honoured. A waiver is a safety mechanism; an unenforced rule in it is
            # worse than no rule, because the operator believes the guard exists.
            sys.exit(f"acquisition_exception waives {sorted(refused)}, which cannot be waived: "
                     f"a file with almost no MS2, or one we cannot read at all, is not an "
                     f"acquisition choice. Remove it from `waives`.")
        unwaived = {n: [r for r in rs if r not in waived] for n, rs in failed.items()}
        unwaived = {n: rs for n, rs in unwaived.items() if rs}
        if unwaived or not waived:
            sys.exit(f"QC failed for {sorted(unwaived or failed)}: "
                     f"not high-res Orbitrap HCD MS2, or too few MS2 scans")
        reasons = sorted({r for rs in failed.values() for r in rs})
        exception_flag = (
            f"acquisition_exception: {len(failed)} file(s) failed QC on {reasons} and were searched "
            f"under a user-granted waiver ({exc.get('granted_by', '?')}, {exc.get('granted_date', '?')}). "
            f"Results are restricted to {exc.get('restricts_to') or ['?']} and must NOT be used for "
            f"{exc.get('bars') or ['?']}")
        prov.rec["acquisition_exception"] = {**exc, "files": sorted(failed), "fail_reasons": reasons}
    prov.inputs(qc)
    # Contaminants (S15): MetaMorpheus ships Contaminants/MetaMorpheusContaminants.xml but CMD uses only
    # the databases passed with -d. Without it, keratins, trypsin, serum albumin etc. have nowhere to match.
    dbs = [db]
    if params["database"].get("include_contaminants", True):
        # `contaminants` overrides the shipped file. It exists so that a Search-only re-run can use
        # the GPTMD task's own augmented contaminant database, which is the one the full chain would
        # have searched — pointing a re-run at the shipped file instead would quietly drop every
        # contaminant modification GPTMD discovered and make the contamination metrics incomparable.
        override = params["database"].get("contaminants")
        contam = Path(override) if override else cmd.parent / "Contaminants" / "MetaMorpheusContaminants.xml"
        if not contam.exists():
            sys.exit(f"contaminant database missing at {contam}")
        dbs.append(str(contam))
        if override:
            prov.note(f"contaminant database overridden: {contam}")
    else:
        prov.note("contaminant database NOT included (params.database.include_contaminants = false)")
    if lib_plan is not None and lib_plan.library_in:
        # A library is supplied as another `-d`: DbForTask decides by extension (.msp/.msl) alone.
        # It rides through the whole chain - GPTMD forwards it in NewDatabases - so one -d is enough
        # for Calibration -> GPTMD -> Search as well as for a Search-only re-run.
        dbs.append(lib_plan.library_in)
        prov.note(f"spectral library in use: {lib_plan.library_in} "
                  f"({lib_plan.organism}, parent version {lib_plan.parent_version})")
    prov.inputs(*spectra_files, *dbs)
    run = [*launch, "-t", *map(str, tomls), "-s", *map(str, spectra_files), "-d", *dbs,
           "-o", str(out / "mm"), "--mmsettings", str(settings), "-v", "normal"]
    if p["accept_thermo_licence"]:
        run.append("--acceptThermoLicence")
        prov.note("Thermo RawFileReader licence accepted via params.search.accept_thermo_licence (operator's recorded choice).")
    prov.command(run)
    # Each log line is stamped with elapsed seconds (same clock as the resource monitor), so every
    # MetaMorpheus task gets its own wall time, CPU, average cores and peak memory (D11).
    t0 = prov.monitor.t0
    marks = []                                   # (task, "start"|"end", t)
    # `for line in proc.stdout` blocks until the pipe reaches EOF, which for a subprocess means until
    # it EXITS. Reading the log that way and then calling `proc.wait(timeout=...)` put the timeout
    # after the only thing that could ever need timing out, so `search.timeout_s` could not fire and a
    # hung search blocked forever with no provenance written. That is what every PXD060431 stall did
    # (S38), and nobody noticed the guard rail itself was inoperative. Read on a thread so the
    # deadline is real.
    timed_out = False
    with (out / "metamorpheus.log").open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(run, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)

        def pump():
            for line in proc.stdout:
                t = round(time.monotonic() - t0, 1)
                log.write(f"{t}\t{line}")
                m = re.match(r"\s*(Starting|Finished) task: (\S+)", line)
                if m:
                    marks.append((m.group(2), "start" if m.group(1) == "Starting" else "end", t))

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            rc = proc.wait(timeout=p["timeout_s"])
        except subprocess.TimeoutExpired:
            timed_out = True
            # Kill the tree, not just the launcher: `dotnet CMD.dll` makes CMD a CHILD, so killing
            # the parent would leave a 32-thread search holding the machine while the caller moves on.
            kill_tree(proc)
            rc = proc.wait(timeout=60)
        reader.join(timeout=10)
    prov.rec["exit_code"] = rc
    if timed_out:
        prov.rec["timed_out_after_s"] = p["timeout_s"]
        prov.note(f"KILLED: the search exceeded search.timeout_s ({p['timeout_s']} s) and the process "
                  f"tree was terminated. Partial outputs in {out / 'mm'} are NOT a completed search.")
    starts = {k: t for k, s, t in marks if s == "start"}
    prov.rec["per_task_resources"] = {k: prov.monitor.window(starts[k], t) for k, s, t in marks
                                      if s == "end" and k in starts and getattr(prov.monitor, "series", None)}

    # 3. success checks
    mm = out / "mm"
    search_dirs = sorted(mm.glob("Task*SearchTask"))
    key = {n: next(iter(sorted(search_dirs[-1].glob(n))), None) if search_dirs else None
           for n in ("AllPSMs.psmtsv", "AllPeptides.psmtsv", "AllQuantifiedProteinGroups.tsv", "AllQuantifiedPeptides.tsv")}
    gptmd_db = next(iter(sorted(mm.glob("Task*GptmdTask/*GPTMD.xml"))), None)
    ok = rc == 0 and all(key.values())
    prov.rec["success"] = ok
    if rc == 0 and not key["AllQuantifiedProteinGroups.tsv"]:
        prov.note("exit 0 but no AllQuantifiedProteinGroups.tsv: FlashLFQ failed silently (pyMM 003 Q4)")
    prov.outputs(*[v for v in key.values() if v], *([gptmd_db] if gptmd_db else []), out / "metamorpheus.log",
                 *sorted(mm.glob("allResults.txt")))
    for ts in sorted(mm.glob("Task Settings/*.toml")):
        prov.outputs(ts)
    # Derived blocks and automatic suspicion flags (user: follow up on anything suspicious). They land in
    # provenance.json and feed results/SUSPICIOUS.md; they never fail the stage by themselves. Shared with
    # reprovenance.py so an old run can be re-derived under today's definitions without re-searching.
    # Register the library only when the search actually succeeded: a failed run's partial library
    # must not become the parent of the next one.
    if lib_plan is not None:
        if ok and search_dirs:
            try:
                rec = spectral_library.register(
                    lib_plan, search_dirs[-1],
                    run_label=f"{params.get('run_date', '')}/{params.get('fetch', {}).get('accession', '')}".strip("/"),
                    accession=params.get("fetch", {}).get("accession") or "",
                    metamorpheus=release)
                prov.rec["spectral_library"]["written"] = rec
                prov.outputs(Path(lib_plan.cfg["root"]) / rec["path"])
                prov.note(f"spectral library {lib_plan.organism} v{rec['version']:03d}: "
                          f"{rec['n_spectra']:,} spectra, {rec['path']}")
            except RuntimeError as e:
                # Never fatal: the search stands, the library did not advance, and the ledger says so.
                prov.rec["spectral_library"]["error"] = str(e)
                prov.note(str(e))
        else:
            prov.note("spectral library NOT registered: the search did not succeed, so this run's "
                      "library cannot become the parent of the next one")

    blocks, flags, notes = derive_metrics(out, params, spectra_files, qc)
    if lib_plan is not None and prov.rec.get("spectral_library", {}).get("error"):
        flags.insert(0, prov.rec["spectral_library"]["error"])
    if exception_flag:
        flags.insert(0, exception_flag)
    prov.rec.update(blocks)
    for n in notes:
        prov.note(n)
    prov.rec["flags"] = flags
    prov.rec["expected_cores"] = p["max_threads"]

    prov.write(out)
    print(json.dumps({"exit_code": rc, "success": ok, **{k: str(v) for k, v in key.items()}}, indent=2))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main(*sys.argv[1:4])
