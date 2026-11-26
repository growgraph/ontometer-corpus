"""Large-file streaming parse: exact counts from an in-process RDF parser."""

import gzip
from pathlib import Path

import pytest
import rdflib

from ontometer_corpus.pipeline.parse import _parse_worker_streaming, extract_metadata

TTL = """
@prefix :     <http://example.org/o#> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .

<http://example.org/o> a owl:Ontology ;
    owl:versionIRI <http://example.org/o/2024-01-01> .
:Dog a owl:Class ;
    rdfs:subClassOf [ a owl:Restriction ;
                      owl:onProperty :hasKeeper ;
                      owl:someValuesFrom :Keeper ] .
:Keeper a owl:Class .
:hasKeeper a owl:ObjectProperty .
:rex a owl:NamedIndividual, :Dog .
:c1 a skos:Concept .
"""

COUNTS = ("class_count", "property_count", "individual_count", "skos_concept_count")


@pytest.fixture(autouse=True)
def no_external_binaries(monkeypatch):
    """The streaming path must not depend on anything outside the Python environment."""
    monkeypatch.setenv("PATH", "")


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _stream(path: Path, kind: str = "plain") -> tuple:
    result = _parse_worker_streaming(1, path, kind, None)
    assert result is not None, "format not recognised by the streaming path"
    return result


def _expected() -> dict:
    meta = extract_metadata(rdflib.Graph().parse(data=TTL, format="turtle"))
    return {k: meta[k] for k in COUNTS}


def test_turtle_with_prefixed_names_and_nesting_counts_exactly(tmp_path):
    path = _write(tmp_path, "o.ttl", TTL.encode())

    _, status, fmt, meta, error = _stream(path)

    assert (status, fmt, error) == ("ok", "stream_nt_cvt", None)
    assert meta is not None
    assert {k: meta[k] for k in COUNTS} == _expected()
    assert meta["ontology_iri"] == "http://example.org/o"
    assert meta["ontology_version_iri"] == "http://example.org/o/2024-01-01"


def test_rdfxml_counts_match_the_in_memory_path(tmp_path):
    xml = rdflib.Graph().parse(data=TTL, format="turtle").serialize(format="xml")
    path = _write(tmp_path, "o.owl", xml.encode())

    _, status, fmt, meta, error = _stream(path)

    assert (status, fmt, error) == ("ok", "stream_xml_cvt", None)
    assert meta is not None
    assert {k: meta[k] for k in COUNTS} == _expected()


def test_rdfxml_typed_node_elements(tmp_path):
    xml = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:skos="http://www.w3.org/2004/02/skos/core#"
         xmlns:ex="http://example.org/o#">
  <owl:Class rdf:about="http://example.org/o#Dog"/>
  <ex:Dog rdf:about="http://example.org/o#fido"/>
  <skos:Concept rdf:about="http://example.org/o#c1"/>
  <owl:Restriction rdf:about="http://example.org/o#r"/>
</rdf:RDF>"""
    path = _write(tmp_path, "o.rdf", xml)

    _, status, _, meta, _ = _stream(path)

    assert status == "ok" and meta is not None
    assert (meta["individual_count"], meta["declared_individual_count"]) == (2, 0)
    assert meta["skos_concept_count"] == 1


def test_nquads_dump_is_read_after_turtle_rejects_it(tmp_path):
    nq = (
        "<http://example.org/o#A> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
        "<http://www.w3.org/2002/07/owl#Class> <http://example.org/g> .\n"
        "<http://example.org/o#B> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
        "<http://www.w3.org/2002/07/owl#Class> <http://example.org/g> .\n"
    )
    path = _write(tmp_path, "o.nq", nq.encode())

    _, status, fmt, meta, _ = _stream(path)

    assert (status, fmt) == ("ok", "stream_nt_cvt")
    assert meta is not None
    assert (meta["triple_count"], meta["class_count"]) == (2, 2)


def test_gzip_content_is_streamed(tmp_path):
    path = _write(tmp_path, "o.ttl.gz", gzip.compress(TTL.encode()))

    _, status, _, meta, _ = _stream(path, "gzip")

    assert status == "ok" and meta is not None
    assert {k: meta[k] for k in COUNTS} == _expected()


def test_malformed_turtle_fails_naming_every_syntax_tried(tmp_path):
    path = _write(tmp_path, "o.ttl", b"@prefix : <http://e/> .\n:a :b .\n")

    _, status, fmt, meta, error = _stream(path)

    assert (status, fmt, meta) == ("fail", None, None)
    assert error is not None and "TriG" in error and "N-Quads" in error


def test_rdf_document_without_triples_is_unsupported(tmp_path):
    xml = (
        b'<?xml version="1.0"?>\n<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>'
    )
    path = _write(tmp_path, "o.rdf", xml)

    _, status, _, _, error = _stream(path)

    assert status == "unsupported"
    assert error is not None and "0 triples" in error


def test_trig_dump_is_read(tmp_path):
    trig = (
        "<http://example.org/g> {\n"
        "<http://example.org/o#A> a <http://www.w3.org/2002/07/owl#Class> .\n"
        "}\n"
    )
    path = _write(tmp_path, "o.trig", trig.encode())

    _, status, fmt, meta, _ = _stream(path)

    assert (status, fmt) == ("ok", "stream_nt_cvt")
    assert meta is not None and meta["class_count"] == 1


def test_rdfxml_in_a_legacy_encoding_is_transcoded(tmp_path):
    xml = (
        '<?xml version="1.0" encoding="ISO-8859-1" ?>\n'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"\n'
        '         xmlns:owl="http://www.w3.org/2002/07/owl#">\n'
        '  <owl:Class rdf:about="http://example.org/o#Café"/>\n'
        "</rdf:RDF>"
    )
    path = _write(tmp_path, "o.rdf", xml.encode("latin-1"))

    _, status, _, meta, error = _stream(path)

    assert (status, error) == ("ok", None)
    assert meta is not None
    assert ("http://example.org/o#Café", "class") in meta["entities"]


def test_malformed_iris_and_language_tags_are_tolerated(tmp_path):
    xml = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
  <owl:Class rdf:about="http://example.org/o#alpha[1]|beta">
    <rdfs:comment xml:lang="download/attachment/sop.pdf">x</rdfs:comment>
  </owl:Class>
  <owl:Class rdf:about="http://example.org/o#B"/>
</rdf:RDF>"""
    path = _write(tmp_path, "o.rdf", xml)

    _, status, _, meta, error = _stream(path)

    assert (status, error) == ("ok", None)
    assert meta is not None and meta["class_count"] == 2
