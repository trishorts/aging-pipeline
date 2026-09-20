#!/usr/bin/env python3
"""A stand-in for the MetaMorpheus command-line tool, for OFFLINE tests of search_mm.py.

It implements only the calls search_mm.py makes, with the output shapes MetaMorpheus 1.1.11 writes:
  CMD -g -o <dir>      -> the banner "Welcome to MetaMorpheus / <release>" + the three default task TOMLs
  CMD --help           -> a help text carrying "CMD 1.0.0+<40-hex commit>"
  CMD -t ... -o <dir>  -> Task1CalibrationTask/, Task2GptmdTask/, Task3SearchTask/ with small result tables

It tests the pipeline's orchestration (arguments, success checks, provenance, flags). It says nothing about
MetaMorpheus's science. Environment switches for the failure paths:
  FAKE_MM_RELEASE=<x>            report release <x> instead of 1.1.11 (the version guard)
  FAKE_MM_NO_PROTEIN_GROUPS=1    omit AllQuantifiedProteinGroups.tsv (FlashLFQ failing silently)
  FAKE_MM_EXIT=<n>               exit with code <n>
"""
import os, sys
from pathlib import Path

RELEASE = os.environ.get("FAKE_MM_RELEASE", "1.1.11")
# The real `-g` output carries both product-tolerance keys in [CommonParameters]. The fake needs them
# so the override path is exercised, and so a test can prove `ProductMassTolerance_LowRes` — which
# shares a prefix with the key being replaced — is left alone.
TOLERANCES = ("ProductMassTolerance = \"±20.0000 PPM\"\n"
              "ProductMassTolerance_LowRes = \"±0.3500 Absolute\"\n"
              "PrecursorMassTolerance = \"±5.0000 PPM\"\n")
TOML = {
    "CalibrationTask.toml": "TaskType = \"Calibrate\"\n[CommonParameters]\nMaxThreadsToUsePerFile = 1\n" + TOLERANCES,
    "GptmdTask.toml": "TaskType = \"Gptmd\"\n[CommonParameters]\nMaxThreadsToUsePerFile = 1\n" + TOLERANCES,
    "SearchTask.toml": "TaskType = \"Search\"\n[SearchParameters]\nMatchBetweenRuns = false\n"
                       "SearchType = \"Classic\"\n"
                       "[CommonParameters]\nMaxThreadsToUsePerFile = 1\n" + TOLERANCES,
}


def arg(flag):
    a = sys.argv
    return a[a.index(flag) + 1] if flag in a else None


def values(flag):
    """Every value after `flag` up to the next option."""
    a, out = sys.argv, []
    if flag in a:
        for v in a[a.index(flag) + 1:]:
            if v.startswith("-"):
                break
            out.append(v)
    return out


def main():
    if "-g" in sys.argv:
        out = Path(arg("-o")); out.mkdir(parents=True, exist_ok=True)
        for name, text in TOML.items():
            (out / name).write_text(text, encoding="utf-8")
        print(f"Welcome to MetaMorpheus\n{RELEASE}\n")
        return 0
    if "--help" in sys.argv:
        print("CMD 1.0.0+" + "0123456789abcdef" * 2 + "01234567\nUsage: CMD [options]")
        return 0

    out = Path(arg("-o")); out.mkdir(parents=True)
    spectra = [Path(s).stem for s in values("-s")]
    for i, task in enumerate(["CalibrationTask", "GptmdTask", "SearchTask"], 1):
        print(f"Starting task: Task{i}{task}", flush=True)
        (out / f"Task{i}{task}").mkdir()
        print(f"Finished task: Task{i}{task}", flush=True)
    (out / "Task2GptmdTask" / "db-GPTMD.xml").write_text("<uniprot/>", encoding="utf-8")
    sd = out / "Task3SearchTask"
    # 8 target, 1 contaminant, 1 decoy PSM at q <= 0.01, plus one above the cut-off.
    rows = ["QValue\tDecoy/Contaminant/Target"] + ["0.001\tT"] * 8 + ["0.002\tC", "0.003\tD", "0.5\tT"]
    (sd / "AllPSMs.psmtsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (sd / "AllPeptides.psmtsv").write_text("QValue\n0.001\n", encoding="utf-8")
    (sd / "AllQuantifiedPeptides.tsv").write_text("Sequence\nPEPTIDE\n", encoding="utf-8")
    cols = "\t".join(f"Intensity_{s}" for s in spectra)
    pg = [f"Protein Full Name\tOrganism\tProtein Decoy/Contaminant/Target\t{cols}",
          "Titin\tHomo sapiens\tT\t" + "\t".join("90" for _ in spectra),
          "Serum albumin\tBos taurus\tC\t" + "\t".join("10" for _ in spectra)]
    if not os.environ.get("FAKE_MM_NO_PROTEIN_GROUPS"):          # simulates FlashLFQ failing with exit 0
        (sd / "AllQuantifiedProteinGroups.tsv").write_text("\n".join(pg) + "\n", encoding="utf-8")
    peaks = ["Peak Detection Type\tRandom RT\tPIP Q-Value\tDecoy Peptide",
             "MSMS\t\t\tFalse", "MSMS\t\t\tFalse", "MSMS\t\t\tFalse",
             "MBR\tFalse\t0.001\tFalse",      # kept
             "MBR\tTrue\t0.001\tFalse",       # the random-RT decoy won: not kept
             "MBR\tFalse\t0.2\tFalse"]        # fails the PIP q-value: not kept
    (sd / "AllQuantifiedPeaks.tsv").write_text("\n".join(peaks) + "\n", encoding="utf-8")
    # Both counts, as 1.1.11 prints them: the target-only summary line first, then the FDR engine's (S21).
    (sd / "results.txt").write_text("All target PSMs with q-value <= 0.01: 8\n\nPSMs within 1% FDR: 9\n", encoding="utf-8")
    (out / "allResults.txt").write_text("fake run\n", encoding="utf-8")
    (out / "Task Settings").mkdir()
    (out / "Task Settings" / "Task3SearchTaskconfig.toml").write_text("fake\n", encoding="utf-8")
    return int(os.environ.get("FAKE_MM_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main())
