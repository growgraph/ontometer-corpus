"""Licence declarations on the ontology header are captured raw by every parser path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import rdflib

from ontometer_corpus.pipeline.db import (
    create_schema,
    get_connection,
    reset_parse_state,
    update_parse_ok,
)
from ontometer_corpus.pipeline.parse import (
    MAX_LICENCE_CHARS,
    _parse_obo,
    _parse_owl_xml,
    _parse_worker_streaming,
    extract_metadata,
)

CC_BY = "http://creativecommons.org/licenses/by/4.0/"

TTL = f"""
@prefix owl:     <http://www.w3.org/2002/07/owl#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix schema:  <https://schema.org/> .

<> a owl:Ontology ;
    dcterms:license <{CC_BY}> ;
    dcterms:rights "Free to reuse" .
<http://example.org/o#Dog> a owl:Class ;
    schema:license <http://example.org/item-licence> .
"""

OWX = f"""<?xml version="1.0"?>
<Ontology xmlns="http://www.w3.org/2002/07/owl#" ontologyIRI="http://example.org/o">
  <Prefix name="terms" IRI="http://purl.org/dc/terms/"/>
  <Annotation>
    <AnnotationProperty abbreviatedIRI="terms:license"/>
    <IRI>{CC_BY}</IRI>
  </Annotation>
  <Declaration><Class IRI="http://example.org/o#A"/></Declaration>
  <Declaration><Class IRI="http://example.org/o#B"/></Declaration>
  <SubClassOf>
    <Annotation>
      <AnnotationProperty IRI="http://purl.org/dc/terms/license"/>
      <Literal>axiom-level</Literal>
    </Annotation>
    <Class IRI="http://example.org/o#A"/>
    <Class IRI="http://example.org/o#B"/>
  </SubClassOf>
</Ontology>""".encode()

OBO = f"""format-version: 1.2
ontology: xyz
property_value: http://purl.org/dc/terms/license {CC_BY}
property_value: dcterms-license "CC BY 4.0" xsd:string

[Term]
id: XYZ:1
name: thing
property_value: http://purl.org/dc/terms/license "term-level" xsd:string
""".encode()


def _stream(path: Path) -> dict:
    result = _parse_worker_streaming(1, path, "plain", None)
    assert result is not None
    _, status, _, meta, error = result
    assert (status, error) == ("ok", None)
    assert meta is not None
    return meta


def test_turtle_licence_on_relative_ontology_subject():
    meta = extract_metadata(rdflib.Graph().parse(data=TTL, format="turtle"))
    assert sorted(meta["licence"]) == sorted([CC_BY, "Free to reuse"])


def test_licence_on_non_header_subject_ignored():
    meta = extract_metadata(rdflib.Graph().parse(data=TTL, format="turtle"))
    assert "http://example.org/item-licence" not in meta["licence"]


def test_streamed_rdfxml_licence(tmp_path):
    xml = rdflib.Graph().parse(data=TTL, format="turtle").serialize(format="xml")
    path = tmp_path / "o.rdf"
    path.write_text(xml)
    assert sorted(_stream(path)["licence"]) == sorted([CC_BY, "Free to reuse"])


def test_streamed_per_item_licences_bounded(tmp_path):
    items = "\n".join(
        f"<http://example.org/o#i{i}> <https://schema.org/license> <http://example.org/l{i}> ."
        for i in range(3000)
    )
    path = tmp_path / "o.ttl"
    path.write_text(TTL + items)
    assert sorted(_stream(path)["licence"]) == sorted([CC_BY, "Free to reuse"])


def test_owl_xml_licence_header_annotation_only():
    _, fmt, error, meta = _parse_owl_xml(OWX, "o")
    assert (fmt, error) == ("owl_xml", None)
    assert meta is not None
    assert meta["licence"] == [CC_BY]


def test_owl_xml_abbreviated_licence_resolved():
    _, _, _, meta = _parse_owl_xml(OWX, "o")
    assert meta is not None
    assert all(not v.startswith("terms:") for v in meta["licence"])


def test_obo_header_licence_property_value():
    _, fmt, error, meta = _parse_obo(OBO, "o")
    assert (fmt, error) == ("obo", None)
    assert meta is not None
    assert meta["licence"] == [CC_BY, "CC BY 4.0"]


def test_long_literal_licence_truncated():
    long_text = "x" * (MAX_LICENCE_CHARS + 100)
    ttl = f"""
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    <http://example.org/o> a owl:Ontology ;
        <http://purl.org/dc/terms/rights> "{long_text}" .
    """
    meta = extract_metadata(rdflib.Graph().parse(data=ttl, format="turtle"))
    assert meta["licence"] == ["x" * MAX_LICENCE_CHARS]


@pytest.mark.parametrize("path_kind", ["rdflib", "stream", "owl_xml", "obo"])
def test_every_parser_emits_licence_key(tmp_path, path_kind):
    bare_ttl = (
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "<http://example.org/o> a owl:Ontology .\n"
        "<http://example.org/o#A> a owl:Class .\n"
    )
    if path_kind == "rdflib":
        meta = extract_metadata(rdflib.Graph().parse(data=bare_ttl, format="turtle"))
    elif path_kind == "stream":
        path = tmp_path / "o.ttl"
        path.write_text(bare_ttl)
        meta = _stream(path)
    elif path_kind == "owl_xml":
        meta = _parse_owl_xml(
            b'<Ontology xmlns="http://www.w3.org/2002/07/owl#" ontologyIRI="http://e/o">'
            b'<Declaration><Class IRI="http://e/o#A"/></Declaration></Ontology>',
            "o",
        )[3]
    else:
        meta = _parse_obo(b"format-version: 1.2\nontology: xyz\n\n[Term]\nid: XYZ:1\n", "o")[3]
    assert meta is not None
    assert meta["licence"] == []


def _insert_row(conn) -> int:
    cur = conn.execute(
        """
        INSERT INTO corpus_registry (
            source_id, canonical_url, retrieved_file, retrieval_date, source_registry,
            created_at, updated_at
        ) VALUES ('lov:o::1', 'u', 'objects/x', '2026-01-01', 'LOV', 'x', 'x')
        """
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def _ok(conn, row_id: int, licence: list[str] | None) -> None:
    update_parse_ok(
        conn,
        row_id,
        parse_format="turtle",
        ontology_iri="http://e/o",
        ontology_version_iri=None,
        ontology_version_info=None,
        triple_count=1,
        class_count=0,
        property_count=0,
        individual_count=0,
        imports=[],
        content_hash=None,
        licence=licence,
    )


def test_update_parse_ok_stores_licence_json(tmp_path):
    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    row_id = _insert_row(conn)
    _ok(conn, row_id, [CC_BY])
    stored = conn.execute("SELECT licence FROM corpus_registry").fetchone()[0]
    assert json.loads(stored) == [CC_BY]


def test_reparse_reset_clears_licence(tmp_path):
    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    _ok(conn, _insert_row(conn), [CC_BY])
    reset_parse_state(conn)
    assert conn.execute("SELECT licence FROM corpus_registry").fetchone()[0] is None


def test_blank_node_licence_ignored():
    ttl = """
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix dcterms: <http://purl.org/dc/terms/> .
    <http://example.org/o> a owl:Ontology ;
        dcterms:license [ a dcterms:LicenseDocument ] , <http://creativecommons.org/licenses/by/4.0/> .
    """
    meta = extract_metadata(rdflib.Graph().parse(data=ttl, format="turtle"))
    assert meta["licence"] == ["http://creativecommons.org/licenses/by/4.0/"]
