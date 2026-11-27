"""Per-ontology metric table: structure, coupling and reuse-network position.

One row per latest, parsed ontology (index ``node``, the network's node id),
joining what the corpus tables measure separately:

``corpus.db``
    counts per ontology (triples, classes, properties, individuals, SKOS
    concepts), ``kind`` and ``home_registry``.
``structural_metrics.parquet``, ``term_coupling.parquet``
    hierarchy shape, term-level coupling and restrictions, each keyed by raw-byte
    ``file_hash``.
``network_nodes`` / ``network_edges``
    the dependency network, read in its logical-reuse view (:func:`logical_view`)
    with SKOS-dominated sources removed (:func:`load_views`).

Derived columns follow fixed rules: a ratio is undefined (NaN) where its
denominator is, except that a value which is genuinely zero is kept as zero
(a flat ontology's branching factor, the coupling of an ontology that borrows
no domain term). Own-vs-external measures are undefined for an ontology that
declares no ontology IRI: most own no namespace, so every term would count as
external. Network columns are NaN for SKOS-dominated rows, which are not in the
network view; :func:`structural_population` drops those rows.
"""

from __future__ import annotations

from ontometer_corpus._extras import require

require("tables")


import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

import networkx as nx
import numpy as np
import pandas as pd

from ontometer_corpus.namespaces import reference_kind
from ontometer_corpus.network.construction import load_network
from ontometer_corpus.paths import CorpusPaths
from ontometer_corpus.release import require_readable
from ontometer_corpus.role_names import SKOS_DOMINATED_KINDS

#: Display name per metric column, for tables and figures.
METRIC_LABELS: dict[str, str] = {
    "triple_count": "Triple count",
    "class_count": "Class count",
    "property_count": "Property count",
    "instance_count": "Individual count",
    "entity_count": "Entity count",
    "n_nodes": "Hierarchy size",
    "schema_density": "Schema density",
    "axiom_density_triples": "Axiom density (triples)",
    "axiom_density_logical": "Axiom density (logical statements)",
    "mean_depth_long": "Mean depth (longest)",
    "mean_depth_short": "Mean depth (shortest)",
    "depth_skew": "Depth skew",
    "branching": "Branching factor",
    "orphan_rate": "Orphan-class rate",
    "cycle_rate": "Cycle rate",
    "multiple_inheritance_rate": "Multiple-inheritance rate",
    "restriction_density": "Restriction density",
    "coupling_ratio": "Coupling ratio",
    "coupling_ratio_logical": "Coupling ratio (logical use)",
    "reuse_intensity": "External-term reuse intensity",
    "reuse_distinctiveness": "Reuse distinctiveness (mean IDF)",
    "in_degree": "In-degree",
    "out_degree": "Out-degree",
    "idf_out_degree": "IDF-weighted out-degree",
    "idf_in_degree": "IDF-weighted in-degree",
    "betweenness": "Betweenness",
    "pagerank": "PageRank",
    "external_dependency_ratio": "External dependency ratio",
}

#: The per-ontology metrics the corpus derives, for describing ontologies.
DESCRIPTIVE_METRICS: tuple[str, ...] = (
    "triple_count",
    "class_count",
    "property_count",
    "instance_count",
    "entity_count",
    "schema_density",
    "axiom_density_triples",
    "axiom_density_logical",
    "coupling_ratio",
    "n_nodes",
    "mean_depth_long",
    "mean_depth_short",
    "branching",
    "depth_skew",
    "cycle_rate",
    "multiple_inheritance_rate",
    "orphan_rate",
    "restriction_density",
    "reuse_intensity",
)

#: Reuse-network measures per ontology, from the logical-reuse view.
NETWORK_METRICS: tuple[str, ...] = (
    "in_degree",
    "out_degree",
    "idf_out_degree",
    "betweenness",
    "pagerank",
    "external_dependency_ratio",
)

#: Decimal places kept for betweenness and PageRank.
CENTRALITY_DIGITS = 12

#: Depth skew is only defined on a hierarchy of at least this many classes.
DEPTH_SKEW_MIN_NODES = 50

_COUPLING_COLUMNS = [
    "file_hash",
    "coupling_ok",
    "n_terms_all",
    "n_ext_all",
    "n_terms_domain",
    "n_ext_domain",
    "n_terms_logical",
    "n_ext_logical",
    "n_logical_statements",
    "n_restrictions",
]


# ---------------------------------------------------------------- network views
@dataclass
class NetworkViews:
    """The dependency network as the metrics read it.

    ``g_all`` keeps every usage edge; ``g`` is the logical-reuse view. Both
    have SKOS-dominated sources removed, and external targets only those
    referenced. Every edge of both carries ``idf_weight``.
    """

    g_all: nx.MultiDiGraph
    g: nx.MultiDiGraph
    corpus_nodes: list[str]
    no_iri_nodes: set[str]
    skos_dropped: int

    @property
    def n_corpus(self) -> int:
        return len(self.corpus_nodes)


def latest_files(paths: CorpusPaths) -> pd.DataFrame:
    """``file_hash``, ``kind`` and ``ontology_iri`` of every latest, parsed file."""
    conn = sqlite3.connect(paths.db_path)
    try:
        return pd.read_sql_query(
            "SELECT file_hash, kind, ontology_iri FROM corpus_registry "
            "WHERE is_latest_version = 1 AND parse_status = 'ok'",
            conn,
        ).drop_duplicates("file_hash")
    finally:
        conn.close()


def _drop_orphan_externals(g: nx.MultiDiGraph) -> None:
    g.remove_nodes_from([n for n in list(g) if g.nodes[n].get("is_external") and g.degree(n) == 0])


def drop_skos_dominated(g: nx.MultiDiGraph, files: pd.DataFrame) -> int:
    """Remove SKOS-dominated corpus sources, and externals left unreferenced; returns how many."""
    kind_by_file = dict(zip(files["file_hash"], files["kind"]))
    skos = [
        n
        for n, d in g.nodes(data=True)
        if not d.get("is_external", False)
        and kind_by_file.get(d.get("file_hash")) in SKOS_DOMINATED_KINDS
    ]
    g.remove_nodes_from(skos)
    _drop_orphan_externals(g)
    return len(skos)


def is_logical_edge(d: dict) -> bool:
    """An import, or a usage edge with at least one logically used domain-vocabulary term."""
    if d["edge_type"] == "import":
        return True
    return d.get("reference_kind") == "domain_vocabulary" and (d.get("n_logical_terms") or 0) > 0


def logical_view(g_all: nx.MultiDiGraph) -> nx.MultiDiGraph:
    """The logical-reuse view of ``g_all``: every node, logical edges only, orphan externals dropped."""
    g = nx.MultiDiGraph()
    g.add_nodes_from(g_all.nodes(data=True))
    g.add_edges_from(
        (u, v, k, dict(d)) for u, v, k, d in g_all.edges(keys=True, data=True) if is_logical_edge(d)
    )
    _drop_orphan_externals(g)
    return g


def attach_idf(g: nx.MultiDiGraph, n_corpus: int) -> None:
    """Set ``idf_weight = log(n_corpus / df(target))`` on every edge.

    ``df`` counts the distinct ontologies referencing the target, so an import
    and a usage edge between one pair count once. Near 0 for a target almost
    every ontology cites, largest for one only a single ontology cites.
    """
    df = {v: len({u for u, _ in g.in_edges(v)}) for v in g.nodes}
    for u, v, k in g.edges(keys=True):
        g[u][v][k]["idf_weight"] = math.log(n_corpus / df[v]) if df[v] else 0.0


def load_views(paths: CorpusPaths, files: pd.DataFrame | None = None) -> NetworkViews:
    """Load the network, drop SKOS-dominated sources, and build both IDF-weighted views."""
    files = latest_files(paths) if files is None else files
    g_all = load_network(paths)
    dropped = drop_skos_dominated(g_all, files)
    corpus = [n for n, d in g_all.nodes(data=True) if not d.get("is_external", False)]
    no_iri = set(files.loc[files["ontology_iri"].isna(), "file_hash"])
    g = logical_view(g_all)
    attach_idf(g, len(corpus))
    attach_idf(g_all, len(corpus))
    return NetworkViews(
        g_all=g_all,
        g=g,
        corpus_nodes=corpus,
        no_iri_nodes={n for n in corpus if g_all.nodes[n].get("file_hash") in no_iri},
        skos_dropped=dropped,
    )


# ---------------------------------------------------------------- network measures
def logical_namespaces(paths: CorpusPaths) -> dict[str, set[str]]:
    """Per file, the domain vocabularies it uses logically (at least one logically used term)."""
    ns = pd.read_parquet(paths.namespace_usage_path)
    ns = ns[ns["namespace"].map(reference_kind) == "domain_vocabulary"]
    return ns[ns["n_logical_terms"] > 0].groupby("file_hash")["namespace"].agg(set).to_dict()


def external_ratio(
    n: str, g: nx.MultiDiGraph, ns_by_file: dict[str, set[str]], no_iri_nodes: set[str]
) -> float:
    """Share of the vocabularies ``n`` uses (``ns_by_file``) that ``g`` resolves to another ontology.

    NaN for an ontology without an ontology IRI, or that uses no vocabulary.
    """
    if n in no_iri_nodes or n not in g:
        return np.nan
    used = ns_by_file.get(g.nodes[n].get("file_hash"))
    if not used:
        return np.nan
    external: set[str] = set()
    for _, _, d in g.out_edges(n, data=True):
        if d["edge_type"] == "usage":
            external.update(d.get("reference_iris") or [d.get("reference_iri")])
    return len(used & external) / len(used)


def network_measures(views: NetworkViews, ns_by_file: dict[str, set[str]]) -> pd.DataFrame:
    """Per corpus ontology, its position in the logical-reuse view.

    Degrees count edges of the multigraph (an import and a usage edge between
    one pair count twice), out to external targets too. Betweenness runs on
    the corpus subgraph, PageRank on it collapsed to one link per pair.
    ``reuse_distinctiveness`` is the mean IDF of all out-edges and
    ``external_dependency_ratio`` the breadth of reuse; both are NaN for an
    ontology with no out-edge (0 would claim it reuses only generic targets).
    """
    g, corpus = views.g, views.corpus_nodes
    in_deg = dict(g.in_degree())
    out_deg = dict(g.out_degree())
    idf_out: dict[str, float] = defaultdict(float)
    idf_in: dict[str, float] = defaultdict(float)
    out_weights: dict[str, list[float]] = defaultdict(list)
    for u, v, _, d in g.edges(keys=True, data=True):
        idf_out[u] += d["idf_weight"]
        idf_in[v] += d["idf_weight"]
        out_weights[u].append(d["idf_weight"])

    h = g.subgraph(corpus)
    betweenness = nx.betweenness_centrality(h)
    hp = nx.DiGraph()
    hp.add_nodes_from(h.nodes())
    hp.add_edges_from({(u, v) for u, v in h.edges()})
    pagerank = nx.pagerank(hp)

    distinct = {n: float(np.mean(out_weights[n])) for n in corpus if out_weights.get(n)}
    return pd.DataFrame(
        {
            "in_degree": [in_deg.get(n, 0) for n in corpus],
            "out_degree": [out_deg.get(n, 0) for n in corpus],
            "idf_out_degree": [idf_out.get(n, 0.0) for n in corpus],
            "idf_in_degree": [idf_in.get(n, 0.0) for n in corpus],
            # Rounded: both sum floats in graph-iteration order, which differs between
            # processes at ~1e-16 and would otherwise reorder ties in rank statistics.
            "betweenness": [round(betweenness[n], CENTRALITY_DIGITS) for n in corpus],
            "pagerank": [round(pagerank[n], CENTRALITY_DIGITS) for n in corpus],
            "reuse_distinctiveness": [distinct.get(n, np.nan) for n in corpus],
            "external_dependency_ratio": [
                external_ratio(n, g, ns_by_file, views.no_iri_nodes) if n in distinct else np.nan
                for n in corpus
            ],
        },
        index=pd.Index(corpus, name="node"),
    )


# ---------------------------------------------------------------- the table
def _depth_skew(n_nodes: float, depth_hist) -> float:
    """Skewness of a depth histogram (per-level node counts); NaN below ``DEPTH_SKEW_MIN_NODES``."""
    if not n_nodes >= DEPTH_SKEW_MIN_NODES or not isinstance(depth_hist, (list, np.ndarray)):
        return np.nan
    counts = np.asarray(depth_hist, dtype=float)
    if counts.sum() == 0:
        return np.nan
    levels = np.arange(len(counts))
    n = counts.sum()
    mean = (levels * counts).sum() / n
    std = np.sqrt((counts * (levels - mean) ** 2).sum() / n)
    if std == 0:
        return 0.0
    return float((counts * (levels - mean) ** 3).sum() / n / std**3)


def _latest_rows(paths: CorpusPaths) -> pd.DataFrame:
    conn = sqlite3.connect(paths.db_path)
    try:
        rows = pd.read_sql_query(
            """
            SELECT id, ontology_iri, source_registry, home_registry, file_hash, content_hash,
                   class_count, property_count, individual_count, skos_concept_count,
                   triple_count, shared_iri, kind
            FROM corpus_registry
            WHERE is_latest_version = 1 AND parse_status = 'ok'
            """,
            conn,
        )
    finally:
        conn.close()
    # The network's node id: the ontology IRI, or row:<id> where none is
    # declared or dedup found it shared by distinct ontologies. An IRI kept
    # latest in several registries keeps the row with the largest id, as the
    # network builder does.
    rows["node"] = [
        iri if pd.notna(iri) and not shared else f"row:{rid}"
        for iri, shared, rid in zip(rows["ontology_iri"], rows["shared_iri"], rows["id"])
    ]
    return rows.sort_values("id").drop_duplicates(subset="node", keep="last")


def add_derived_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Add the derived columns to a frame of raw counts and joined table columns (in place)."""
    skos = df["skos_concept_count"].fillna(0)
    df["entity_count"] = df[["class_count", "property_count", "individual_count"]].sum(axis=1)
    # A SKOS concept is a vocabulary term, so it counts on the schema side;
    # instance_count is what makes a source knowledge-base-like.
    df["instance_count"] = df["individual_count"] - skos
    entities = df["entity_count"].replace(0, np.nan)
    df["schema_density"] = (df["class_count"] + df["property_count"] + skos) / entities
    df["axiom_density_triples"] = df["triple_count"] / entities
    df["cycle_rate"] = df["n_cyclic"] / df["n_nodes"].replace(0, np.nan)
    flat = (df["struct_ok"] == True) & (df["n_nodes"] == 0)  # noqa: E712
    df.loc[flat, ["branching", "cycle_rate"]] = 0.0

    parsed = df["coupling_ok"] == True  # noqa: E712
    df["coupling_ratio"] = df["n_ext_domain"] / df["n_terms_domain"].replace(0, np.nan)
    df["coupling_ratio_logical"] = df["n_ext_logical"] / df["n_terms_logical"].replace(0, np.nan)
    for ratio, n_terms in [
        ("coupling_ratio", "n_terms_domain"),
        ("coupling_ratio_logical", "n_terms_logical"),
    ]:
        df.loc[~parsed, ratio] = np.nan
        df.loc[parsed & (df[n_terms] == 0), ratio] = 0.0
    df["restriction_density"] = df["n_restrictions"] / df["class_count"].replace(0, np.nan)
    df.loc[~parsed, "restriction_density"] = np.nan
    # Distinct external domain terms per local schema entity: the magnitude
    # the coupling ratio, a share, leaves out.
    schema_entities = (df["class_count"] + df["property_count"]).replace(0, np.nan)
    df["reuse_intensity"] = df["n_ext_domain"] / schema_entities
    df.loc[~parsed, "reuse_intensity"] = np.nan
    df.loc[
        df["ontology_iri"].isna(), ["coupling_ratio", "coupling_ratio_logical", "reuse_intensity"]
    ] = np.nan
    df["axiom_density_logical"] = df["n_logical_statements"] / entities
    df["depth_skew"] = [_depth_skew(n, h) for n, h in zip(df["n_nodes"], df["depth_hist"])]
    return df


def build_ontology_metrics(paths: CorpusPaths, views: NetworkViews | None = None) -> pd.DataFrame:
    """The per-ontology metric table (see the module docstring), indexed by ``node``."""
    views = load_views(paths) if views is None else views

    df = _latest_rows(paths)
    struct = pd.read_parquet(paths.structural_metrics_path).rename(columns={"ok": "struct_ok"})
    coupling = pd.read_parquet(paths.term_coupling_path).rename(columns={"ok": "coupling_ok"})
    df = df.merge(struct, on="file_hash", how="left")
    df = df.merge(coupling[_COUPLING_COLUMNS], on="file_hash", how="left")
    df = add_derived_metrics(df).set_index("node")
    if not df.index.is_unique:
        raise ValueError("node ids are not unique in the latest, parsed population")

    network = network_measures(views, logical_namespaces(paths))
    return df.join(network, how="left")


def structural_population(df: pd.DataFrame) -> pd.DataFrame:
    """The rows the structural analyses use: SKOS-dominated sources left out."""
    return df[~df["kind"].isin(SKOS_DOMINATED_KINDS)].copy()


def load_ontology_metrics(paths: CorpusPaths) -> pd.DataFrame:
    """Read ``ontology_metrics.parquet`` (written by ``ontometer-corpus ontology-metrics``).

    Raises:
        ReleaseError: the corpus directory holds tables of another format.
    """
    require_readable(paths.corpus_dir)
    path = paths.ontology_metrics_path
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing: run ontometer-corpus ontology-metrics")
    return pd.read_parquet(path)
