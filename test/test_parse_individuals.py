"""Individual counts: typed and SKOS individuals, not only owl:NamedIndividual."""

import io

import rdflib

from ontometer_corpus.pipeline.parse import (
    _parse_owl_xml,
    _stream_parse_ntriples_like,
    extract_metadata,
)

TTL = """
@prefix :     <http://example.org/o#> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix swrl: <http://www.w3.org/2003/11/swrl#> .

<http://example.org/o> a owl:Ontology .
:Dog a owl:Class .
:Lab a owl:Class ; a skos:Concept .          # punned schema term: not an individual
:hasKeeper a owl:ObjectProperty .
:rex a owl:NamedIndividual, :Dog .           # declared
:fido a :Dog .                               # typed only
:old a owl:Thing .                           # untyped individual, older OWL
:c1 a skos:Concept .
:c2 a skos:Concept .
:scheme a skos:ConceptScheme .
:x a swrl:Variable .                         # rule machinery: not an individual
"""

EXPECTED = {
    "individual_count": 6,  # rex fido old c1 c2 scheme
    "declared_individual_count": 1,
    "skos_concept_count": 2,
}


def _counts(meta: dict | None) -> dict:
    assert meta is not None
    return {k: meta[k] for k in EXPECTED}


def test_rdflib_path_counts_typed_and_skos_individuals():
    graph = rdflib.Graph().parse(data=TTL, format="turtle")
    assert _counts(extract_metadata(graph)) == EXPECTED


def test_streaming_ntriples_matches_rdflib_path():
    nt = rdflib.Graph().parse(data=TTL, format="turtle").serialize(format="nt")
    meta = _stream_parse_ntriples_like(io.StringIO(nt), "o")
    assert _counts(meta) == EXPECTED


def test_owl_xml_counts_skos_class_assertions():
    owx = b"""<?xml version="1.0"?>
<Ontology xmlns="http://www.w3.org/2002/07/owl#" ontologyIRI="http://example.org/o">
  <Prefix name="skos" IRI="http://www.w3.org/2004/02/skos/core#"/>
  <Declaration><Class IRI="http://example.org/o#Dog"/></Declaration>
  <Declaration><NamedIndividual IRI="http://example.org/o#rex"/></Declaration>
  <ClassAssertion>
    <Class abbreviatedIRI="skos:Concept"/>
    <NamedIndividual IRI="http://example.org/o#c1"/>
  </ClassAssertion>
</Ontology>"""
    _, fmt, error, meta = _parse_owl_xml(owx, "o")
    assert error is None and fmt == "owl_xml"
    assert _counts(meta) == {
        "individual_count": 2,
        "declared_individual_count": 2,
        "skos_concept_count": 1,
    }
