"""Which contaminant database a search uses, and building a reduced copy of it when entries are excluded.

MetaMorpheus ships `Contaminants/MetaMorpheusContaminants.xml`. Since aging D54 (S58) the pipeline can drop listed
entries from it before searching (`database.contaminant_exclude`, a TSV with an `accession` column). The shipped panel
carries a human spike-in protein standard (UPS1/UPS2-like: PRDX1, SOD1, CAT, CKM, MAPT and more) alongside the
reagents. In mouse and rat searches those human entries captured peptides of the native proteins and labelled them
contaminant. The reduced copy is content-addressed, so the same panel plus the same list always gives the same
file, and the search records both inputs' sha256.

Both the search command and the contamination metrics call `resolve`, so they always agree on the panel.
"""
import hashlib, re
from pathlib import Path

_ENTRY = re.compile(r"<entry\b.*?</entry>\s*", re.S)
_ACC = re.compile(r"<accession>([^<]+)</accession>")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def excluded_accessions(list_path: Path) -> set:
    rows = [l for l in list_path.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    head = rows[0].split("\t")
    i = head.index("accession")
    return {r.split("\t")[i].strip() for r in rows[1:]}


def source_panel(params: dict) -> Path:
    """The panel before any exclusion: the `database.contaminants` override, else the one MetaMorpheus ships."""
    override = params["database"].get("contaminants")
    return Path(override) if override else \
        Path(params["search"]["metamorpheus_cmd"]).parent / "Contaminants" / "MetaMorpheusContaminants.xml"


def resolve(params: dict, pipeline_dir: Path, out_dir: Path) -> tuple:
    """Return (panel to search, provenance dict or None). Builds the reduced copy under `out_dir` if needed.

    `database.contaminant_exclude` is resolved against the pipeline directory when relative. Every listed accession
    must be in the panel: a list that names an entry the panel lacks is a list written for a different panel.
    """
    src = source_panel(params)
    rel = params["database"].get("contaminant_exclude")
    if not rel:
        return src, None
    lst = Path(rel) if Path(rel).is_absolute() else pipeline_dir / rel
    drop = excluded_accessions(lst)
    text = src.read_text(encoding="utf-8")
    present = set(_ACC.findall(text))
    missing = sorted(drop - present)
    if missing:
        raise SystemExit(f"contaminant_exclude lists {len(missing)} accession(s) not in {src.name}: {missing[:5]}")
    src_sha, lst_sha = _sha(src), _sha(lst)
    out = out_dir / f"{src.stem}.minus-{lst.stem}.{hashlib.sha256((src_sha + lst_sha).encode()).hexdigest()[:12]}.xml"
    removed = []

    def keep(m):
        acc = _ACC.search(m.group(0))
        if acc and acc.group(1) in drop:
            removed.append(acc.group(1))
            return ""
        return m.group(0)

    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        reduced = _ENTRY.sub(keep, text)
        tmp = out.with_suffix(".partial")
        tmp.write_text(reduced, encoding="utf-8")
        tmp.replace(out)
    else:
        removed = sorted(drop)
    return out, {"source": str(src), "source_sha256": src_sha, "exclude_list": str(lst), "exclude_list_sha256": lst_sha,
                 "excluded": sorted(set(removed)), "searched": str(out), "searched_sha256": _sha(out),
                 "decision": "aging D54 (S58): a human spike-in standard is not a contaminant"}
