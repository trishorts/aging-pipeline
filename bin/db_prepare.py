"""Stage 0 - prepare the search database once, in the pipeline's own work area.

A gzipped database makes MetaMorpheus write a fixed temp.fasta/temp.xml BESIDE the input (mzLib
#1323, found by pyMetaMorpheus). That writes into whatever folder holds the database (here, another
project's), and concurrent searches collide on it. So the pipeline decompresses once into
<work_root>/db/, records provenance, and every search reads that copy.

Optional EXTRA databases (`database.extra_xml`, a list) are prepared the same way, each under its
own name. The first is the targeted aging isoform database (D8, D43): searched BESIDE the reference
proteome and the contaminants, never instead of them. Each must land at the path listed in
`database.extra_prepared`, which is `<out_dir>/<source name minus .gz>`.

Later this stage also fetches the database from UniProt (REQ-PYMZ-1).

usage: db_prepare.py <params.json> <out_dir>   -> prints each prepared path, the main one first
"""
import gzip, json, shutil, sys
from pathlib import Path

from provenance import Provenance


def prepare(src: Path, out: Path, prov) -> Path:
    """Decompress or copy one database into `out`, atomically; reuse it when already there."""
    dst = out / (src.name[:-3] if src.suffix == ".gz" else src.name)
    prov.inputs(src)
    if not dst.exists():
        tmp = dst.with_name(dst.name + ".partial")
        if src.suffix == ".gz":
            prov.command(["gunzip", str(src), "->", str(dst)])
            with gzip.open(src, "rb") as fi, tmp.open("wb") as fo:
                shutil.copyfileobj(fi, fo, 1 << 22)
        else:
            prov.command(["copy", str(src), "->", str(dst)])
            shutil.copyfile(src, tmp)
        tmp.replace(dst)                    # atomic: a crash never leaves a half file under the real name
    else:
        prov.note(f"already prepared; reused: {dst.name}")
    prov.outputs(dst)
    return dst


def main(params_path: str, out_dir: str) -> None:
    db = json.loads(Path(params_path).read_text(encoding="utf-8"))["database"]
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    prov = Provenance("db_prepare", params_path, "database")
    done = [prepare(Path(db["uniprot_xml"]), out, prov)]
    for extra in db.get("extra_xml") or []:
        done.append(prepare(Path(extra), out, prov))
    prov.write(out)
    for d in done:
        print(d)


if __name__ == "__main__":
    main(*sys.argv[1:3])
