"""Stage 0 - prepare the search database once, in the pipeline's own work area.

A gzipped database makes MetaMorpheus write a fixed temp.fasta/temp.xml BESIDE the input (mzLib
#1323, found by pyMetaMorpheus). That writes into whatever folder holds the database (here, another
project's), and concurrent searches collide on it. So the pipeline decompresses once into
<work_root>/db/, records provenance, and every search reads that copy.

Later this stage also fetches the database from UniProt (REQ-PYMZ-1) and appends the targeted
isoforms (aging D8).

usage: db_prepare.py <params.json> <out_dir>   -> prints the prepared path
"""
import gzip, json, shutil, sys
from pathlib import Path

from provenance import Provenance


def main(params_path: str, out_dir: str) -> None:
    src = Path(json.loads(Path(params_path).read_text(encoding="utf-8"))["database"]["uniprot_xml"])
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    dst = out / (src.name[:-3] if src.suffix == ".gz" else src.name)
    prov = Provenance("db_prepare", params_path, "database")
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
        prov.note("already prepared; reused")
    prov.outputs(dst); prov.write(out)
    print(dst)


if __name__ == "__main__":
    main(*sys.argv[1:3])
