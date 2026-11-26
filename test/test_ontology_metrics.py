"""The per-ontology metric table's rules, on a hand-built network and frame."""

import math

import networkx as nx
import numpy as np
import pandas as pd
import pytest

from ontometer_corpus.ontology_metrics import (
    NetworkViews,
    add_derived_metrics,
    attach_idf,
    drop_skos_dominated,
    external_ratio,
    logical_view,
    network_measures,
    structural_population,
)


def _graph() -> nx.MultiDiGraph:
    """a imports b and uses it logically; c uses b only in metadata; a uses ext; d is a thesaurus."""
    g = nx.MultiDiGraph()
    for n, fh in [("a", "fa"), ("b", "fb"), ("c", "fc"), ("d", "fd")]:
        g.add_node(n, is_external=False, file_hash=fh, home_registry="LOV")
    g.add_node("ext", is_external=True)
    g.add_node("only_d", is_external=True)
    g.add_edge("a", "b", key="import", edge_type="import")
    usage = {"edge_type": "usage", "reference_kind": "domain_vocabulary"}
    g.add_edge("a", "b", key="usage", n_logical_terms=2, reference_iris=["http://b/"], **usage)
    g.add_edge("c", "b", key="usage", n_logical_terms=0, reference_iris=["http://b/"], **usage)
    g.add_edge("a", "ext", key="usage", n_logical_terms=1, reference_iris=["http://ext/"], **usage)
    g.add_edge("d", "only_d", key="usage", n_logical_terms=1, **usage)
    return g


def _views() -> NetworkViews:
    g_all = _graph()
    files = pd.DataFrame(
        {
            "file_hash": ["fa", "fb", "fc", "fd"],
            "kind": ["ontology", "ontology", "ontology", "thesaurus"],
            "ontology_iri": ["http://a/", "http://b/", None, "http://d/"],
        }
    )
    dropped = drop_skos_dominated(g_all, files)
    corpus = [n for n, d in g_all.nodes(data=True) if not d["is_external"]]
    g = logical_view(g_all)
    attach_idf(g, len(corpus))
    return NetworkViews(
        g_all=g_all, g=g, corpus_nodes=corpus, no_iri_nodes={"c"}, skos_dropped=dropped
    )


def test_skos_dominated_sources_and_their_only_targets_are_dropped():
    views = _views()
    assert views.skos_dropped == 1
    assert "d" not in views.g_all and "only_d" not in views.g_all


def test_logical_view_keeps_imports_and_logical_usage_only():
    g = _views().g
    assert sorted(g.edges("a", keys=True)) == [
        ("a", "b", "import"),
        ("a", "b", "usage"),
        ("a", "ext", "usage"),
    ]
    assert g.out_degree("c") == 0


def test_idf_counts_distinct_referencing_ontologies():
    g = _views().g
    # b is referenced by one ontology (a), through two edges: df = 1, not 2.
    assert g["a"]["b"]["import"]["idf_weight"] == pytest.approx(math.log(3 / 1))


def test_external_ratio_undefined_without_iri_or_used_vocabulary():
    views = _views()
    ns = {"fa": {"http://b/", "http://ext/", "http://own-only/"}, "fc": {"http://b/"}}
    assert external_ratio("a", views.g, ns, views.no_iri_nodes) == pytest.approx(2 / 3)
    assert math.isnan(external_ratio("c", views.g, ns, views.no_iri_nodes))
    assert math.isnan(external_ratio("b", views.g, ns, views.no_iri_nodes))


def test_network_measures_leave_distinctiveness_undefined_without_out_edges():
    m = network_measures(_views(), {"fa": {"http://b/"}})
    assert m.loc["a", "out_degree"] == 3 and m.loc["b", "in_degree"] == 2
    assert np.isnan(m.loc["b", "reuse_distinctiveness"])
    assert np.isnan(m.loc["b", "external_dependency_ratio"])
    assert m.loc["a", "reuse_distinctiveness"] == pytest.approx(
        np.mean([math.log(3), math.log(3), math.log(3)])
    )


def _frame(**overrides) -> pd.DataFrame:
    row = {
        "ontology_iri": "http://x/",
        "class_count": 4,
        "property_count": 2,
        "individual_count": 6,
        "skos_concept_count": 2,
        "triple_count": 120,
        "struct_ok": True,
        "n_nodes": 4,
        "n_cyclic": 0,
        "branching": 1.5,
        "coupling_ok": True,
        "n_terms_domain": 10,
        "n_ext_domain": 5,
        "n_terms_logical": 4,
        "n_ext_logical": 1,
        "n_restrictions": 2,
        "n_logical_statements": 30,
        "depth_hist": None,
        "kind": "ontology",
    }
    row.update(overrides)
    return add_derived_metrics(pd.DataFrame([row]))


def test_schema_density_counts_skos_concepts_on_the_schema_side():
    df = _frame()
    assert df.loc[0, "schema_density"] == pytest.approx((4 + 2 + 2) / 12)
    assert df.loc[0, "instance_count"] == 4


def test_coupling_is_zero_without_domain_terms_and_undefined_without_parse_or_iri():
    assert _frame(n_terms_domain=0, n_ext_domain=0).loc[0, "coupling_ratio"] == 0.0
    assert np.isnan(_frame(coupling_ok=False).loc[0, "coupling_ratio"])
    assert np.isnan(_frame(coupling_ok=False).loc[0, "restriction_density"])
    no_iri = _frame(ontology_iri=None)
    assert (
        no_iri[["coupling_ratio", "coupling_ratio_logical", "reuse_intensity"]]
        .isna()
        .all(axis=None)
    )


def test_flat_ontology_has_zero_branching_and_cycle_rate():
    df = _frame(n_nodes=0, branching=np.nan)
    assert df.loc[0, "branching"] == 0.0 and df.loc[0, "cycle_rate"] == 0.0


def test_depth_skew_needs_a_large_enough_hierarchy():
    assert np.isnan(_frame(depth_hist=[1, 2, 1]).loc[0, "depth_skew"])
    big = _frame(n_nodes=60, depth_hist=[10, 20, 30])
    assert big.loc[0, "depth_skew"] < 0


def test_structural_population_leaves_out_skos_dominated_sources():
    df = pd.DataFrame({"kind": ["ontology", "thesaurus", "mostly_skos"]})
    assert list(structural_population(df)["kind"]) == ["ontology"]
