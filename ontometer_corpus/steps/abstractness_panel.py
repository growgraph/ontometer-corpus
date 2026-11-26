"""
Abstractness across releases of the same ontology.

Registries keep release histories: OntoPortal submissions and LOV versions are
each fetched and parsed rows, grouped into release families by
``ontometer_corpus.versions``. This script measures every selected release on
its own terms — its own hierarchy, its own role table — so the trajectory of
one ontology is not confounded by the rest of the corpus changing around it.

Runs after ``ontometer-corpus entity-network``. Needs ``--store`` (or
``$ONTOMETER_STORE``) for both steps. Three commands, in order:

    ontometer-corpus panel select --store /path/to/ontologies
    ontometer-corpus entity-roles --panel --store /path/to/ontologies
    ontometer-corpus panel measure --store /path/to/ontologies

``select`` reads ``<corpus>/corpus.db`` and
``$ONTOMETER_STORE/cache/entity_ontologies.parquet`` and writes
``$ONTOMETER_STORE/cache/panel_releases.parquet``. It keeps families of the
entity network with at least two dated releases of distinct content, and at
most ``--per-family`` releases each, spread evenly over the family's history
(first and last always kept).

``entity_roles.py --panel`` writes the role tables of the selected releases
under ``$ONTOMETER_STORE/cache/roles/v<N>/`` and their coverage table
``$ONTOMETER_STORE/cache/roles_summary_panel.parquet``.

``measure`` reads the panel, ``$ONTOMETER_STORE/cache/entity_terms.parquet``,
each release's sidecar under ``$ONTOMETER_STORE/cache/structure/v<N>/`` and its role table
where there is one, and writes ``<corpus>/abstractness_panel.parquet``. A
selected release whose sidecar is missing is recorded with ``ok`` false and
not measured (the parse phase writes a sidecar for every parsed row). Without
a role table, roles come from the sidecar's declarations and subclass edges.

``measure`` computes, per release, over the terms homed in the family's
namespaces (as assigned in the static network):

* ``A_local`` — mean generality ``1 - IC`` of the home classes in the release's
  own hierarchy, and ``A_null`` — the same after replacing the growth since the
  previous release with the same number of classes attached as random leaves.
  Adding leaves mechanically shifts IC, so the claim that abstraction rises is
  judged by ``A_local - A_null``, not by ``A_local`` alone.
* ``class_share`` / ``instance_share`` / ``punned_share`` of home terms (sense C);
* between consecutive releases: *promotions* (a term used only as an instance
  becomes a class), *demotions*, and *abstractions* (a home class that was a
  leaf gains subclasses, or a new class appears above existing ones).
"""

from __future__ import annotations

import sqlite3

import click
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from ontometer_corpus.metrics.abstractness import dag_closure, encode_edges, ic_seco
from ontometer_corpus.namespaces import iri_namespace
from ontometer_corpus.paths import CorpusPaths, corpus_paths
from ontometer_corpus.roles import CLASS_ROLES, INSTANCE_ROLES, is_meta
from ontometer_corpus.versions import family_key, release_date, release_order

_PANEL_FILE = "panel_releases.parquet"


def select(paths: CorpusPaths, per_family: int) -> None:
    conn = sqlite3.connect(str(paths.db_path))
    rows = pd.read_sql(
        """
        SELECT id, source_id, file_hash, content_hash, retrieved_file, parse_format,
               source_registry_metadata
        FROM corpus_registry
        WHERE parse_status = 'ok' AND is_duplicate = 0 AND file_hash IS NOT NULL
        """,
        conn,
    )
    conn.close()
    rows["family"] = rows.source_id.map(family_key)
    rows["release_date"] = rows.source_registry_metadata.map(release_date)
    rows = rows.dropna(subset=["release_date"])
    onts = pd.read_parquet(paths.cache_dir / "entity_ontologies.parquet")
    rows = rows[rows.family.isin(onts.family)]
    # One row per byte-distinct release within a family: the earliest registry
    # entry carrying it. The coarse class/property hash is not release identity.
    rows["_order"] = [release_order(s, d) for s, d in zip(rows.source_id, rows.release_date)]
    rows = rows.sort_values(["family", "_order"]).drop_duplicates(["family", "file_hash"])
    keep = []
    for _, fam in rows.groupby("family"):
        if len(fam) < 2:
            continue
        idx = np.unique(np.linspace(0, len(fam) - 1, min(per_family, len(fam))).round().astype(int))
        keep.append(fam.iloc[idx].assign(release_index=np.arange(len(idx))))
    panel = pd.concat(keep, ignore_index=True).drop(columns=["_order", "source_registry_metadata"])
    panel = panel.merge(
        onts[["family", "onto"]].rename(columns={"onto": "family_onto"}), on="family"
    )
    out = paths.cache_dir / _PANEL_FILE
    panel.to_parquet(out, index=False)
    print(f"Wrote {out}: {panel.family.nunique()} families, {len(panel)} releases")


def _load_release(
    paths: CorpusPaths, file_hash: str
) -> tuple[list[tuple[str, str]], dict[str, set[str]]] | None:
    side = paths.sidecar_path(file_hash)
    if not side.exists():
        return None
    t = pq.read_table(side).to_pandas()
    e = t[t.record_type == "subclass_edge"]
    edges = [
        (c, p) for c, p in zip(e.subject, e.object) if c != p and not is_meta(c) and not is_meta(p)
    ]
    roles: dict[str, set[str]] = {}
    rf = paths.roles_path(file_hash)
    if rf.exists():
        r = pq.read_table(rf).to_pandas()
        r = r[r.record_type == "role"]
        for s, o in zip(r.subject, r.object):
            roles.setdefault(s, set()).add(o)
    else:
        ent = t[t.record_type == "entity"]
        for s, k in zip(ent.subject, ent.object):
            if k == "class":
                roles.setdefault(s, set()).add("decl_class")
            elif k == "individual":
                roles.setdefault(s, set()).add("decl_individual")
        for c, p in edges:
            roles.setdefault(c, set()).add("sub_child")
            roles.setdefault(p, set()).add("sub_parent")
    return edges, roles


def _generality(edges: list[tuple[str, str]], home: set[str]) -> tuple[float, dict[str, int]]:
    """Mean ``1 - IC`` over home classes in this hierarchy, and child counts."""
    if not edges or not home:
        return (0.0 if home else np.nan), {}
    names, ch, pa = encode_edges(edges)
    cl = dag_closure(len(names), ch, pa)
    gen = 1.0 - ic_seco(cl.n_desc, len(names))
    by_name = dict(zip(names, gen))
    kids = dict(zip(names, np.bincount(pa, minlength=len(names)).tolist()))
    return float(np.mean([by_name.get(t, 0.0) for t in home])), kids


def _null_generality(
    prev_edges: list[tuple[str, str]], n_new: int, home_prev: set[str], rng: np.random.Generator
) -> float:
    """Generality after adding *n_new* classes as leaves under random existing classes."""
    nodes = sorted({x for e in prev_edges for x in e} | home_prev)
    if not nodes:
        return np.nan
    targets = rng.choice(len(nodes), size=n_new, replace=True) if n_new > 0 else []
    synth = [(f"__null_{i}", nodes[j]) for i, j in enumerate(targets)]
    home = home_prev | {c for c, _ in synth}
    g, _ = _generality(prev_edges + synth, home)
    return g


def measure(paths: CorpusPaths, seed: int) -> None:
    rng = np.random.default_rng(seed)
    panel = pd.read_parquet(paths.cache_dir / _PANEL_FILE)
    terms = pd.read_parquet(paths.cache_dir / "entity_terms.parquet", columns=["ns", "home"])
    home_ns = terms[terms.home >= 0].drop_duplicates("ns").groupby("home").ns.apply(set).to_dict()

    records = []
    for family, fam in panel.sort_values(["family", "release_index"]).groupby("family"):
        ns = home_ns.get(int(fam.family_onto.iloc[0]), set())
        prev = None
        columns = [
            "release_index",
            "release_date",
            "file_hash",
            "content_hash",
            "source_id",
        ]
        for release_index, release_day, file_hash, content_hash, source_id in fam[
            columns
        ].itertuples(index=False, name=None):
            loaded = _load_release(paths, file_hash)
            rec = {
                "family": family,
                "release_index": release_index,
                "release_date": release_day,
                "file_hash": file_hash,
                "content_hash": content_hash,
                "source_id": source_id,
                "rich_roles": paths.roles_path(file_hash).exists(),
                "ok": loaded is not None,
            }
            if loaded is None:
                records.append(rec)
                prev = None
                continue
            edges, roles = loaded
            home_terms = {t for t in roles if iri_namespace(t) in ns}
            is_cls = {t for t in home_terms if roles[t] & CLASS_ROLES}
            is_ins = {t for t in home_terms if roles[t] & INSTANCE_ROLES}
            home_classes = is_cls
            a_local, kids = _generality(edges, home_classes)
            rec.update(
                n_home_terms=len(home_terms),
                n_home_classes=len(home_classes),
                n_edges=len(edges),
                A_local=a_local,
                class_share=len(is_cls - is_ins) / len(home_terms) if home_terms else np.nan,
                instance_share=len(is_ins - is_cls) / len(home_terms) if home_terms else np.nan,
                punned_share=len(is_cls & is_ins) / len(home_terms) if home_terms else np.nan,
            )
            if prev is not None:
                children: dict[str, set[str]] = {}
                for c, p in edges:
                    children.setdefault(p, set()).add(c)
                p_edges, p_cls, p_ins, p_kids = prev
                p_inst_only = p_ins - p_cls
                p_cls_only = p_cls - p_ins
                common = (p_cls | p_ins) & home_terms
                new_classes = home_classes - p_cls
                rec.update(
                    n_common=len(common),
                    n_new_classes=len(new_classes),
                    n_removed_classes=len(p_cls - home_classes),
                    promotions=len(p_inst_only & is_cls),
                    demotions=len(p_cls_only & (is_ins - is_cls)),
                    leaf_to_parent=sum(
                        1
                        for t in p_cls & home_classes
                        if p_kids.get(t, 0) == 0 and kids.get(t, 0) > 0
                    ),
                    new_above=sum(1 for t in new_classes if children.get(t, set()) & p_cls),
                    A_null=_null_generality(p_edges, max(len(new_classes), 0), p_cls, rng),
                )
            prev = (edges, is_cls, is_ins, kids)
            records.append(rec)
        if len(records) % 200 < len(fam):
            print(f"  {len(records)} releases measured", flush=True)

    out = paths.corpus_dir / "abstractness_panel.parquet"
    pd.DataFrame.from_records(records).to_parquet(out, index=False)
    print(f"Wrote {out} ({len(records)} releases)")


@click.group()
def main() -> None:
    """Abstractness across releases of the same ontology.

    Run select, then entity_roles.py --panel, then measure.
    """


@main.command("select")
@click.option(
    "--per-family", type=int, default=12, show_default=True, help="Most releases kept per family."
)
@corpus_paths
def select_command(per_family: int, paths: CorpusPaths) -> None:
    """Choose releases per family, spread over its history."""
    paths.require_store_dir()
    select(paths, per_family)


@main.command("measure")
@click.option(
    "--seed", type=int, default=0, show_default=True, help="Seed of the null-growth model."
)
@corpus_paths
def measure_command(seed: int, paths: CorpusPaths) -> None:
    """Compute abstractness per selected release."""
    paths.require_store_dir()
    measure(paths, seed)


if __name__ == "__main__":
    main()
