"""The namespace rules every coupling measure reads."""

from __future__ import annotations

import pytest

from ontometer_corpus.namespaces import (
    LANGUAGE_NAMESPACES,
    OBO_BASE_NS,
    UNOWNABLE_NAMESPACES,
    iri_namespace,
)


@pytest.mark.parametrize(
    ("iri", "expected"),
    [
        ("http://example.org/onto#Thing", "http://example.org/onto#"),
        ("http://example.org/onto/Thing", "http://example.org/onto/"),
        ("http://example.org/a/b#C", "http://example.org/a/b#"),
        ("", None),
        (None, None),
        ("nodelimiter", None),
    ],
)
def test_namespace_split(iri, expected):
    assert iri_namespace(iri) == expected


def test_obo_terms_split_by_prefix_not_by_the_shared_path():
    # Every OBO term lives directly under the same path. Returning the bare
    # path would collapse GO, CHEBI and everything else into one dependency.
    assert iri_namespace("http://purl.obolibrary.org/obo/GO_0008150") == f"{OBO_BASE_NS}GO_"
    assert iri_namespace("http://purl.obolibrary.org/obo/CHEBI_36357") == f"{OBO_BASE_NS}CHEBI_"


def test_malformed_obo_local_name_falls_back_to_the_generic_path():
    assert iri_namespace("http://purl.obolibrary.org/obo/not-an-obo-id") == OBO_BASE_NS


def test_language_set_is_the_representation_languages_only():
    for ns in (
        "http://www.w3.org/2002/07/owl#",
        "http://www.w3.org/2000/01/rdf-schema#",
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "http://www.w3.org/2001/XMLSchema#",
    ):
        assert ns in LANGUAGE_NAMESPACES
    # Vocabularies written in those languages are not listed, however popular.
    for ns in (
        "http://purl.org/dc/terms/",
        "http://www.w3.org/2004/02/skos/core#",
        "http://xmlns.com/foaf/0.1/",
        "http://www.w3.org/ns/prov#",
    ):
        assert ns not in LANGUAGE_NAMESPACES


def test_the_bare_obo_root_is_unownable_but_not_a_language():
    assert OBO_BASE_NS in UNOWNABLE_NAMESPACES
    assert OBO_BASE_NS not in LANGUAGE_NAMESPACES


def test_the_corpus_parser_uses_the_shared_definitions():
    # A second copy of the set would drift, and coupling numbers from
    # different measures would stop being comparable.
    from ontometer_corpus.pipeline import parse

    assert parse._UNOWNABLE_NAMESPACES is UNOWNABLE_NAMESPACES
    assert parse._iri_namespace is iri_namespace
