"""
Abstractness of terms and ontologies, in two senses.

Consumes the term-level network written by ontometer-corpus entity-network and computes

* per term (``$ONTOMETER_STORE/cache/entity_abstractness.parquet``):
    sense G, generality — depth and intrinsic IC in the corpus-wide
      subsumption DAG G, and the number and diversity of foreign ontologies
      that specialise the term;
    sense C, class-likeness — the share of documents using the term that use
      it as a class, and whether any document puns it;
    popularity — foreign declaration and reference counts, kept apart because
      heavily reused terms are not thereby general.
* per ontology (``<corpus>/ontology_abstractness.parquet``, a small table):
    the mean generality and class-likeness of the terms it owns, measured
    locally (its own hierarchy only) and globally (in G); Martin's couplings
    Ca/Ce, instability I and main-sequence distance D; its layer in the
    ontology-level specialisation DAG; and the population flags that separate
    ontologies with a hierarchy from knowledge-base-like and flat vocabularies.
    The ``subject`` column marks the *measured ontologies*: those with a class
    hierarchy and no more individuals than classes.

G is the union of edges asserted with authority (own and bridge edges) plus
copied edges about terms no corpus ontology owns — for those the copies are
the only evidence of their hierarchy. Definitions and edge authority are in
``ontometer_corpus.metrics.abstractness`` and ``ontometer_corpus.network.entities``.

Reads:  ``$ONTOMETER_STORE/cache/entity_{ontologies,terms,edges,docs}.parquet``.
Writes: the two tables above.
Runs after: ``ontometer-corpus entity-network``.
Needs ``--store`` (or ``$ONTOMETER_STORE``).

Usage:
    ontometer-corpus abstractness --store /path/to/ontologies
"""

from __future__ import annotations

import time
from typing import cast

import click
import numpy as np
import pandas as pd

from ontometer_corpus.metrics.abstractness import (
    dag_closure,
    effective_number,
    ic_sanchez,
    ic_seco,
    main_sequence_distance,
    martin_instability,
    role_share,
)
from ontometer_corpus.network.entities import (
    EDGE_BRIDGE,
    EDGE_COPY,
    EDGE_OWN_FOREIGN,
    EDGE_OWN_INTERNAL,
    EXTERNAL,
)
from ontometer_corpus.paths import CorpusPaths, corpus_paths
from ontometer_corpus.roles import kb_individuals

F_DECL_CLASS, F_DECL_IND, F_DECL_PROP, F_CLASS, F_INST = 1, 2, 4, 8, 16
F_DECL = F_DECL_CLASS | F_DECL_IND | F_DECL_PROP

#: Population thresholds: an ontology is measured (``subject``) when its own
#: hierarchy has at least this many nodes and this depth, and it is not
#: knowledge-base-like (more individuals than classes; SKOS concepts count as
#: terms, see kb_individuals). Swept by
#: ``ontometer-corpus validation``.
MIN_HIERARCHY_NODES = 5
MIN_MAX_DEPTH = 2


def _communities(
    docs: pd.DataFrame, term_home: np.ndarray, n_onto: int, seed: int = 0
) -> np.ndarray:
    """Louvain communities of the ontology reuse graph built from term usage.

    Two ontologies are linked when one uses terms the other owns, weighted by
    the number of such terms. Returns a community id per ontology, numbered by
    size and then by smallest member. The graph is built in sorted edge order,
    because Louvain's result depends on insertion order even under a fixed seed.
    """
    import networkx as nx

    home = term_home[docs.term.to_numpy()]
    user = docs.onto.to_numpy()
    mask = (home != EXTERNAL) & (home != user)
    a, b = user[mask], home[mask]
    pairs = (
        pd.DataFrame({"a": np.minimum(a, b), "b": np.maximum(a, b)})
        .value_counts()
        .rename("w")
        .reset_index()
        .sort_values(["a", "b"], ignore_index=True)
    )
    g = nx.Graph()
    g.add_nodes_from(range(n_onto))
    g.add_weighted_edges_from(pairs.itertuples(index=False))
    comms = nx.community.louvain_communities(g, weight="weight", seed=seed)
    out = np.empty(n_onto, dtype=np.int64)
    for cid, members in enumerate(sorted(comms, key=lambda m: (-len(m), min(m)))):
        for m in cast(set[int], members):
            out[m] = cid
    return out


def _layers(n: int, src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Longest path to a sink in the condensed ontology specialisation DAG.

    An ontology that specialises nobody's classes is layer 0; one that builds
    only on layer-0 ontologies is layer 1; and so on. Members of a dependency
    cycle share a layer.
    """
    from ontometer_corpus.metrics.abstractness import _condense

    comp, cs, cd, _ = _condense(n, src, dst)
    m = int(comp.max()) + 1 if n else 0
    # Treat "specialises a class of" as child -> parent: the roots are the
    # ontologies that specialise nobody, and depth from them is the layer.
    closure = dag_closure(m, cs, cd)
    return closure.depth_long[comp]


def build(paths: CorpusPaths) -> None:
    t0 = time.time()
    d = paths.cache_dir
    onts = pd.read_parquet(d / "entity_ontologies.parquet")
    terms = pd.read_parquet(d / "entity_terms.parquet")
    edges = pd.read_parquet(d / "entity_edges.parquet")
    docs = pd.read_parquet(d / "entity_docs.parquet")
    n_onto, n_terms = len(onts), len(terms)
    term_home = terms.home.to_numpy()
    flags = docs["flags"].to_numpy()
    print(
        f"{n_onto} ontologies, {n_terms:,} terms, {len(edges):,} edges, {len(docs):,} term-document rows"
    )

    # --- G: the corpus-wide subsumption DAG ---------------------------------
    kind = edges.kind.to_numpy()
    in_g = (kind != EDGE_COPY) | (term_home[edges.child.to_numpy()] == EXTERNAL)
    g_edges = edges.loc[in_g, ["child", "parent"]].drop_duplicates()
    g_nodes = np.unique(np.concatenate([g_edges.child.to_numpy(), g_edges.parent.to_numpy()]))
    local_id = np.full(n_terms, -1, dtype=np.int64)
    local_id[g_nodes] = np.arange(len(g_nodes))
    cl = dag_closure(
        len(g_nodes), local_id[g_edges.child.to_numpy()], local_id[g_edges.parent.to_numpy()]
    )
    print(
        f"G: {len(g_nodes):,} nodes, {len(g_edges):,} edges, {int(cl.cyclic.sum()):,} in cycles ({time.time() - t0:.0f}s)"
    )

    ent = pd.DataFrame({"term": g_nodes})
    ent["depth_global_long"] = cl.depth_long
    ent["depth_global_short"] = cl.depth_short
    ent["n_desc"] = cl.n_desc
    ent["n_leaves"] = cl.n_leaves
    ent["n_anc"] = cl.n_anc
    ent["cyclic"] = cl.cyclic
    ent["ic_seco"] = ic_seco(cl.n_desc, len(g_nodes))
    ent["ic_sanchez"] = ic_sanchez(cl.n_leaves, cl.n_anc)
    ent["generality"] = 1.0 - ent.ic_seco

    # --- sense C and popularity from the term-document table -------------------
    term = docs.term.to_numpy()
    onto = docs.onto.to_numpy()
    foreign = term_home[term] != onto
    usage = pd.DataFrame(
        {
            "term": term,
            "df_class": (flags & F_CLASS) > 0,
            "df_inst": (flags & F_INST) > 0,
            "df_punned": ((flags & F_CLASS) > 0) & ((flags & F_INST) > 0),
            "df_decl_foreign": ((flags & F_DECL) > 0) & foreign,
            "df_ref_foreign": foreign,
            "is_property": (flags & F_DECL_PROP) > 0,
        }
    )
    usage = usage.groupby("term").sum()
    usage["role_share"] = role_share(usage.df_class.to_numpy(), usage.df_inst.to_numpy())

    # --- sense G, distributional: who specialises the term ---------------------
    comm = _communities(docs, term_home, n_onto)
    of = edges[kind == EDGE_OWN_FOREIGN]
    spec = of[["parent", "onto"]].drop_duplicates()
    spec = spec.assign(
        comm=comm[spec.onto.to_numpy()], reg=onts.registry.to_numpy()[spec.onto.to_numpy()]
    )
    df_spec = spec.groupby("parent").onto.nunique().rename("df_spec")
    div = (
        spec.groupby("parent")
        .comm.apply(lambda c: effective_number(np.bincount(c.to_numpy())))
        .rename("div_comm")
    )
    div_reg = spec.groupby("parent").reg.nunique().rename("n_registries_spec")
    print(
        f"reuse: {len(df_spec):,} terms specialised by a foreign ontology; {comm.max() + 1} communities"
    )

    # --- local hierarchies: each ontology's own view ------------------------
    local_rows = []
    onto_rows = []
    edges_by_onto = dict(tuple(edges.groupby("onto")))
    docs_by_onto = dict(tuple(docs.groupby("onto")))
    for o in range(n_onto):
        e = edges_by_onto.get(o)
        dd = docs_by_onto.get(o)
        home_terms = (
            dd.loc[
                (term_home[dd.term.to_numpy()] == o) & ((dd["flags"].to_numpy() & F_CLASS) > 0),
                "term",
            ].to_numpy()
            if dd is not None
            else np.empty(0, dtype=np.int64)
        )
        n_nodes = max_depth = 0
        gen_local = np.zeros(len(home_terms))
        if e is not None and len(e):
            nodes = np.unique(np.concatenate([e.child.to_numpy(), e.parent.to_numpy()]))
            lid = pd.Series(np.arange(len(nodes)), index=nodes)
            lc = dag_closure(
                len(nodes), lid[e.child.to_numpy()].to_numpy(), lid[e.parent.to_numpy()].to_numpy()
            )
            n_nodes, max_depth = len(nodes), int(lc.depth_long.max())
            ic_local = pd.Series(ic_seco(lc.n_desc, len(nodes)), index=nodes)
            depth_local = pd.Series(lc.depth_long, index=nodes)
            present = np.isin(home_terms, nodes)
            gen_local[present] = 1.0 - ic_local[home_terms[present]].to_numpy()
            if len(home_terms):
                local_rows.append(
                    pd.DataFrame(
                        {
                            "term": home_terms[present],
                            "depth_local": depth_local[home_terms[present]].to_numpy(),
                            "generality_local": gen_local[present],
                        }
                    )
                )
        onto_rows.append(
            {
                "onto": o,
                "n_home_classes": len(home_terms),
                "hierarchy_nodes": n_nodes,
                "max_depth_local": max_depth,
                "A_local": float(gen_local.mean()) if len(gen_local) else np.nan,
            }
        )
    local = (
        pd.concat(local_rows, ignore_index=True).drop_duplicates("term")
        if local_rows
        else pd.DataFrame()
    )
    per_onto = pd.DataFrame(onto_rows)
    print(f"local hierarchies done ({time.time() - t0:.0f}s)")

    # --- assemble the entity table ------------------------------------------
    ent = terms[["term", "iri", "home"]].merge(ent, on="term", how="left")
    ent = ent.merge(usage, left_on="term", right_index=True, how="left")
    ent = ent.merge(df_spec, left_on="term", right_index=True, how="left")
    ent = ent.merge(div, left_on="term", right_index=True, how="left")
    ent = ent.merge(div_reg, left_on="term", right_index=True, how="left")
    if len(local):
        ent = ent.merge(local, on="term", how="left")
    for col in (
        "df_spec",
        "n_registries_spec",
        "df_class",
        "df_inst",
        "df_punned",
        "df_decl_foreign",
        "df_ref_foreign",
    ):
        ent[col] = ent[col].fillna(0).astype(np.int64)
    ent["in_g"] = ent.n_desc.notna()
    out = d / "entity_abstractness.parquet"
    ent.to_parquet(out, index=False)
    print(f"Wrote {out}  ({len(ent):,} terms)")

    # --- ontology level --------------------------------------------------------
    home_cls = ent[(ent.home >= 0) & (ent.df_class > 0) & (ent.is_property == 0)]
    agg = home_cls.groupby("home").agg(
        A_global=("generality", "mean"),
        A_class=("role_share", "mean"),
        upper_share=("df_spec", lambda s: float((s > 0).mean())),
        punned_share=("df_punned", lambda s: float((s > 0).mean())),
        max_df_spec=("df_spec", "max"),
    )
    own_foreign = of.assign(phome=term_home[of.parent.to_numpy()])
    ce = own_foreign[own_foreign.phome != EXTERNAL].groupby("onto").phome.nunique().rename("Ce")
    ce_ext = (
        own_foreign[own_foreign.phome == EXTERNAL]
        .assign(ns=lambda x: terms.iri.to_numpy()[x.parent.to_numpy()])
        .groupby("onto")
        .parent.nunique()
        .rename("n_external_parents")
    )
    ca = own_foreign[own_foreign.phome != EXTERNAL].groupby("phome").onto.nunique().rename("Ca")
    bridges = edges[kind == EDGE_BRIDGE].groupby("onto").size().rename("n_bridge_edges")
    copies = edges[kind == EDGE_COPY].groupby("onto").size().rename("n_copy_edges")
    own = (
        edges[(kind == EDGE_OWN_INTERNAL) | (kind == EDGE_OWN_FOREIGN)]
        .groupby("onto")
        .size()
        .rename("n_own_edges")
    )

    dep = own_foreign[own_foreign.phome != EXTERNAL][["onto", "phome"]].drop_duplicates()
    layer = _layers(n_onto, dep.onto.to_numpy(), dep.phome.to_numpy())

    res = onts.merge(per_onto, on="onto").set_index("onto")
    for s in (agg, ce, ce_ext, ca, bridges, copies, own):
        res = res.join(s)
    for col in ("Ca", "Ce", "n_external_parents", "n_bridge_edges", "n_copy_edges", "n_own_edges"):
        res[col] = res[col].fillna(0).astype(np.int64)
    res["layer"] = layer
    res["community"] = comm
    res["I"] = martin_instability(res.Ca.to_numpy(), res.Ce.to_numpy())
    res["D"] = main_sequence_distance(res.A_local.to_numpy(), res.I.to_numpy())
    res["D_global"] = main_sequence_distance(res.A_global.to_numpy(), res.I.to_numpy())
    ind = kb_individuals(res.individual_count, res.get("skos_concept_count"))
    cls = res.class_count.fillna(0)
    res["kb_like"] = ind > cls
    res["flat"] = (res.hierarchy_nodes < MIN_HIERARCHY_NODES) | (
        res.max_depth_local < MIN_MAX_DEPTH
    )
    res["subject"] = ~res.kb_like & ~res.flat
    res = res.reset_index()
    out_path = paths.ontology_abstractness_path
    res.to_parquet(out_path, index=False)
    print(
        f"Wrote {out_path}  ({len(res)} ontologies; {int(res.subject.sum())} measured, "
        f"{int(res.kb_like.sum())} KB-like, {int(res.flat.sum())} flat)  ({time.time() - t0:.0f}s)"
    )


@click.command()
@corpus_paths
def main(paths: CorpusPaths) -> None:
    """Term- and ontology-level abstractness from the entity network."""
    paths.require_store_dir()
    build(paths)


if __name__ == "__main__":
    main()
