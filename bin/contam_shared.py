"""`aging:DEF-CONTAM-INT-SHARED v1`: the contaminant intensity share that ALSO counts proteins that sit
in both the contaminant panel and the searched proteome (D53, S56).

Why it exists. MetaMorpheus's `TCAmbiguity = RemoveContaminant` (the default, and ours) drops the
contaminant copy of any accession that is also in the target database, before the search
(`DatabaseLoadingEngine`, MetaMorpheus 1.1.11). Those proteins are then always `T`. In the human
proteome that is 116 of the panel's 264 entries, human serum albumin (P02768) and the keratins among
them; in mouse 26; in rat 0 (dataRepo 062 §3). So `QuantProject:DEF-QC-9`, which reads MetaMorpheus's
`C`, cannot see human serum or skin contamination in a human sample.

What it is: an UPPER BOUND, reported BESIDE DEF-QC-9, which is the LOWER bound. Neither is "the"
contamination (user, 2026-09-24): whether keratin or albumin is contamination depends on the sample
(in skin keratins are biology, in serum albumin is), and even there some of it may still be
contamination from handling. The true share lies between the two numbers, and the gap between them is
how much of the answer rests on the shared proteins. Nothing flags on this number; DEF-QC-9 keeps the
flag.

Definition, per file: over protein groups at `Protein QValue` <= 0.01 (as DEF-QC-9 v3.5), numerator =
apex intensity of `C` groups PLUS `T` groups whose every member accession is a shared accession;
denominator = apex intensity of all `T` and `C` groups. A group that mixes shared and non-shared
members is NOT counted as contaminant (conservative). The shared list is computed from the two
databases the search actually used, and its sha256 is recorded with the number.
"""
import hashlib
import re
from pathlib import Path

DEFINITION = "aging DEF-CONTAM-INT-SHARED v1"
_ACC = re.compile(r"<accession>([^<]+)</accession>")


def primary_accessions(xml) -> set:
    """The first <accession> of every <entry> in a UniProt XML (MetaMorpheus's protein accession)."""
    out, want = set(), False
    with Path(xml).open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "<entry" in line:
                want = True
            if want:
                m = _ACC.search(line)
                if m:
                    out.add(m.group(1))
                    want = False
    return out


def shared_accessions(contaminant_xml, proteome_xmls) -> tuple:
    """(sorted shared accessions, sha256 of the newline-joined list)."""
    contam = primary_accessions(contaminant_xml)
    target = set()
    for x in proteome_xmls:
        target |= primary_accessions(x)
    shared = sorted(contam & target)
    return shared, hashlib.sha256("\n".join(shared).encode("utf-8")).hexdigest()


def share_per_file(rows, shared) -> dict:
    """Per `Intensity_<file>` column: (C + all-shared T) / (T + C). `rows` are protein groups already
    filtered to 1% (DEF-QC-9 v3.5's population)."""
    shared = set(shared)
    cols = [k for k in (rows[0] if rows else {}) if k.startswith("Intensity_")]
    out = {}
    for col in cols:
        tot = con = 0.0
        for r in rows:
            td = r["Protein Decoy/Contaminant/Target"]
            if td not in ("T", "C"):
                continue
            v = float(r[col] or 0)
            tot += v
            accs = [a for a in r["Protein Accession"].split("|") if a]
            if td == "C" or (accs and all(a in shared for a in accs)):
                con += v
        out[col[len("Intensity_"):]] = round(con / tot, 4) if tot else None
    return out
