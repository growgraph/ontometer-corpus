"""
Per-term class/instance roles and labels (abstractness, sense C).

The structural sidecars record declarations and subclass edges, which is enough
for taxonomic generality but not for class-likeness: whether a term is *used*
as a class (it types something, it fills a restriction) or as an instance (it
is typed, it takes part in facts) needs the rest of the file. This pass
re-reads each canonical latest-version file once.

Reads:  ``<corpus>/corpus.db`` (latest-version, parsed rows) and the raw
        files under ``$ONTOMETER_STORE/objects/``; with ``--panel``, the releases
        listed in ``$ONTOMETER_STORE/cache/panel_releases.parquet`` instead.
Writes, per ontology,

    $ONTOMETER_STORE/cache/roles/v<N>/<hh>/<file_hash>.parquet
        record_type='role'  → (subject=term IRI, object=role name)
        record_type='label' → (subject=term IRI, object=preferred label)

plus a per-ontology coverage table, ``$ONTOMETER_STORE/cache/roles_summary.parquet``
(``roles_summary_panel.parquet`` with ``--panel``). Ontologies already done
are skipped unless ``--force``.
Runs after: ``ontometer-corpus process``; with ``--panel``, after
``ontometer-corpus panel select``.
Needs ``--store`` (or ``$ONTOMETER_STORE``).

Role names and the fact definition are documented in ``ontometer_corpus.roles``.

Files are loaded exactly as the parse phase loads them — archive
decompression, the tolerant rdflib dispatch with syntax repairs, the
owlready2 fallback — so the role pass sees the same graph the sidecar came
from. Files larger than the size guard once decompressed are skipped here;
their declaration and subclass roles still come from the sidecar downstream,
so they lose only the richer usage roles and labels.

Usage:
    ontometer-corpus entity-roles --store /path/to/ontologies --limit 50
    ontometer-corpus entity-roles --store /path/to/ontologies --workers 10
    ontometer-corpus entity-roles --store /path/to/ontologies --panel
"""

from __future__ import annotations

import bz2
import gzip
import sqlite3
import warnings
import zipfile
from pathlib import Path

import click

from ontometer_corpus.paths import CorpusPaths, corpus_paths
from ontometer_corpus.supervisor import run_supervised

warnings.filterwarnings("ignore")

_PER_FILE_TIMEOUT_S = 120
_MAX_BYTES = 50 * 1024 * 1024
_ARCHIVE_MEMBERS = (".owl", ".rdf", ".ttl", ".obo", ".nt", ".xml")

_PATHS: CorpusPaths | None = None


def _read_bytes(path: Path) -> tuple[bytes, str]:
    """File content, decompressed the way the parse phase does, and its suffix."""
    with open(path, "rb") as f:
        sig = f.read(4)
    suffix = path.suffix.lower() or ".owl"
    if sig.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            cands = [n for n in names if n.lower().endswith(_ARCHIVE_MEMBERS)]
            chosen = cands[0] if cands else (names[0] if names else None)
            if chosen is None:
                raise ValueError("empty zip")
            if z.getinfo(chosen).file_size > _MAX_BYTES:
                raise OverflowError(
                    f"skipped: {z.getinfo(chosen).file_size / 1e6:.0f}MB > size guard"
                )
            return z.read(chosen), Path(chosen).suffix or ".owl"
    if sig.startswith(b"\x1f\x8b"):
        with gzip.open(path, "rb") as g:
            data = g.read(_MAX_BYTES + 1)
        inner = path.with_suffix("").suffix if suffix == ".gz" else suffix
        return data, inner or ".owl"
    if sig.startswith(b"BZh"):
        with bz2.open(path, "rb") as b:
            data = b.read(_MAX_BYTES + 1)
        inner = path.with_suffix("").suffix if suffix == ".bz2" else suffix
        return data, inner or ".owl"
    if path.stat().st_size > _MAX_BYTES:
        raise OverflowError(f"skipped: {path.stat().st_size / 1e6:.0f}MB > size guard")
    return path.read_bytes(), suffix


def _extract(path: Path, parse_format: str) -> tuple[dict[str, set[str]], dict[str, str], str]:
    from ontometer_corpus.pipeline.parse import try_owlready2, try_rdflib
    from ontometer_corpus.roles import roles_from_graph, roles_from_obo

    raw, suffix = _read_bytes(path)
    if len(raw) > _MAX_BYTES:
        raise OverflowError(f"skipped: >{_MAX_BYTES / 1e6:.0f}MB decompressed > size guard")
    head = raw[:10000].lower()
    is_obo = parse_format == "obo" or b"format-version:" in head or b"[term]" in head
    if is_obo:
        roles, labels = roles_from_obo(
            raw.decode("utf-8", errors="ignore"), fallback_name=path.stem.lower()
        )
        return roles, labels, "obo"
    graph, _, err = try_rdflib(raw)
    via = "rdflib"
    if graph is None:
        graph, _, err = try_owlready2(raw, suffix)
        via = "owlready2"
    if graph is None:
        raise RuntimeError(f"unparseable: {err}")
    roles, labels = roles_from_graph(graph)
    return roles, labels, via


def _write(path: Path, roles: dict[str, set[str]], labels: dict[str, str]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq
    from suthing import atomic_open

    rt: list[str] = []
    subj: list[str] = []
    obj: list[str] = []
    for iri, rs in roles.items():
        for r in sorted(rs):
            rt.append("role")
            subj.append(iri)
            obj.append(r)
    for iri, lab in labels.items():
        rt.append("label")
        subj.append(iri)
        obj.append(lab)
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_open(path) as f:
        pq.write_table(pa.table({"record_type": rt, "subject": subj, "object": obj}), f)


def _has_skos_concept_roles(path: Path) -> bool:
    """Whether an existing role artifact contains the namespace-local marker."""
    if not path.exists():
        return False
    import pyarrow.parquet as pq

    from ontometer_corpus.role_names import SKOS_CONCEPT_ROLE

    objects = pq.read_table(path, columns=["object"])["object"]
    return SKOS_CONCEPT_ROLE in objects.to_pylist()


def _has_skos_concept_sidecar(path: Path) -> bool:
    """Whether a structural sidecar already carries concept-local evidence."""
    if not path.exists():
        return False
    import pyarrow.parquet as pq

    kinds = pq.read_table(path, columns=["record_type"])["record_type"]
    return "skos_concept" in kinds.to_pylist()


def _empty(task: tuple[str, str, str], error: str) -> dict:
    return {
        "file_hash": task[0],
        "ok": False,
        "via": None,
        "n_terms": 0,
        "n_class": 0,
        "n_instance": 0,
        "n_punned": 0,
        "n_labels": 0,
        "error": error,
    }


def _process(task: tuple[str, str, str]) -> dict:
    from ontometer_corpus.roles import summarise

    file_hash, retrieved_file, parse_format = task
    assert _PATHS is not None
    out = _PATHS.roles_path(file_hash)
    roles, labels, via = _extract(Path(retrieved_file), parse_format)
    _write(out, roles, labels)
    flags = summarise(roles).values()
    return {
        "file_hash": file_hash,
        "ok": True,
        "via": via,
        "n_terms": len(roles),
        "n_class": sum(1 for c, _ in flags if c),
        "n_instance": sum(1 for _, i in flags if i),
        "n_punned": sum(1 for c, i in flags if c and i),
        "n_labels": len(labels),
        "error": None,
    }


def build(paths: CorpusPaths, limit: int | None, workers: int, force: bool, panel: bool) -> None:
    global _PATHS
    _PATHS = paths
    refresh_skos: set[str] = set()
    if panel:
        import pandas as pd

        sel = pd.read_parquet(paths.cache_dir / "panel_releases.parquet")
        sel = sel.drop_duplicates("file_hash").sort_values("file_hash")
        rows = list(zip(sel.file_hash, sel.retrieved_file, sel.parse_format))
    else:
        conn = sqlite3.connect(str(paths.db_path))
        db_rows = conn.execute(
            """
            SELECT file_hash, retrieved_file, parse_format, skos_concept_count
            FROM corpus_registry
            WHERE id IN (
                SELECT MIN(id) FROM corpus_registry
                WHERE is_latest_version = 1 AND parse_status = 'ok' AND file_hash IS NOT NULL
                GROUP BY file_hash)
            ORDER BY file_hash
            """
        ).fetchall()
        conn.close()
        refresh_skos = {
            file_hash
            for file_hash, _, _, n_skos in db_rows
            if (n_skos or 0) > 0
            and not _has_skos_concept_sidecar(paths.sidecar_path(file_hash))
            and not _has_skos_concept_roles(paths.roles_path(file_hash))
        }
        rows = [
            (file_hash, retrieved_file, parse_format)
            for file_hash, retrieved_file, parse_format, _ in db_rows
        ]
    if limit:
        rows = rows[:limit]

    summary_path = paths.cache_dir / (
        "roles_summary_panel.parquet" if panel else "roles_summary.parquet"
    )
    previous: dict[str, dict] = {}
    if summary_path.exists() and not force:
        import pandas as pd

        old = pd.read_parquet(summary_path)
        if "file_hash" in old:
            for rec in old.to_dict("records"):
                if (
                    rec["ok"]
                    and rec["file_hash"] not in refresh_skos
                    and paths.roles_path(rec["file_hash"]).exists()
                ):
                    previous[rec["file_hash"]] = rec

    tasks = [(h, str(paths.resolve_object(f)), fmt) for h, f, fmt in rows if h not in previous]
    print(f"{len(rows)} ontologies; {len(previous)} already done; {len(tasks)} to process")
    records = run_supervised(
        tasks,
        _process,
        _empty,
        timeout_s=_PER_FILE_TIMEOUT_S,
        workers=workers,
        progress_every=100,
        label="parsed",
    )
    records += list(previous.values())

    import pyarrow as pa
    import pyarrow.parquet as pq

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(records), summary_path)
    n_ok = sum(1 for r in records if r["ok"])
    print(f"Wrote {summary_path}  ({n_ok}/{len(records)} ok)")


@click.command()
@click.option("--limit", type=int, default=None, help="Process at most N ontologies.")
@click.option(
    "--workers", type=int, default=8, show_default=True, help="Concurrent worker processes."
)
@click.option("--force", is_flag=True, help="Recompute ontologies already done.")
@click.option(
    "--panel", is_flag=True, help="Process the releases chosen by `ontometer-corpus panel select`."
)
@corpus_paths
def main(limit: int | None, workers: int, force: bool, panel: bool, paths: CorpusPaths) -> None:
    """Per-term class/instance roles and labels. Needs the object store."""
    paths.require_store_dir()
    build(paths, limit=limit, workers=workers, force=force, panel=panel)


if __name__ == "__main__":
    main()
