"""
Per-ontology hierarchy metrics from the structural sidecars.

Taxonomic depth, branching factor and the depth-distribution shape all need
the full subsumption hierarchy, which lives in the per-ontology structural
sidecars (``$ONTOMETER_STORE/cache/structure/v<N>/<hh>/<file_hash>.parquet``, the
``subclass_edge`` and ``entity`` records written by the parse phase). Some
hierarchies are very large, so this script computes the metrics once, in
parallel, and writes a compact per-ontology table; consumers load that small
table instead of re-reading sidecars.

Reads:  ``<corpus>/corpus.db`` (the latest-version, parsed rows) and the
        sidecars under ``$ONTOMETER_STORE/cache/structure/v<N>/``; never the raw files.
Writes: ``<corpus>/structural_metrics.parquet``, one row per file hash.
Runs after: ``ontometer-corpus process`` (parse and dedup phases).
Needs ``--store`` (or ``$ONTOMETER_STORE``).

Keyed by raw-byte file_hash (matching the sidecar store and
term_coupling.parquet), so distinct files cannot inherit one another's
hierarchy merely because they declare the same class/property IRIs.

Depth model:
  - The hierarchy is a DAG (multiple inheritance is real). A node's parents
    are the targets of its subclass_edges; a "local root" is a node that never
    appears as a child within this ontology's own edges (its parent is
    external/imported or owl:Thing — depth 0).
  - An explicit `rdfs:subClassOf owl:Thing` is not an edge, and owl:Thing is
    not a class: whether a file writes that line depends on the tool that
    saved it, not on the hierarchy, so keeping it would give the same
    hierarchy different depth, size and branching. The line still counts as
    an asserted parent for multiple inheritance, as OQuaRE's TMOnto does.
  - Depth is computed by Kahn topological relaxation from the roots, which is
    exact for both the longest path (max over parents + 1) and the shortest
    path (min over parents + 1); both are kept, as separate columns. Nodes
    trapped in a cycle never reach in-degree 0; they are counted (n_cyclic)
    and excluded from the depth statistics rather than looping forever.

Metrics per ontology (all over the subclass-edge node set):
  n_nodes, n_roots, n_cyclic;
  n_classes, n_multiple_inheritance, multiple_inheritance_rate;
  n_orphans, orphan_rate;
  mean/max/std of longest-path depth, mean/std of shortest-path depth;
  branching = mean distinct-children fan-out over non-leaf nodes;
  depth_hist = node counts per longest-path depth level (list; index = depth),
               from which the shape of the depth distribution can be read.

Usage:
    ontometer-corpus structural-metrics --store /path/to/ontologies
    ontometer-corpus structural-metrics --store /path/to/ontologies --limit 50
"""

import sqlite3
from collections import defaultdict, deque
from pathlib import Path

import click

from ontometer_corpus.paths import CorpusPaths, corpus_paths

# Configured once by build(), before any worker is forked, so the children
# inherit it.
_PATHS: CorpusPaths | None = None


def _paths() -> CorpusPaths:
    global _PATHS
    if _PATHS is None:
        _PATHS = CorpusPaths.resolve()
    return _PATHS


# Wall-clock budget per ontology, enforced by build()'s subprocess supervisor
# (external SIGTERM/SIGKILL) rather than signal.alarm -- see term_coupling.py
# for why an in-process alarm can't reliably interrupt a C-level hang (e.g.
# pyarrow's parquet reader on a pathological sidecar). Kahn's algorithm itself
# is linear, so this is a generous budget even for very large hierarchies.
_PER_FILE_TIMEOUT_S = 120


def _sidecar_path(file_hash: str) -> Path:
    return _paths().sidecar_path(file_hash)


def _load_structure(file_hash: str) -> tuple[list[tuple[str, str]], set[str]] | None:
    """Named subclass edges and classes from one structural sidecar.

    Edge endpoints are included as classes even when a parser did not emit a
    separate declaration record. This makes the orphan denominator the union
    of declared and structurally implied named classes.
    """
    import pyarrow.parquet as pq

    path = _sidecar_path(file_hash)
    if not path.exists():
        return None
    tbl = pq.read_table(path, columns=["record_type", "subject", "object"])
    df = tbl.to_pandas()
    edge_rows = df[df["record_type"] == "subclass_edge"]
    edges = [(c, p) for c, p in zip(edge_rows["subject"], edge_rows["object"]) if c != p]
    entity_rows = df[(df["record_type"] == "entity") & (df["object"] == "class")]
    classes = set(entity_rows["subject"])
    classes.update(iri for edge in edges for iri in edge)
    return edges, classes


def _depths(children: dict, indeg: dict, nodes: set, mode: str) -> dict:
    """Kahn topological relaxation giving each node its longest- or
    shortest-path depth from a local root. Cyclic nodes (never reaching
    in-degree 0) are simply absent from the result."""
    remaining = dict(indeg)
    queue = deque(n for n in nodes if remaining[n] == 0)
    depth = {n: 0 for n in queue}
    while queue:
        n = queue.popleft()
        for child in children.get(n, ()):  # noqa: SIM118
            cand = depth[n] + 1
            if child not in depth:
                depth[child] = cand
            elif mode == "long":
                depth[child] = max(depth[child], cand)
            else:
                depth[child] = min(depth[child], cand)
            remaining[child] -= 1
            if remaining[child] == 0:
                queue.append(child)
    return depth


_OWL_THING = "http://www.w3.org/2002/07/owl#Thing"


def _metrics_from_structure(edges: list[tuple[str, str]], classes: set[str]) -> dict:
    import numpy as np

    thing_children = {c for c, p in edges if p == _OWL_THING}
    edges = [(c, p) for c, p in edges if _OWL_THING not in (c, p)]
    classes = (set(classes) | thing_children) - {_OWL_THING}

    children: dict[str, list[str]] = defaultdict(list)
    parents: dict[str, set[str]] = defaultdict(set)
    nodes: set[str] = set()
    for c, p in edges:
        children[p].append(c)
        parents[c].add(p)
        nodes.add(c)
        nodes.add(p)

    indeg = {n: len(parents.get(n, ())) for n in nodes}
    n_roots = sum(1 for n in nodes if indeg[n] == 0)
    n_classes = len(classes)
    n_multiple_inheritance = sum(
        1 for cls in classes if len(parents.get(cls, ())) + (cls in thing_children) > 1
    )
    n_orphans = len(classes - nodes)

    long_depth = _depths(children, indeg, nodes, "long")
    short_depth = _depths(children, indeg, nodes, "short")
    n_cyclic = len(nodes) - len(long_depth)

    long_vals = np.fromiter(long_depth.values(), dtype=float, count=len(long_depth))
    short_vals = np.fromiter(short_depth.values(), dtype=float, count=len(short_depth))

    # Distinct-children fan-out over non-leaf nodes (nodes that are a parent).
    fan = [len(set(cs)) for cs in children.values()]

    # Depth histogram (longest path): counts[d] = #nodes at depth d.
    if len(long_vals):
        maxd = int(long_vals.max())
        hist = np.bincount(long_vals.astype(int), minlength=maxd + 1).tolist()
    else:
        hist = []

    return {
        "n_nodes": len(nodes),
        "n_roots": n_roots,
        "n_cyclic": n_cyclic,
        "n_classes": n_classes,
        "n_multiple_inheritance": n_multiple_inheritance,
        "multiple_inheritance_rate": (n_multiple_inheritance / n_classes if n_classes else None),
        "n_orphans": n_orphans,
        "orphan_rate": n_orphans / n_classes if n_classes else None,
        "mean_depth_long": float(long_vals.mean()) if len(long_vals) else None,
        "max_depth_long": int(long_vals.max()) if len(long_vals) else None,
        "std_depth_long": float(long_vals.std()) if len(long_vals) else None,
        "mean_depth_short": float(short_vals.mean()) if len(short_vals) else None,
        "std_depth_short": float(short_vals.std()) if len(short_vals) else None,
        "branching": float(np.mean(fan)) if fan else None,
        "depth_hist": hist,
    }


_EMPTY = {
    "n_nodes": 0,
    "n_roots": 0,
    "n_cyclic": 0,
    "n_classes": 0,
    "n_multiple_inheritance": 0,
    "multiple_inheritance_rate": None,
    "n_orphans": 0,
    "orphan_rate": None,
    "mean_depth_long": None,
    "max_depth_long": None,
    "std_depth_long": None,
    "mean_depth_short": None,
    "std_depth_short": None,
    "branching": None,
    "depth_hist": [],
}


def _compute(file_hash: str) -> dict:
    rec = {"file_hash": file_hash, "ok": False, "error": None, **_EMPTY}
    structure = _load_structure(file_hash)
    if structure is None:
        rec["error"] = "MissingSidecar"
        return rec
    edges, classes = structure
    rec.update(_metrics_from_structure(edges, classes))
    rec["ok"] = True
    return rec


def _subprocess_target(file_hash: str, conn) -> None:
    """Runs in its own OS process (see build()) so a pathological sidecar
    (e.g. a stuck C-level pyarrow parquet read) can be killed from the
    OUTSIDE via SIGTERM/SIGKILL -- see term_coupling.py's build() for why
    signal.alarm alone is insufficient for this."""
    try:
        rec = _compute(file_hash)
    except BaseException as exc:  # incl. MemoryError
        rec = {
            "file_hash": file_hash,
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)[:120]}",
            **_EMPTY,
        }
    try:
        conn.send(rec)
    except (BrokenPipeError, OSError):
        pass
    finally:
        conn.close()


def build(
    paths: CorpusPaths,
    limit: int | None = None,
    workers: int = 8,
) -> None:
    import multiprocessing as mp
    import time
    from collections import deque

    global _PATHS
    _PATHS = paths

    conn = sqlite3.connect(str(paths.db_path))
    hashes = deque(
        r[0]
        for r in conn.execute(
            """
        SELECT DISTINCT file_hash FROM corpus_registry
        WHERE is_latest_version = 1 AND parse_status = 'ok'
              AND file_hash IS NOT NULL
        """
        ).fetchall()
    )
    conn.close()
    if limit:
        hashes = deque(list(hashes)[:limit])
    total = len(hashes)

    ctx = mp.get_context("fork")
    running: dict[int, list] = {}  # pid -> [process, conn, file_hash, start_time]
    records = []
    n_ok = n_fail = 0

    def _spawn(file_hash):
        parent_conn, child_conn = ctx.Pipe(duplex=False)
        p = ctx.Process(target=_subprocess_target, args=(file_hash, child_conn))
        p.start()
        child_conn.close()
        if p.pid is None:
            raise RuntimeError("structural-metrics worker did not receive a PID")
        running[p.pid] = [p, parent_conn, file_hash, time.monotonic()]

    while hashes or running:
        while len(running) < workers and hashes:
            _spawn(hashes.popleft())

        for pid, (p, pconn, file_hash, start) in list(running.items()):
            timed_out = time.monotonic() - start > _PER_FILE_TIMEOUT_S
            if pconn.poll(0.2):
                try:
                    rec = pconn.recv()
                except EOFError:
                    rec = {
                        "file_hash": file_hash,
                        "ok": False,
                        "error": "EOFError: worker died without a result",
                        **_EMPTY,
                    }
                p.join(2)
            elif timed_out:
                p.terminate()
                p.join(3)
                if p.is_alive():
                    p.kill()
                    p.join(3)
                rec = {
                    "file_hash": file_hash,
                    "ok": False,
                    "error": f"HardTimeout: exceeded {_PER_FILE_TIMEOUT_S}s wall clock",
                    **_EMPTY,
                }
            else:
                continue

            pconn.close()
            records.append(rec)
            n_ok += int(bool(rec["ok"]))
            n_fail += int(not bool(rec["ok"]))
            del running[pid]
            if len(records) % 200 == 0 or len(records) == total:
                print(f"  {len(records)}/{total} done  (ok={n_ok} fail={n_fail})", flush=True)

    import pyarrow as pa
    import pyarrow.parquet as pq

    out_path = paths.structural_metrics_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(records), out_path)
    print(f"Wrote {len(records)} rows to {out_path}  (ok={n_ok} fail={n_fail})")


@click.command()
@click.option("--limit", type=int, default=None, help="Process at most N ontologies.")
@click.option(
    "--workers", type=int, default=8, show_default=True, help="Concurrent worker processes."
)
@corpus_paths
def main(limit: int | None, workers: int, paths: CorpusPaths) -> None:
    """Per-ontology hierarchy metrics from the structural sidecars. Needs the object store."""
    paths.require_store_dir()
    build(paths, limit=limit, workers=workers)


if __name__ == "__main__":
    main()
