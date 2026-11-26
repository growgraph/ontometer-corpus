"""
The term-level network: homes, edge authority, term-document table.

Terms are interned to integer ids, each namespace is given a home ontology,
and every asserted subclass edge is classified by authority (see
``ontometer_corpus.network.entities``).

Reads:  ``<corpus>/corpus.db`` (one ontology per release family, its latest
        release), ``<corpus>/network_nodes.parquet`` (to attach network node
        ids), and for every ontology its structural sidecar under
        ``$ONTOMETER_STORE/cache/structure/v<N>/`` (subclass edges, declarations) and, where
        ``ontometer-corpus entity-roles`` produced one, its role table under
        ``$ONTOMETER_STORE/cache/roles/v<N>/`` (class/instance usage, labels).
Runs after: ``ontometer-corpus process``, then the network construction
(``uv run ontometer-corpus network``), and optionally
``ontometer-corpus entity-roles``. Without role tables it still runs, with roles
inferred from the sidecars alone (see below).
Needs ``--store`` (or ``$ONTOMETER_STORE``).

Writes, under ``$ONTOMETER_STORE/cache/``:

    entity_ontologies.parquet  one row per ontology (index ``onto``)
    entity_terms.parquet       term id, IRI, namespace, home ontology
    entity_edges.parquet       (onto, child, parent, kind) per asserted edge
    entity_docs.parquet        (onto, term, flags) — which terms each ontology
                               uses and in which roles (bit flags below)
    entity_labels.parquet      (onto, term, label)

Flag bits in entity_docs: 1 declared class, 2 declared individual,
4 declared property, 8 class role, 16 instance role. Where no role table
exists (the file exceeded the role pass's size guard, or failed it), roles are
inferred from the sidecar alone: declared classes and subclass endpoints are
class roles, declared individuals instance roles.

Usage:
    ontometer-corpus entity-network --store /path/to/ontologies
"""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict

import click
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ontometer_corpus.namespaces import iri_namespace
from ontometer_corpus.network.entities import (
    EXTERNAL,
    Claimant,
    classify_edges,
    pick_home,
)
from ontometer_corpus.paths import CorpusPaths, corpus_paths
from ontometer_corpus.registries import home_registry_sql
from ontometer_corpus.role_names import SKOS_CONCEPT_ROLE, SKOS_DOMINATED_KINDS
from ontometer_corpus.roles import CLASS_ROLES, INSTANCE_ROLES, is_meta
from ontometer_corpus.versions import family_key, release_date, release_order

F_DECL_CLASS, F_DECL_IND, F_DECL_PROP, F_CLASS, F_INST = 1, 2, 4, 8, 16
_PROPERTY_KINDS = {"object_property", "datatype_property", "annotation_property"}


def _ontologies(paths: CorpusPaths) -> pd.DataFrame:
    """One row per ontology: the latest release of each release family.

    The IRI-based dedup leaves every submission of an IRI-less OntoPortal
    ontology flagged as latest; grouping by release family keeps only the most
    recent. Cross-registry copies have already been resolved by dedup Pass D;
    a matching coarse content hash is not another identity decision here.
    """
    conn = sqlite3.connect(str(paths.db_path))
    # `registry` is the ontology's home registry, not the registry this copy
    # was fetched from (ontometer_corpus.registries); every table built from
    # this one inherits it.
    df = pd.read_sql(
        f"""
        SELECT id, source_id, file_hash, content_hash,
               {home_registry_sql(conn)} AS registry, ontology_iri,
               class_count, property_count, individual_count, skos_concept_count,
               triple_count, source_registry_metadata, shared_iri, kind
        FROM corpus_registry
        WHERE is_latest_version = 1 AND parse_status = 'ok' AND file_hash IS NOT NULL
        """,
        conn,
    )
    # SKOS thesauri have no OWL class hierarchy for this network to measure;
    # they stay in the dependency network (role_names.SKOS_DOMINATED_KINDS).
    df = df[~df.kind.isin(SKOS_DOMINATED_KINDS)].drop(columns="kind")
    # An IRI dedup found shared by distinct ontologies names none of them, so
    # it cannot win a namespace (entities.iri_names_namespace).
    df["identity_iri"] = df.ontology_iri.where(df.shared_iri.fillna(0) == 0)
    fam_sizes = pd.read_sql(
        "SELECT source_id FROM corpus_registry WHERE parse_status = 'ok' AND is_duplicate = 0",
        conn,
    )
    conn.close()
    df["family"] = df.source_id.map(family_key)
    df["release_date"] = df.source_registry_metadata.map(release_date)
    df["_order"] = [release_order(s, d) for s, d in zip(df.source_id, df.release_date)]
    df = df.sort_values(["family", "_order"]).groupby("family", as_index=False).tail(1)
    df = df.drop(columns=["_order"])
    n_rel = fam_sizes.source_id.map(family_key).value_counts()
    df["n_releases"] = df.family.map(n_rel).fillna(1).astype(int)
    nodes = pd.read_parquet(
        paths.network_nodes_path, columns=["node_id", "file_hash", "is_external"]
    )
    nodes = nodes[~nodes.is_external].drop_duplicates("file_hash")
    df = df.merge(nodes[["node_id", "file_hash"]], on="file_hash", how="left")
    df = df.sort_values(["family", "id"]).reset_index(drop=True)
    df["onto"] = np.arange(len(df))
    return df


def _meta_mask(values: pd.Series) -> np.ndarray:
    return np.fromiter((is_meta(v) for v in values), dtype=bool, count=len(values))


class Interner:
    def __init__(self) -> None:
        self.ids: dict[str, int] = {}

    def ids_of(self, values) -> np.ndarray:
        get = self.ids.setdefault
        n = self.ids
        return np.fromiter((get(v, len(n)) for v in values), dtype=np.int64, count=len(values))

    def names(self) -> list[str]:
        out = [""] * len(self.ids)
        for name, i in self.ids.items():
            out[i] = name
        return out


def build(paths: CorpusPaths) -> None:
    t0 = time.time()
    onts = _ontologies(paths)
    print(f"{len(onts)} ontologies")
    terms = Interner()
    edge_parts: list[tuple[int, np.ndarray, np.ndarray]] = []
    doc_parts: list[pd.DataFrame] = []
    label_parts: list[pd.DataFrame] = []
    rich = np.zeros(len(onts), dtype=bool)
    has_sidecar = np.zeros(len(onts), dtype=bool)
    skos_by_onto: dict[int, dict[str, int]] = {}

    columns = ["onto", "file_hash"]
    for onto, file_hash in onts[columns].itertuples(index=False, name=None):
        onto = int(onto)
        flags: dict[int, int] = defaultdict(int)
        concepts: dict[str, int] = defaultdict(int)
        side = paths.sidecar_path(file_hash)
        if side.exists():
            has_sidecar[onto] = True
            t = pq.read_table(side).to_pandas()
            e = t[t.record_type == "subclass_edge"]
            keep = ~(_meta_mask(e.subject) | _meta_mask(e.object)) & (
                e.subject.to_numpy() != e.object.to_numpy()
            )
            e = e[keep]
            c = terms.ids_of(e.subject.tolist())
            p = terms.ids_of(e.object.tolist())
            edge_parts.append((onto, c, p))
            ent = t[t.record_type == "entity"]
            ent = ent[~_meta_mask(ent.subject)]
            ids = terms.ids_of(ent.subject.tolist())
            for i, kind in zip(ids.tolist(), ent.object.tolist()):
                if kind == "class":
                    flags[i] |= F_DECL_CLASS
                elif kind == "individual":
                    flags[i] |= F_DECL_IND
                elif kind in _PROPERTY_KINDS:
                    flags[i] |= F_DECL_PROP
            for i in c.tolist() + p.tolist():
                flags[i] |= F_CLASS
            for iri in t.subject[t.record_type == "skos_concept"]:
                ns = iri_namespace(iri)
                if ns:
                    concepts[ns] += 1
        roles_file = paths.roles_path(file_hash)
        if roles_file.exists():
            rich[onto] = True
            r = pq.read_table(roles_file).to_pandas()
            rr = r[r.record_type == "role"]
            if not concepts:
                for iri in rr.subject[rr.object == SKOS_CONCEPT_ROLE]:
                    ns = iri_namespace(iri)
                    if ns:
                        concepts[ns] += 1
            is_c = rr.object.isin(CLASS_ROLES).to_numpy()
            is_i = rr.object.isin(INSTANCE_ROLES).to_numpy()
            ids = terms.ids_of(rr.subject.tolist())
            for i, ic, ii in zip(ids.tolist(), is_c.tolist(), is_i.tolist()):
                flags[i] |= (F_CLASS if ic else 0) | (F_INST if ii else 0)
            lab = r[r.record_type == "label"]
            if len(lab):
                label_parts.append(
                    pd.DataFrame(
                        {
                            "onto": onto,
                            "term": terms.ids_of(lab.subject.tolist()),
                            "label": lab.object.to_numpy(),
                        }
                    )
                )
        else:
            # Sidecar-only fallback: declared individuals are instance roles.
            for i, f in list(flags.items()):
                if f & F_DECL_IND:
                    flags[i] |= F_INST
                if f & F_DECL_CLASS:
                    flags[i] |= F_CLASS
        if concepts:
            skos_by_onto[onto] = dict(concepts)
        if flags:
            k = np.fromiter(flags.keys(), dtype=np.int64, count=len(flags))
            v = np.fromiter(flags.values(), dtype=np.int64, count=len(flags))
            doc_parts.append(pd.DataFrame({"onto": onto, "term": k, "flags": v.astype(np.int16)}))
        if onto % 500 == 0:
            print(
                f"  loaded {onto}/{len(onts)}  terms={len(terms.ids):,}  ({time.time() - t0:.0f}s)",
                flush=True,
            )

    names = terms.names()
    print(f"interned {len(names):,} terms ({time.time() - t0:.0f}s)")
    docs = pd.concat(doc_parts, ignore_index=True)
    del doc_parts

    # --- namespaces and homes -------------------------------------------------
    ns_of = [iri_namespace(n) or "" for n in names]
    ns_index: dict[str, int] = {}
    term_ns = np.fromiter(
        (ns_index.setdefault(n, len(ns_index)) for n in ns_of), dtype=np.int64, count=len(ns_of)
    )
    ns_names = [""] * len(ns_index)
    for n, i in ns_index.items():
        ns_names[i] = n

    # Declarations per (ontology, namespace) — the size tie-break.
    declared = docs[(docs["flags"] & (F_DECL_CLASS | F_DECL_IND | F_DECL_PROP)) > 0]
    decl_counts = (
        pd.DataFrame({"onto": declared.onto.to_numpy(), "ns": term_ns[declared.term.to_numpy()]})
        .value_counts()
        .to_dict()
    )
    # Every ontology declaring terms in a namespace claims it. Claims are read
    # from the declarations themselves rather than the registry's
    # own_namespaces column, which is missing for some rows and drops shared
    # vocabularies (FOAF, PROV, SKOS, ...) that still need a home.
    claims: dict[int, list[Claimant]] = defaultdict(list)
    registry = onts.registry.to_numpy()
    ont_iri = onts.identity_iri.to_numpy()
    for (o, nsi), n in decl_counts.items():
        if ns_names[nsi] and not is_meta(ns_names[nsi]):
            claims[nsi].append(
                Claimant(
                    int(o),
                    registry[o],
                    ont_iri[o],
                    int(n),
                    skos_by_onto.get(int(o), {}).get(ns_names[nsi], 0),
                )
            )
    ns_home = np.full(len(ns_names), EXTERNAL, dtype=np.int64)
    for nsi, cl in claims.items():
        ns_home[nsi] = pick_home(ns_names[nsi], cl)
    term_home = ns_home[term_ns]
    print(f"{len(ns_names):,} namespaces, {(ns_home != EXTERNAL).sum():,} homed in the corpus")

    # --- edge authority ------------------------------------------------------
    e_onto, e_c, e_p, e_kind = [], [], [], []
    for onto, c, p in edge_parts:
        if not len(c):
            continue
        e_onto.append(np.full(len(c), onto, dtype=np.int32))
        e_c.append(c)
        e_p.append(p)
        e_kind.append(classify_edges(onto, term_home[c], term_home[p]))
    edges = pd.DataFrame(
        {
            "onto": np.concatenate(e_onto),
            "child": np.concatenate(e_c),
            "parent": np.concatenate(e_p),
            "kind": np.concatenate(e_kind),
        }
    ).drop_duplicates()
    print(
        f"{len(edges):,} asserted edges; by kind: {edges.kind.value_counts().sort_index().to_dict()}"
    )

    # --- write ---------------------------------------------------------------
    out = paths.cache_dir
    out.mkdir(parents=True, exist_ok=True)
    onts = onts.assign(
        has_sidecar=has_sidecar,
        rich_roles=rich,
    ).drop(columns=["source_registry_metadata"])
    onts.to_parquet(out / "entity_ontologies.parquet", index=False)
    pq.write_table(
        pa.table(
            {
                "term": np.arange(len(names)),
                "iri": names,
                "ns": ns_of,
                "home": term_home,
            }
        ),
        out / "entity_terms.parquet",
    )
    edges.to_parquet(out / "entity_edges.parquet", index=False)
    docs.to_parquet(out / "entity_docs.parquet", index=False)
    if label_parts:
        pd.concat(label_parts, ignore_index=True).to_parquet(
            out / "entity_labels.parquet", index=False
        )
    print(f"Wrote entity tables to {out}  ({time.time() - t0:.0f}s)")


@click.command()
@corpus_paths
def main(paths: CorpusPaths) -> None:
    """Term-level homes, edge authority and term-document table."""
    paths.require_store_dir()
    build(paths)


if __name__ == "__main__":
    main()
