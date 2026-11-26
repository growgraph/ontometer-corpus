"""Per-use classification of borrowed terms: logical, annotation, declaration."""

from __future__ import annotations

import rdflib

from ontometer_corpus.usage import (
    ANNOTATION,
    DECLARED,
    LOGICAL,
    classify_graph,
    classify_triples,
    ntriples,
)

_TTL = """
@prefix : <http://ex.org/o#> . @prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> . @prefix foaf: <http://xmlns.com/foaf/0.1/> .
@prefix dct: <http://purl.org/dc/terms/> . @prefix prov: <http://www.w3.org/ns/prov#> .
@prefix vann: <http://purl.org/vocab/vann/> .
<http://ex.org/o> a owl:Ontology ; dct:creator <https://orcid.org/1> ; vann:preferredNamespacePrefix "o" .
:Researcher a owl:Class ; rdfs:subClassOf foaf:Person ; dct:description "d" .
:Project a owl:Class ; rdfs:subClassOf [ a owl:Restriction ;
    owl:onProperty prov:wasAssociatedWith ; owl:someValuesFrom :Researcher ] .
:Team a owl:Class ; owl:unionOf ( foaf:Group foaf:Organization ) .
:alice a :Researcher ; foaf:knows :bob ; dct:creator :bob .
foaf:Agent a owl:Class .
"""


def _graph() -> rdflib.Graph:
    return rdflib.Graph().parse(data=_TTL, format="turtle")


def test_modes_follow_position_not_vocabulary():
    modes = classify_graph(_graph())
    assert modes["http://xmlns.com/foaf/0.1/Person"] == LOGICAL  # subclass parent
    assert modes["http://www.w3.org/ns/prov#wasAssociatedWith"] == LOGICAL  # restriction
    assert modes["http://xmlns.com/foaf/0.1/Group"] == LOGICAL  # union member
    assert modes["http://xmlns.com/foaf/0.1/knows"] == LOGICAL  # fact between individuals
    # dcterms:creator is a fact between individuals, metadata on the header:
    # one logical use makes the term logical.
    assert modes["http://purl.org/dc/terms/creator"] == LOGICAL
    assert modes["http://purl.org/dc/terms/description"] == ANNOTATION  # literal on a class
    assert modes["http://purl.org/vocab/vann/preferredNamespacePrefix"] == ANNOTATION
    assert modes["http://xmlns.com/foaf/0.1/Agent"] == DECLARED
    assert "http://www.w3.org/2002/07/owl#Class" not in modes  # the language itself


def test_streamed_ntriples_classify_like_the_in_memory_graph():
    graph = _graph()
    text = graph.serialize(format="nt")
    lines = text.splitlines(keepends=True)
    assert classify_triples(lambda: ntriples(lines)) == classify_graph(graph)
