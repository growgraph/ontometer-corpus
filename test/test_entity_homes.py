"""Who owns a term, and which asserted edges count as reuse."""

from __future__ import annotations

import numpy as np

from ontometer_corpus.network.entities import (
    EDGE_BRIDGE,
    EDGE_COPY,
    EDGE_OWN_FOREIGN,
    EDGE_OWN_INTERNAL,
    EXTERNAL,
    Claimant,
    NamespaceOwnerRecord,
    classify_edges,
    iri_names_namespace,
    pick_home,
    resolve_namespace_owners,
)

GO_NS = "http://purl.obolibrary.org/obo/GO_"


def test_obo_prefix_owned_by_matching_ontology_iri():
    assert iri_names_namespace("http://purl.obolibrary.org/obo/go.owl", GO_NS)
    assert not iri_names_namespace("http://purl.obolibrary.org/obo/uberon.owl", GO_NS)


def test_hash_and_slash_namespaces_match_their_ontology_iri():
    assert iri_names_namespace("http://xmlns.com/foaf/0.1", "http://xmlns.com/foaf/0.1/")
    assert iri_names_namespace("http://example.org/zoo", "http://example.org/zoo#")


def test_copier_declaring_more_terms_loses_to_the_named_owner():
    # A BioPortal file bakes in thousands of GO terms; GO itself declares fewer
    # in this toy but its IRI names the namespace.
    copier = Claimant(
        onto=0, registry="BioPortal", ontology_iri="http://example.org/app.owl", n_declared=5000
    )
    owner = Claimant(
        onto=1, registry="OBO", ontology_iri="http://purl.obolibrary.org/obo/go.owl", n_declared=10
    )
    assert pick_home(GO_NS, [copier, owner]) == 1


def test_dominant_skos_scheme_beats_a_smaller_named_vocabulary():
    ns = "http://aims.fao.org/aos/agrovoc/"
    scheme = Claimant(0, "AgroPortal", "http://aims.fao.org/aos/agrontology", 41841, 41841)
    named_vocabulary = Claimant(1, "AgroPortal", ns, 0, 310)
    assert pick_home(ns, [scheme, named_vocabulary]) == 0


def test_skos_copier_cannot_displace_a_named_owner_that_declares_terms():
    ro_ns = "http://purl.obolibrary.org/obo/RO_"
    owner = Claimant(0, "OBO", "http://purl.obolibrary.org/obo/ro.owl", 100, 0)
    copier = Claimant(1, "AgroPortal", "https://example.org/thesaurus", 2, 1785)
    assert pick_home(ro_ns, [owner, copier]) == 0


def test_metadata_namespace_is_never_owned_by_a_corpus_ontology():
    licence = "https://creativecommons.org/licenses/by/3.0/"
    claimant = Claimant(0, "BioPortal", licence, 1000, 0)
    assert pick_home(licence, [claimant]) == EXTERNAL


def test_dominant_declarer_owns_a_namespace_no_iri_names():
    # GAZ's file declares no IRI; a copier holds a thousand of its terms.
    gaz_ns = "http://purl.obolibrary.org/obo/GAZ_"
    copier = Claimant(onto=0, registry="OBO", ontology_iri=None, n_declared=1069)
    gaz = Claimant(onto=1, registry="Ontohub", ontology_iri=None, n_declared=664813)
    assert pick_home(gaz_ns, [copier, gaz]) == 1


def test_contested_namespace_without_a_dominant_declarer_is_external():
    orcid = "https://orcid.org/"
    a = Claimant(
        onto=0,
        registry="OBO",
        ontology_iri="http://purl.obolibrary.org/obo/uberon.owl",
        n_declared=98,
    )
    b = Claimant(
        onto=1, registry="OBO", ontology_iri="http://purl.obolibrary.org/obo/ro.owl", n_declared=78
    )
    assert pick_home(orcid, [a, b]) == EXTERNAL


def test_obo_subnamespace_belongs_to_its_ontology():
    ro = "http://purl.obolibrary.org/obo/ro.owl"
    assert iri_names_namespace(ro, "http://purl.obolibrary.org/obo/ro/subsets#")
    assert iri_names_namespace(
        "http://purl.obolibrary.org/obo/ncbitaxon.owl", "http://purl.obolibrary.org/obo/ncbitaxon#"
    )
    assert not iri_names_namespace(ro, "http://purl.obolibrary.org/obo/uberon/core#")
    vbo = Claimant(
        onto=0, registry="OBO", ontology_iri="http://purl.obolibrary.org/obo/vbo.owl", n_declared=3
    )
    owner = Claimant(onto=1, registry="OBO", ontology_iri=ro, n_declared=3)
    assert pick_home("http://purl.obolibrary.org/obo/ro/subsets#", [vbo, owner]) == 1


def test_unclaimed_namespace_is_external():
    assert pick_home("http://nowhere.example/ns#", []) == EXTERNAL


def test_edge_authority():
    onto = 7
    home_child = np.array([7, 7, 3, 3, EXTERNAL])
    home_parent = np.array([7, 2, 7, 2, EXTERNAL])
    kinds = classify_edges(onto, home_child, home_parent)
    assert kinds.tolist() == [
        EDGE_OWN_INTERNAL,
        EDGE_OWN_FOREIGN,
        EDGE_BRIDGE,
        EDGE_COPY,
        EDGE_COPY,
    ]


def test_missing_iri_from_pandas_names_nothing():
    assert not iri_names_namespace(float("nan"), GO_NS)


def _record(owner: int, declarations: dict[str, int]) -> NamespaceOwnerRecord:
    return NamespaceOwnerRecord(
        owner=owner,
        ontology_iri=None,
        source_registry="LOV",
        own_namespaces=tuple(declarations),
        declarations=declarations,
        skos_concepts={},
    )


def test_two_spellings_of_one_namespace_do_not_make_an_ontology_its_own_rival():
    record = _record(7, {"http://x.org/a#": 100, "http://x.org/a/": 50})
    assert resolve_namespace_owners([record])["http://x.org/a"] == 7


def test_a_real_rival_still_blocks_ownership_without_dominance():
    owners = resolve_namespace_owners(
        [_record(1, {"http://x.org/a#": 100}), _record(2, {"http://x.org/a/": 50})]
    )
    assert "http://x.org/a" not in owners
