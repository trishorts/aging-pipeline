"""Provenance record written next to EVERY stage output (aging D9).

The user's requirement: every output carries a log of all the tools, versions and settings used, so
that it can be reproduced. SDRF covers only part of that. This module writes `provenance.json` into a
stage's output directory with:
  * the stage name, UTC start/end, host, OS and Python version;
  * the tools and their versions (pymzlib bridge + mzLib commit, MetaMorpheus version + SHA-256 of its
    binary, the pipeline's own git commit);
  * the exact params section used, plus the full params file's SHA-256;
  * the command lines executed;
  * every input and output file, with size and SHA-256.

Stage 7 and operators read this; it is part of the output contract, not a debug log.
"""
import datetime, hashlib, json, platform, subprocess, sys, time
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def file_entry(path, root=None, known=None) -> dict:
    """One input/output record.

    * Portable path: stored relative to `root` (the params `work_root`) when the file lies under it,
      with `"root": "work_root"`, so the record reads the same on an operator's cluster. Files outside
      the root (tools, params) keep their absolute path.
    * Hash reuse: when an upstream stage already hashed this file (same resolved path and size, or
      same name and size for a hard link elsewhere), its hash is reused instead of re-reading a
      multi-GB .raw, and `sha256_from` names the stage it came from.
    """
    p = Path(path).resolve()
    size = p.stat().st_size
    entry = {"path": str(p), "size_bytes": size}
    if root is not None:
        try:
            entry = {"path": p.relative_to(Path(root).resolve()).as_posix(), "root": "work_root", "size_bytes": size}
        except ValueError:
            pass
    hit = None
    if known:
        hit = known.get(("path", str(p), size))
        if not hit and size >= 100_000_000:      # name+size only for big files (a hard link of a .raw)
            hit = known.get(("name", p.name, size))
    if hit:
        entry["sha256"], entry["sha256_from"] = hit
    else:
        entry["sha256"] = sha256(p)
    return entry


def pipeline_commit() -> str:
    here = Path(__file__).resolve().parent
    try:
        sha = subprocess.run(["git", "-C", str(here), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(here), "status", "--porcelain", "--", "."], capture_output=True, text=True).stdout.strip()
        return sha + ("+dirty" if dirty else "")
    except OSError:
        return "unknown"


class ResourceMonitor:
    """Compute and memory for this stage (aging D11): collaborators will ask as we scale up.

    Samples this process and ALL its descendants (MetaMorpheus CMD, mzlib-bridge) once per `interval`
    seconds and keeps peaks. CPU time is summed per process from the last sample seen, so a child that
    exits between samples loses at most `interval` of CPU. That is fine for stages that run minutes to
    hours. In production, Nextflow's -with-trace records the same per process, and the two should agree.
    """

    def __init__(self, interval: float = 1.0):
        import threading
        try:
            import psutil
        except ImportError:  # the record still gets wall time
            self.psutil = None
            self.t0 = time.monotonic()
            return
        self.psutil, self.interval = psutil, interval
        self.me = psutil.Process()
        self.t0 = time.monotonic()
        self.cpu = {}                      # pid -> (user, system) last seen
        self.io0 = self._io()
        self.peak_rss = self.peak_threads = self.peak_procs = 0
        self.samples = 0
        self.series = []                   # (t_s, cpu_s_total, rss_bytes, threads) per sample
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _io(self):
        try:
            c = self.psutil.disk_io_counters()
            return (c.read_bytes, c.write_bytes)
        except Exception:
            return None

    def _run(self):
        while not self.stop.is_set():
            self.sample()
            self.stop.wait(self.interval)

    def sample(self):
        ps = self.psutil
        try:
            procs = [self.me] + self.me.children(recursive=True)
        except ps.Error:
            return
        rss = threads = 0
        for pr in procs:
            try:
                with pr.oneshot():
                    rss += pr.memory_info().rss
                    threads += pr.num_threads()
                    t = pr.cpu_times()
                    self.cpu[pr.pid] = (t.user, t.system)
            except ps.Error:
                continue
        self.peak_rss = max(self.peak_rss, rss)
        self.peak_threads = max(self.peak_threads, threads)
        self.peak_procs = max(self.peak_procs, len(procs))
        self.samples += 1
        cpu_total = sum(u + s for u, s in self.cpu.values())
        self.series.append((round(time.monotonic() - self.t0, 1), round(cpu_total, 1), rss, threads))

    def window(self, t_start: float, t_end: float) -> dict:
        """Wall, CPU, average cores and peak memory between two elapsed-time marks (e.g. one MM task)."""
        pts = [s for s in self.series if t_start <= s[0] <= t_end]
        if len(pts) < 2:
            return {"wall_s": round(t_end - t_start, 1), "samples": len(pts)}
        wall = pts[-1][0] - pts[0][0]; cpu = pts[-1][1] - pts[0][1]
        return {"wall_s": round(t_end - t_start, 1), "cpu_s": round(cpu, 1),
                "avg_cores_used": round(cpu / wall, 2) if wall else None,
                "peak_rss_gib": round(max(p[2] for p in pts) / 2**30, 2),
                "peak_threads": max(p[3] for p in pts), "samples": len(pts)}

    def write_series(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("t_s\tcpu_s_cumulative\trss_bytes\tthreads\n")
            for row in self.series:
                fh.write("\t".join(map(str, row)) + "\n")

    def result(self) -> dict:
        wall = time.monotonic() - self.t0
        if self.psutil is None:
            return {"wall_s": round(wall, 1), "note": "psutil not installed; wall time only"}
        self.stop.set(); self.thread.join(timeout=5); self.sample()
        user = sum(u for u, _ in self.cpu.values()); system = sum(s for _, s in self.cpu.values())
        io1 = self._io()
        vm = self.psutil.virtual_memory()
        return {
            "wall_s": round(wall, 1),
            "cpu_user_s": round(user, 1), "cpu_system_s": round(system, 1),
            "avg_cores_used": round((user + system) / wall, 2) if wall else None,
            "peak_rss_bytes": self.peak_rss, "peak_rss_gib": round(self.peak_rss / 2**30, 2),
            "peak_threads": self.peak_threads, "peak_processes": self.peak_procs,
            "host_disk_read_bytes": (io1[0] - self.io0[0]) if io1 and self.io0 else None,
            "host_disk_write_bytes": (io1[1] - self.io0[1]) if io1 and self.io0 else None,
            "host": {"logical_cpus": self.psutil.cpu_count(), "physical_cpus": self.psutil.cpu_count(logical=False),
                     "ram_bytes": vm.total},
            "sampling": {"interval_s": self.interval, "samples": self.samples,
                         "scope": "this process + all descendants; disk I/O is host-wide"},
        }


class Provenance:
    def __init__(self, stage: str, params_path: str, section: str):
        self.monitor = ResourceMonitor()
        self.params_path = Path(params_path)
        params = json.loads(self.params_path.read_text(encoding="utf-8"))
        self.rec = {
            "schema": "aging-provenance/2",
            "stage": stage,
            "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "host": {"node": platform.node(), "os": platform.platform(), "python": sys.version.split()[0]},
            "pipeline": {"repo": "trishorts/aging", "commit": pipeline_commit()},
            "params_file": file_entry(self.params_path),
            "params": params.get(section, {}),
            "run_date": params.get("run_date"),
            "tools": {}, "commands": [], "upstream": [], "inputs": [], "outputs": [], "notes": [],
        }
        # Portable paths: everything under work_root is stored relative to it (improvement 2).
        self.root = params.get("work_root")
        self.rec["roots"] = {"work_root": self.root}
        self.known = {}                    # hash cache from upstream stages (improvement 3)

    def upstream(self, *prov_paths):
        """Chain this stage to the stages it consumed (improvement 1): each upstream provenance.json is
        recorded by stage name, relative path and SHA-256, so a result table can be traced back
        search -> qc -> fetch -> discover. Their output hashes are also loaded into the hash cache."""
        for pp in prov_paths:
            pp = Path(pp)
            if not pp.exists():
                self.note(f"expected upstream provenance missing: {pp}")
                continue
            up = json.loads(pp.read_text(encoding="utf-8"))
            e = file_entry(pp, self.root)
            self.rec["upstream"].append({"stage": up.get("stage"), "path": e["path"], "sha256": e["sha256"]})
            base = Path(up.get("roots", {}).get("work_root") or ".")
            for o in up.get("outputs", []) + up.get("inputs", []):
                if "sha256" not in o:
                    continue
                full = (base / o["path"]) if o.get("root") == "work_root" else Path(o["path"])
                src = up.get("stage")
                self.known[("path", str(full.resolve()), o["size_bytes"])] = (o["sha256"], src)
                self.known[("name", full.name, o["size_bytes"])] = (o["sha256"], src)

    def tool(self, name: str, **info):
        self.rec["tools"][name] = info

    def command(self, argv):
        self.rec["commands"].append([str(a) for a in argv])

    def inputs(self, *paths):
        self.rec["inputs"] += [file_entry(p, self.root, self.known) for p in paths]

    def outputs(self, *paths):
        self.rec["outputs"] += [file_entry(p, self.root) for p in paths]     # new files: always hashed

    def note(self, text: str):
        self.rec["notes"].append(text)

    def write(self, out_dir):
        self.rec["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.rec["resources"] = self.monitor.result()
        if getattr(self.monitor, "series", None):
            self.monitor.write_series(Path(out_dir) / "resources_timeseries.tsv")
            self.rec["resources"]["timeseries_file"] = "resources_timeseries.tsv"
        self.rec["resources"]["output_bytes"] = sum(o["size_bytes"] for o in self.rec["outputs"])
        used, want = self.rec["resources"].get("avg_cores_used"), self.rec.get("expected_cores")
        if used and want and used < 0.5 * want:
            self.rec.setdefault("flags", []).append(
                f"low_core_use: {used} avg cores of {want} allowed (S5)")
        path = Path(out_dir) / "provenance.json"
        path.write_text(json.dumps(self.rec, indent=2), encoding="utf-8")
        return path


def pymzlib_tool(prov: Provenance):
    import importlib.metadata, pymzlib
    prov.tool("pymzlib", package="mzlib", version=importlib.metadata.version("mzlib"), bridge=pymzlib.bridge_version())
